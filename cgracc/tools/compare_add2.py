#!/usr/bin/env python3
"""逐字对账：cgracc 复现 GPCP 规则映射器的 add2 微码。

对照物：../GPCP/sw/soc/gen/add2_prog.h（cgra/tools/gen_kernel_prog.py 生成）
等价 DFG（与 cgra/kernels/add2.yaml + rule_map.schedule 的落点一致）：
  每个实例 i（i = PE 号）：
    cycle0  ld  spad[bank26, word i] -> r0
    cycle1  ld  spad[bank27, word i] -> r1
    cycle2  add r0, r1               -> r2
    cycle3  st  r2 -> spad[bank28, word i]

用法：python3 tools/compare_add2.py [add2_prog.h 路径]
退出码 0 = 64 个字逐字相同。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cgracc.archreg import load_arch                      # noqa: E402
from cgracc.ir.dfg import DFG, Node                       # noqa: E402
from cgracc.sched.list_sched import ListScheduler         # noqa: E402
from cgracc.verify.legality import check                  # noqa: E402

BANK_A0, BANK_A1, BANK_SUM = 26, 27, 28


def build_dfg() -> DFG:
    nodes: list[Node] = []
    for i in range(16):
        nodes += [
            Node(f"ld0_{i}", "ld", imm=BANK_A0 * 32 + i, dst="r0", space="spad"),
            Node(f"ld1_{i}", "ld", imm=BANK_A1 * 32 + i, dst="r1", space="spad"),
            Node(f"add_{i}", "add", (f"ld0_{i}", f"ld1_{i}"), dst="r2"),
            Node(f"st_{i}", "st", (f"add_{i}",), imm=BANK_SUM * 32 + i, space="spad"),
        ]
    return DFG("add2", tuple(nodes))


def parse_words(path: Path) -> list[int]:
    return [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{8})u", path.read_text())]


def main() -> int:
    ref_path = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        ROOT.parent / "GPCP" / "sw" / "soc" / "gen" / "add2_prog.h")
    model, emitter = load_arch("gpcp_4x4", ROOT / "arch")

    dfg = build_dfg()
    sched = ListScheduler(order="topo", bind="decl").schedule(dfg, model)
    vs = check(sched, dfg, model)
    if vs:
        print("调度不合法：")
        for v in vs:
            print(" ", v)
        return 2

    header = emitter.emit(sched, dfg, model)
    got = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{8})u", header)]
    ref = parse_words(ref_path)

    print(f"cgracc: {len(got)} words / {sched.cycles()} cycles")
    print(f"ref   : {len(ref)} words ({ref_path})")
    if len(got) != len(ref):
        print(f"长度不一致：{len(got)} != {len(ref)}")
        return 1

    diff = [(i // 16, i % 16, r, g) for i, (r, g) in enumerate(zip(ref, got)) if r != g]
    if diff:
        print(f"逐字不一致 {len(diff)}/{len(ref)} 处：")
        for cyc, pe, r, g in diff[:16]:
            print(f"  c{cyc} pe{pe}: ref=0x{r:08x} got=0x{g:08x}")
        return 1

    print(f"逐字一致：{len(ref)}/{len(ref)} words，0 差异")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
