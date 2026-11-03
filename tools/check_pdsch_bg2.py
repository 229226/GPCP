#!/usr/bin/env python3
"""Check the DMA-blocked real BG2 check-node pass (sw/soc/pdsch_bg2_bm.c).

Mirrors the benchmark: same LCG channel LLRs, same base graph (tools/ldpc5g.py),
same 10-input min-sum with the +32767 sentinel padding for edges >= degree,
same -1/0 sign accumulation.
"""
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import ldpc5g as L

ZC = 20
MAXD = 10
SENTINEL = 32767
LCG_SEED = 1
NROWS = 42


def channel_llrs():
    s, out = LCG_SEED, []
    for _ in range(52 * ZC):
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        out.append(((s >> 16) & 0x3FF) - 512)
    return out


def edges(r):
    return [(c, L.BG2_A5[r][c]) for c in range(52) if L.BG2_A5[r][c] >= 0]


def model():
    """Return {row: (min1[20], min2[20], sign[20])}."""
    llr = channel_llrs()
    out = {}
    for r in range(NROWS):
        e = edges(r)
        vals = []                      # 10 banks x ZC, sentinel padded
        for k in range(MAXD):
            if k < len(e):
                c, sh = e[k]
                vals.append([llr[c * ZC + (i + sh) % ZC] for i in range(ZC)])
            else:
                vals.append([SENTINEL] * ZC)
        m1, m2, sg = [], [], []
        for i in range(ZC):
            col = [vals[k][i] for k in range(MAXD)]
            ab = [abs(x) for x in col]
            srt = sorted(ab)
            m1.append(srt[0])
            m2.append(srt[1])
            sign = 0
            for x in col:
                sign ^= (1 if x < 0 else 0)
            sg.append(-1 if sign else 0)
        out[r] = (m1, m2, sg)
    return out


def main(path):
    vals = []
    for line in open(path):
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "word" and parts[2].startswith("0x"):
            w = int(parts[2], 16)
            vals.append(w & 0xFFFFFFFF)
            vals.append((w >> 32) & 0xFFFFFFFF)
    base = 8                                   # RES_BASE = result base + 8 words
    if len(vals) < base + NROWS * 3 * ZC:
        print(f"[FAIL] short dump ({len(vals)} words)")
        return 1
    want = model()
    bad = 0
    for r in range(NROWS):
        for j in range(3):
            for i in range(ZC):
                got = vals[base + (r * 3 + j) * ZC + i]
                exp = want[r][j][i] & 0xFFFFFFFF
                if got != exp:
                    bad += 1
                    if bad <= 3:
                        print(f"[diff] row {r} field {j} lifted row {i}: "
                              f"got 0x{got:08x} want 0x{exp:08x}")
    print(f"[info] check nodes {vals[1]}, array runs {vals[2]}, DMA {vals[3]}, "
          f"cycles {vals[4]}, errors {vals[5]}")
    if bad or vals[5] != 0 or vals[0] != 0xCAFEBABE:
        print(f"[FAIL] BG2 blocked check-node pass: {bad} mismatches")
        return 1
    print(f"[ OK ] real BG2 block (42 check nodes x 20 lifted rows), DMA-blocked: "
          f"min1/min2/sign match the golden model, {vals[4]} cycles, {vals[2]} array runs")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "pdsch_bg2_dump.txt"))
