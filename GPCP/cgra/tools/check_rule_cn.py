#!/usr/bin/env python3
"""硬件级验收：规则映射器的 CN 微码（min1/min2/sign）在 Verilator 台架上跑。

    python3 cgra/tools/check_rule_cn.py [--tb PATH] [--keep]
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

BANKS = 31       # 0..30（30 是 sentinel）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tb", default=os.path.join(ROOT, "sim/cgra/build/Vcgra_axi_lite"))
    ap.add_argument("--out", default=os.path.join(ROOT, "build/rule_cn"))
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.tb):
        print(f"missing bench {a.tb}（先 `make -C sim/cgra`）", file=sys.stderr)
        return 2

    mc = rm.schedule_cn()
    spad, exp = rm.cn_case()
    prog, pre = rm.write_hex(os.path.join(a.out, "cn"), mc, spad)
    dump = os.path.join(a.out, "cn.dump.txt")

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
          f"对账 {len(exp)} 个值（min1/min2/sign × 20 行），差异 {len(errs)} 个")
    for idx, g, w in errs[:8]:
        print(f"  idx={idx} (bank {idx // 32} word {idx % 32}) got="
              f"{'缺失' if g is None else format(g, '#010x')} want={w:#010x}")
    if not a.keep and not errs:
        for p in (prog, pre, dump):
            os.remove(p)
    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
