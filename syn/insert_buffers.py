#!/usr/bin/env python3
"""Insert buffer trees on the high-fanout nets of a yosys gate-level netlist.

Pre-layout STA of CVA6 was dominated by wide-fanout control registers
(issue_entry_o bits driving 2000+ loads -> 5.8 pF -> 14 ns clk-to-Q).  abc's
buffer command cannot help: from its point of view the FF outputs are the
network inputs.  This pass rewrites the netlist directly:

  for every net whose fanout exceeds MAXF, build a BUF_X16 tree
  (leaf drives <= MAXF loads, every level fans out <= MAXF), move the loads
  to the leaf outputs, and append the buffer cells at the end of the module.

clk_i / rst_ni are skipped: the clock is ideal in the SDC and the async reset
is not on any setup path.
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
DRIVER_PINS = ('.Z(', '.ZN(', '.Q(', '.QN(')
EXCLUDE = {'clk_i', 'rst_ni'}

REF = re.compile(r'\\[^\s]+(?:\s*\[\d+(?::\d+)?\])?|[A-Za-z_$][\w$]*(?:\[\d+(?::\d+)?\])?')
DECL = re.compile(r'^\s*(?:wire|reg|input|output|inout)\b')
DECL_NAME = re.compile(r'\\[^\s,]+|[A-Za-z_$][\w$]*')


def declared_nets(path):
    names = set()
    with open(path) as f:
        for line in f:
            if DECL.match(line):
                for nm in DECL_NAME.findall(line):
                    if nm not in ('wire', 'reg', 'input', 'output', 'inout', 'signed'):
                        names.add(nm)
    return names


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


def main(inp, outp, threshold):
    nets = declared_nets(inp)

    # pass 1: reference counts
    counts = Counter()
    with open(inp) as f:
        for line in f:
            if DECL.match(line):
                continue
            for ref in REF.findall(line):
                if ref.startswith('\\'):
                    base = ref.split(' ')[0]
                else:
                    base = re.sub(r'\[\d+(?::\d+)?\]$', '', ref)
                if base in nets:
                    counts[ref.rstrip()] += 1
    targets = {n: c - 1 for n, c in counts.items()
               if c - 1 > threshold and n not in EXCLUDE
               and n.split(' ')[0] not in EXCLUDE}
    print(f'target nets (fanout>{threshold}): {len(targets)}')

    # pass 2: occurrences; mark the single driver occurrence per net
    occ = {n: [] for n in targets}
    with open(inp) as f:
        for lineno, line in enumerate(f, 1):
            for n in targets:
                if n in line:
                    pat = re.escape(n) + r'\s*\)'
                    is_driver = any(re.search(re.escape(dp) + pat, line)
                                    for dp in DRIVER_PINS) \
                        or (line.lstrip().startswith('assign ')
                            and line.lstrip()[6:].lstrip().startswith(n))
                    occ[n].append((lineno, is_driver, line))

    bufs = []          # (out_name, in_net)
    repl = {}          # (lineno, net) -> replacement name (per occurrence)
    tag = 0
    for n, lst in occ.items():
        drv = [x for x in lst if x[1]]
        loads = [x for x in lst if not x[1]]
        if not drv:
            print(f'  !! no driver found for {n[:60]} ({len(loads)} loads), skip')
            continue
        k = len(loads)
        levels = plan(k)
        # build levels top-down
        node_names = [n]                       # current level outputs
        leaf_outputs = None
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
        for j, (lineno, _d, _l) in enumerate(loads):
            out = leaf_outputs[j % len(leaf_outputs)]
            repl.setdefault(lineno, []).append((n, out))
        # widen leaves if plan rounded down (some leaves take one extra load)
        # (j % len handles it)

    print(f'buffers inserted: {len(bufs)}')

    # pass 3: rewrite
    with open(inp) as f, open(outp, 'w') as g:
        for lineno, line in enumerate(f, 1):
            out_line = line
            for (n, out) in repl.get(lineno, []):
                out_line = out_line.replace(n, out, 1)
            g.write(out_line)
            if line.strip() == 'endmodule':
                for name in sorted({b[0] for b in bufs}):
                    g.write(f'  wire {name};\n')
                for out_name, in_net, cell in bufs:
                    g.write(f'  {cell} {out_name}_c (\n')
                    g.write(f'    .A({in_net}),\n')
                    g.write(f'    .Z({out_name})\n')
                    g.write('  );\n')
    print('done')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 32)
