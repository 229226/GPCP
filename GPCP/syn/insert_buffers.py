#!/usr/bin/env python3
"""Insert buffer trees on the high-fanout nets of a yosys gate-level netlist.

Pre-layout STA of CVA6 was dominated by wide-fanout control registers
(issue_entry_o bits driving 2000+ loads -> 5.8 pF -> 14 ns clk-to-Q).  abc's
buffer command cannot help: from its point of view the FF outputs are the
network inputs.  This pass rewrites the netlist directly:

  for every net whose fanout exceeds MAXF, build a BUF tree
  (leaf drives <= MAXF loads, every level fans out <= BRANCH), move the loads
  to the leaf outputs, and append the buffer cells at the end of the module.

clk_i / rst_n are skipped: the clock is ideal in the SDC and the async reset
is not on any setup path.

Usage: insert_buffers.py <in.v> <out.v> [maxfanout] [top_cell mid_cell leaf_cell]
"""
import re
import sys
from collections import Counter
from math import ceil

MAXF = 24          # loads per leaf buffer
BRANCH = 4         # fanout per intermediate tree level
BUF_LEAF = 'BUF_X16'
BUF_MID = 'BUF_X4'
BUF_TOP = 'BUF_X2'  # smallest input cap: the FF has to drive these
DRIVER_PINS = ('Z', 'ZN', 'Q', 'QN')
EXCLUDE = {'clk', 'rst_n'}

DECL = re.compile(r'^\s*(?:wire|reg|input|output|inout)\b')
DECL_NAME = re.compile(r'\\[^\s,]+|[A-Za-z_$][\w$]*')
# escaped identifiers end at whitespace or punctuation; plain ones at the same
REF = re.compile(r'\\[^\s(),;]+|[A-Za-z_$][\w$]*')


def declared_nets(path):
    names = set()
    with open(path) as f:
        for line in f:
            if DECL.match(line):
                for nm in DECL_NAME.findall(line):
                    if nm not in ('wire', 'reg', 'input', 'output', 'inout', 'signed'):
                        names.add(nm)
    return names


def input_nets(path):
    """module input ports: their driver is outside the module, so the port net
    itself is the root of the buffer tree."""
    names = set()
    with open(path) as f:
        for line in f:
            if line.lstrip().startswith('input'):
                for nm in DECL_NAME.findall(line):
                    if nm not in ('input', 'wire', 'reg', 'signed', 'logic'):
                        names.add(nm)
    return names


def base_name(tok):
    """strip an unescaped bit select from a plain identifier (\a[3] is a net)."""
    if tok.startswith('\\'):
        return tok
    return re.sub(r'\[\d+(?::\d+)?\]$', '', tok)


def is_driver(line, tok):
    for pin in DRIVER_PINS:
        if re.search(r'\.' + pin + r'\(\s*' + re.escape(tok) + r'(?=[\s),;]|$)', line):
            return True
    if line.lstrip().startswith('assign '):
        rest = line.lstrip()[6:].lstrip()
        if rest.startswith(tok) and (len(rest) == len(tok) or
                                     not (rest[len(tok)].isalnum() or rest[len(tok)] in '_$')):
            return True
    return False


def replace_token(line, tok, out, count=1):
    pat = re.compile(re.escape(tok) + r'(?=[\s(),;]|$)')
    return pat.sub(out, line, count=count)


def plan(k):
    """levels of buffer counts, top first.
    bottom level: ceil(k / MAXF) leaf buffers, each driving <= MAXF loads;
    every higher level fans out <= BRANCH."""
    levels = [ceil(k / MAXF)]          # leaf level
    cur = levels[0]
    while cur > BRANCH:
        cur = ceil(cur / BRANCH)
        levels.append(cur)
    levels.reverse()
    return levels


def buf_for(level_idx, n_levels):
    if level_idx == 0:
        return BUF_TOP
    if level_idx == n_levels - 1:
        return BUF_LEAF
    return BUF_MID


def main(inp, outp, threshold, top=BUF_TOP, mid=BUF_MID, leaf=BUF_LEAF):
    nets = declared_nets(inp)
    ports_in = input_nets(inp)

    # pass 1: reference counts (token based, so \busy never matches \busy_q)
    counts = Counter()
    with open(inp) as f:
        for line in f:
            if DECL.match(line):
                continue
            for ref in REF.findall(line):
                base = base_name(ref)
                if base in nets:
                    counts[ref] += 1
    targets = {n: c - 1 for n, c in counts.items()
               if c - 1 > threshold and n not in EXCLUDE}
    print(f'target nets (fanout>{threshold}): {len(targets)}')

    # pass 2: occurrences; mark the single driver occurrence per net
    occ = {n: [] for n in targets}
    with open(inp) as f:
        for lineno, line in enumerate(f, 1):
            if DECL.match(line):
                continue
            for tok in set(REF.findall(line)):
                if tok in targets:
                    occ[tok].append((lineno, is_driver(line, tok)))

    bufs = []          # (out_name, in_net, cell)
    repl = {}          # lineno -> [(net, replacement name), ...]
    tag = 0
    for n, lst in occ.items():
        drivers = sum(1 for _l, d in lst if d)
        if drivers == 0:
            if n not in ports_in:
                print(f'  !! no driver found for {n[:60]} ({len(lst)} refs), skip')
                continue
        elif drivers > 1:
            print(f'  !! {drivers} drivers for {n[:60]}, skip')
            continue
        k = len(lst) - drivers
        levels = plan(k)
        # build levels top-down
        node_names = [n]                       # current level outputs
        for li, n_bufs in enumerate(levels):
            new_nodes = []
            for i in range(n_bufs):
                tag += 1
                in_net = node_names[i % len(node_names)]
                out_name = f'_gpcp_b{tag}'
                bufs.append((out_name, in_net, buf_for(li, len(levels))))
                new_nodes.append(out_name)
            node_names = new_nodes
        leaf_outputs = node_names              # == n_leaves buffers
        # distribute loads over leaf outputs, in order
        j = 0
        for lineno, is_drv in lst:
            if is_drv:
                continue
            out = leaf_outputs[j % len(leaf_outputs)]
            repl.setdefault(lineno, []).append((n, out))
            j += 1

    print(f'buffers inserted: {len(bufs)}')

    # pass 3: rewrite
    with open(inp) as f, open(outp, 'w') as g:
        for lineno, line in enumerate(f, 1):
            if line.strip() == 'endmodule':
                for out_name, in_net, cell in bufs:
                    g.write(f'  {cell} {out_name}_c (\n')
                    # escaped identifiers end at whitespace, so keep a space
                    # before the closing paren (yosys writes `.A(\net );`)
                    g.write(f'    .A({in_net} ),\n')
                    g.write(f'    .Z({out_name})\n')
                    g.write('  );\n')
            out_line = line
            for (n, out) in repl.get(lineno, []):
                out_line = replace_token(out_line, n, out, count=1)
            g.write(out_line)
            # iEDA's verilog reader wants every declaration before the first
            # instance, so the buffer wires go right after the module header
            if line.lstrip().startswith('module ') and line.rstrip().endswith(';'):
                for name in sorted({b[0] for b in bufs}):
                    g.write(f'  wire {name};\n')
    print('done')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2],
         int(sys.argv[3]) if len(sys.argv) > 3 else 32,
         *(sys.argv[4:7] if len(sys.argv) > 6 else ()))
