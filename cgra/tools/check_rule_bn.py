#!/usr/bin/env python3
"""硬件级验收：规则映射器生成的 BN 微码在 Verilator 台架上跑，逐值对账。

流程：
  1. `cgra/tools/rule_map.py --bn` 生成微码（每 PE 一条串行指令流，16 个实例）；
  2. 生成激励（24 槽 × 16 行）与黄金结果（v = llr + Σmsg、out = v − msg、hard）；
  3. 调 `sim/cgra/build/Vcgra_axi_lite` 跑 118 拍，dump 26 个 bank；
  4. 逐值比较。

    python3 cgra/tools/check_rule_bn.py [--tb PATH] [--keep]
"""
import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import rule_map as rm                      # noqa: E402

ROWS, NMSG = 16, 23
BANKS = 26


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tb", default=os.path.join(ROOT, "sim/cgra/build/Vcgra_axi_lite"))
    ap.add_argument("--out", default=os.path.join(ROOT, "build/rule_bn"))
    ap.add_argument("--split", action="store_true",
                    help="用 link 版（4 PE/行）的调度，而不是 v0 的单 PE/行")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.tb):
        print(f"missing bench {a.tb}（先 `make -C sim/cgra`）", file=sys.stderr)
        return 2

    if a.split:
        rows = 4
        mc = rm.schedule_bn_split(rows=rows)
    else:
        rows = ROWS
        dfg = rm.build_bn_dfg()
        mc, _lv, _sink, _banks = rm.schedule(dfg, ROWS)
    spad, exp = rm.bn_case(rows=rows, nmsg=NMSG)

    os.makedirs(a.out, exist_ok=True)
    prog = os.path.join(a.out, "bn.prog.hex")
    pre = os.path.join(a.out, "bn.preload.hex")
    dump = os.path.join(a.out, "bn.dump.txt")
    with open(prog, "w") as f:
        for w in mc.build():
            f.write(f"0x{w:08x}\n")
    with open(pre, "w") as f:
        for idx, v in sorted(spad.items()):
            f.write(f"0x{rm.SPAD_WIN + idx * 4:04x} 0x{v:08x}\n")

    cmd = [a.tb, "--prog", prog, "--cycles", str(mc.cycles), "--preload", pre,
           "--spad-dump", hex(rm.SPAD_WIN), str(BANKS * 32), "--dump", dump]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"bench failed ({r.returncode})\n{r.stdout}\n{r.stderr}", file=sys.stderr)
        return 1
    got = {}
    for line in open(dump):
        m = re.match(r"spad ([0-9a-f]+) 0x([0-9a-f]+)", line)
        if m:
            got[int(m.group(1), 16)] = int(m.group(2), 16)

    errs = []
    for idx, want in exp.items():
        addr = rm.SPAD_WIN + idx * 4
        if got.get(addr) != want:
            errs.append((idx, got.get(addr), want))
    print(f"微码 {mc.cycles} 拍；台架跑完 {len(got)} 个 spad 字；"
          f"对账 {len(exp)} 个值，差异 {len(errs)} 个")
    for idx, g, w in errs[:8]:
        print(f"  idx={idx} (bank {idx // 32} word {idx % 32}) "
              f"got={g:#010x} want={w:#010x}" if g is not None else
              f"  idx={idx} 缺值，want={w:#010x}")
    if not a.keep and not errs:
        for p in (prog, pre, dump):
            os.remove(p)
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
