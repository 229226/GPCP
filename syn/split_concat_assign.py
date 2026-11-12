#!/usr/bin/env python3
"""Bit-blast wide `assign` statements so iEDA's Verilog parser accepts them.

iEDA (Rust verilog parser) rejects assignments whose LHS is a concatenation or
a part-select, and wide binary constants; yosys write_verilog -noexpr produces
plenty of both, e.g.
    assign { _000020_[54], _000020_[0] } = { noc_req_o[389], noc_req_o[389] };
    assign \\name [255:128] = 128'b0000....;
    assign { a[133], a[3] } = rvfi_probes[68:67];

This rewrites every *simple* assign (both sides made of bit refs, part-selects,
concatenations and constants) into single-bit assigns, MSB-first.  Assigns with
operators on either side are expressions (the logic lives in cells in a yosys
netlist) and are left untouched.  Element widths come from the netlist's own
wire/reg declarations.
"""
import re
import sys

RANGE = re.compile(r'^(.*)\[(\d+):(\d+)\]$')
INDEX = re.compile(r'^(.*)\[(\d+)\]$')
CONST = re.compile(r"^(\d+)'([bhd])([0-9a-fA-FxXzZ_]+)$")
DECL = re.compile(r'^\s*(?:wire|reg|input|output|inout)\b(.*);\s*$')
DECL_RANGE = re.compile(r'^\s*\[(\d+):(\d+)\]\s*(.*)$')
NAME = re.compile(r'\\[^\s,]+|[A-Za-z_][\w$]*')
ASSIGN = re.compile(r'^(\s*)assign\s+(.+?)\s*=\s*(.+);\s*$')
# binary operators that mean "this is an expression, not a bit list"
OPS = set('&|^~+-*/%!<>')

WIDTH = {}


def build_widths(path):
    with open(path) as f:
        for line in f:
            if not re.match(r'^\s*(wire|reg|input|output|inout)\b', line):
                continue
            m = DECL.match(line)
            if not m:
                continue
            rest, w = m.group(1), 1
            mr = DECL_RANGE.match(rest)
            if mr:
                w = abs(int(mr.group(1)) - int(mr.group(2))) + 1
                rest = mr.group(3)
            for nm in NAME.findall(rest):
                if nm not in ('wire', 'reg', 'input', 'output', 'inout', 'signed'):
                    WIDTH[nm] = w
    print(f'width map: {len(WIDTH)} names')


def split_top(s):
    elems, depth, cur = [], 0, ''
    for ch in s:
        if ch in '[(':
            depth += 1
        elif ch in '])':
            depth -= 1
        if ch == ',' and depth == 0:
            elems.append(cur.strip())
            cur = ''
        else:
            cur += ch
    if cur.strip():
        elems.append(cur.strip())
    return elems


def bitref(base, i):
    b = base.rstrip()
    return f'{b} [{i}]' if b.startswith('\\') else f'{b}[{i}]'


def expand_const(m):
    width = int(m.group(1))
    base = m.group(2).lower()
    digits = m.group(3).replace('_', '')
    if base == 'b':
        bits = digits
    elif base == 'h':
        bits = ''.join(format(int(c, 16), '04b') if c not in 'xXzZ' else c * 4
                       for c in digits)
    else:
        bits = format(int(digits), 'b')
    bits = bits.zfill(width) if len(bits) < width else bits[-width:]
    return [f"1'b{b}" for b in bits]


def expand(e):
    e = e.strip()
    m = RANGE.match(e)
    if m:
        base, hi, lo = m.group(1), int(m.group(2)), int(m.group(3))
        step = -1 if hi >= lo else 1
        return [bitref(base, i) for i in range(hi, lo + step, step)]
    m = INDEX.match(e)
    if m:
        return [e]
    m = CONST.match(e)
    if m:
        return expand_const(m)
    w = WIDTH.get(e, 1)
    if w > 1:
        return [bitref(e, i) for i in range(w - 1, -1, -1)]
    return [e + ' ' if e.startswith('\\') else e]


def bits(side):
    out = []
    for e in split_top(side):
        out += expand(e)
    return out


def has_op(s):
    return any(c in OPS for c in s)


def strip_braces(s):
    s = s.strip()
    return s[1:-1] if s.startswith('{') and s.endswith('}') else s


def process(line):
    m = ASSIGN.match(line)
    if not m:
        return None
    indent, lhs, rhs = m.group(1), m.group(2).strip(), m.group(3).strip()
    if has_op(lhs) or has_op(rhs):
        return None                      # expression: leave to the cells
    L, R = bits(strip_braces(lhs)), bits(strip_braces(rhs))
    if len(L) != len(R):
        return False
    out = []
    for l, r in zip(L, R):
        if l == r:
            continue
        out.append(f'{indent}assign {l} = {r};')
    return out if out else []


def main(inp, outp):
    build_widths(inp)
    ok = bail = 0
    with open(inp) as f, open(outp, 'w') as g:
        for line in f:
            if line.lstrip().startswith('assign '):
                res = process(line)
                if res is False:
                    bail += 1
                    g.write(line)
                    continue
                if res is not None:
                    ok += 1
                    for o in res:
                        g.write(o + '\n')
                    continue
            g.write(line)
    print(f'bit-blasted {ok} assigns, {bail} left unsplit')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
