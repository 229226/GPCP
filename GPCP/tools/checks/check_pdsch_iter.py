#!/usr/bin/env python3
# --- tools path bootstrap (subdirs import root modules) ---
import os as _os, sys as _sys
_T = _os.path.dirname(_os.path.abspath(__file__))
_TOOLS = _T if _os.path.basename(_T) == "tools" else _os.path.dirname(_T)
for _p in (_TOOLS, _os.path.join(_TOOLS, "models"), _os.path.join(_TOOLS, "checks"),
           _os.path.join(_os.path.dirname(_TOOLS), "cgra", "tools")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
_ROOT = _os.path.dirname(_TOOLS)
# --- end bootstrap ---
"""P1 判据：硬件跑完整 BG2 迭代（52 变量块 × ITERS）后的硬判决 vs 纯 min-sum 模型。

约定与硬件完全一致：
  - 初始 msg[r][k][i] = llr[col][(i+sh)%Zc]，不足 10 条边补 +32767 哨兵；
  - CN：min1/min2/sign（含哨兵），逐边重构 m' = sign(a)·((|a|==min1)?min2:min1)；
  - gather：gath[k][i] = msg[r][k][(i-sh)%Zc]，24 槽补零 + llr；
  - BN：v = llr + Σ gath，out_k = v − gath_k，hard = (v < 0)；
  - scatter：msg[r][k][i] = out_k[(i+sh)%Zc]。
"""
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import ldpc5g as L

ZC, MAXD, SENT, SEED = 20, 10, 32767, 7
NROWS, NCOLS = 42, 52
ITERS = 8      # 可用命令行 --iters N 覆盖


def edges_table():
    return [[(c, L.BG2_A5[r][c]) for c in range(NCOLS) if L.BG2_A5[r][c] >= 0]
            for r in range(NROWS)]


def build(edges):
    s, llr = SEED, []
    for _ in range(NCOLS * ZC):
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        llr.append(((s >> 16) & 0x3FF) - 512)
    msg = [[[0] * ZC for _ in range(MAXD)] for _ in range(NROWS)]
    for r in range(NROWS):
        for k in range(MAXD):
            if k < len(edges[r]):
                c, sh = edges[r][k]
                msg[r][k] = [llr[c * ZC + (i + sh) % ZC] for i in range(ZC)]
            else:
                msg[r][k] = [SENT] * ZC
    return llr, msg


def iterate(llr, msg, edges, iters=ITERS):
    hard = [[0] * ZC for _ in range(NCOLS)]
    for _ in range(iters):
        # CN + 逐边重构
        for r in range(NROWS):
            for i in range(ZC):
                col = [msg[r][k][i] for k in range(MAXD)]
                ab = sorted(abs(x) for x in col)
                m1, m2 = ab[0], ab[1]
                sgn = 0
                for x in col:
                    sgn ^= 1 if x < 0 else 0
                for k in range(len(edges[r])):
                    a = msg[r][k][i]
                    mag = m2 if abs(a) == m1 else m1
                    neg = (a < 0) ^ (sgn != 0)
                    msg[r][k][i] = -mag if neg else mag
        # 每个变量块：gather → BN → scatter
        for b in range(NCOLS):
            mine = [(r, k, edges[r][k][1]) for r in range(NROWS) for k in range(len(edges[r]))
                    if edges[r][k][0] == b]
            gath = [[msg[r][k][(i - sh) % ZC] for i in range(ZC)] for (r, k, sh) in mine]
            while len(gath) < 24:
                gath.append([0] * ZC)
            gath.append(llr[b * ZC:(b + 1) * ZC])
            out = [[0] * ZC for _ in range(24)]
            for i in range(ZC):
                v = llr[b * ZC + i] + sum(gath[k][i] for k in range(24))
                for k in range(24):
                    out[k][i] = v - gath[k][i]
                hard[b][i] = 1 if v < 0 else 0
            for n, (r, k, sh) in enumerate(mine):
                for i in range(ZC):
                    msg[r][k][i] = out[n][(i + sh) % ZC]
    return hard


def main(path, iters=ITERS):
    vals = []
    for line in open(path):
        p = line.split()
        if len(p) >= 3 and p[0] == "word" and p[2].startswith("0x"):
            w = int(p[2], 16)
            vals.append(w & 0xFFFFFFFF)
            vals.append((w >> 32) & 0xFFFFFFFF)
    base = 8
    if len(vals) < base + NCOLS * ZC:
        print(f"[FAIL] short dump ({len(vals)} words)")
        return 1
    edges = edges_table()
    llr, msg = build(edges)
    hard = iterate(llr, msg, edges, iters)
    bad = 0
    for b in range(NCOLS):
        for i in range(ZC):
            got = 1 if (vals[base + b * ZC + i] & 0xFFFFFFFF) == 0xFFFFFFFF else 0
            if got != hard[b][i]:
                bad += 1
                if bad <= 6:
                    print(f"[diff] block {b} lifted row {i}: got {got} want {hard[b][i]}")
    print(f"[info] blocks {vals[1]}, array runs {vals[2]}, DMA {vals[3]}, "
          f"cycles {vals[4]}, errors {vals[5]}")
    print(f"[info] 分项: CN {vals[1048]} / BN+搬运 {vals[1049]} / CPU 重构 {vals[1050]}")
    if bad or vals[5] != 0 or vals[0] != 0xCAFEBABE:
        print(f"[FAIL] BG2 iteration: {bad} of {NCOLS * ZC} hard decisions differ")
        return 1
    print(f"[ OK ] BG2 iteration ({iters} iters x {NROWS} CN + {NCOLS} BN): "
          f"all {NCOLS * ZC} hard decisions match the min-sum model")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    iters = ITERS
    for a in sys.argv[1:]:
        if a.startswith("--iters="):
            iters = int(a.split("=", 1)[1])
    sys.exit(main(args[0] if args else "pdsch_iter_dump.txt", iters))
