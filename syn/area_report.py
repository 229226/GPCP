#!/usr/bin/env python3
"""Area distribution of a mapped netlist: by functional group and by cell type.

The synthesis flow flattens the design and renames wires, so *combinational*
cells only keep their type (`_NNNN_`), while registers keep their RTL names.
Therefore:

  * registers are attributed to a functional block by name prefix
    (`u_array.*` = PE array, `perf_*` = performance counters, ...);
  * combinational cells are reported as one bucket plus a per-cell-type table
    (mux / buffer / logic dominance is what usually decides the next step).

Usage:  python3 syn/area_report.py <netlist.v> <liberty> [--json]
"""
import json
import re
import sys

CELL = re.compile(r'^\s+(?P<cell>[A-Za-z_][\w]*)\s+\\?(?P<name>\S+)\s+\(')
LIB_CELL = re.compile(r'^\s*cell\s*\(\s*"?(?P<name>[\w]+)"?\s*\)\s*\{')
LIB_AREA = re.compile(r'^\s*area\s*:\s*(?P<area>[0-9.eE+-]+)\s*;')

# register-name prefix -> functional block (first match wins, order matters)
GROUPS = [
    ("性能计数器", ("perf_",)),
    ("旋转表", ("rot_",)),
    ("描述符环 + 拷贝引擎 + 写窗口", (
        "dma_", "desc_", "ring_", "out_", "in_", "arr_", "rep_", "dstate_",
        "d0_", "d1_", "d4_", "win_", "rd_desc", "wr_status", "irq_",
        "cycle_", "num_", "busy_", "done_", "status_", "idle")),
    ("取指 / IMEM 控制", ("imem", "inst_flat")),
    ("spad 窗口读", ("pe_rd", "spad")),
]

PE_PREFIX = "u_array."
PE_LABEL = "PE 阵列（16 PE + 网格布线）"
TOP_COMB = "组合逻辑（ABC 命名，无法归属）"


def liberty_areas(path):
    area, cur = {}, None
    with open(path, errors="ignore") as f:
        for line in f:
            m = LIB_CELL.match(line)
            if m:
                cur = m.group("name")
                continue
            if cur:
                m = LIB_AREA.match(line)
                if m:
                    area[cur] = float(m.group("area"))
                    cur = None
    return area


def classify(reg):
    for label, prefixes in GROUPS:
        if reg.startswith(prefixes):
            return label
    return "其他顶层寄存器"


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    netlist, lib = sys.argv[1], sys.argv[2]
    as_json = "--json" in sys.argv[3:]

    area = liberty_areas(lib)
    groups, types = {}, {}
    missing = set()
    total = 0.0
    for line in open(netlist, errors="ignore"):
        m = CELL.match(line)
        if not m:
            continue
        cell, name = m.group("cell"), m.group("name")
        a = area.get(cell)
        if a is None:
            missing.add(cell)
            a = 0.0
        total += a
        types[cell] = types.get(cell, 0.0) + a
        if name.startswith(PE_PREFIX):
            key = PE_LABEL
        elif "_$_" in name:
            reg = re.sub(r"\[\d+\]", "[]", name.split("_$_")[0])
            key = classify(reg)
        else:
            key = TOP_COMB
        g = groups.setdefault(key, [0.0, 0])
        g[0] += a
        g[1] += 1

    rows = sorted(((k, v[0], v[1]) for k, v in groups.items()),
                  key=lambda r: -r[1])
    print(f"netlist : {netlist}")
    print(f"liberty : {lib} ({len(area)} cells)")
    print(f"total   : {total:,.1f} um^2, {sum(r[2] for r in rows):,} instances")
    if missing:
        print(f"WARNING : {len(missing)} cell types missing from liberty: "
              f"{sorted(missing)[:5]}")
    print()
    print(f"{'功能块':44s}{'面积/um^2':>12s}{'占比':>8s}{'实例':>8s}")
    for k, a, n in rows:
        print(f"{k:44s}{a:12,.1f}{100.0 * a / total:7.1f}%{n:8d}")
    seq = sum(a for k, a, _ in rows if k not in (TOP_COMB,))
    print(f"\n寄存器（可归属）{seq:,.1f} um^2 ({100.0 * seq / total:.1f}%) / "
          f"组合逻辑 {total - seq:,.1f} um^2 ({100.0 * (total - seq) / total:.1f}%)")
    print()
    print("按单元类型（面积 Top 12）")
    for k, a in sorted(types.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {k:34s}{a:12,.1f}{100.0 * a / total:7.1f}%")

    if as_json:
        out = {
            "netlist": netlist,
            "liberty": lib,
            "total_area": total,
            "groups": [{"name": k, "area": a, "instances": n} for k, a, n in rows],
            "cell_types": dict(sorted(types.items(), key=lambda kv: -kv[1])),
        }
        print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
