#!/usr/bin/env python3
"""Debug check: reconstructed messages and BN gather for variable block 0."""
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import ldpc5g as L

ZC, MAXD, SEED, SENT = 20, 10, 7, 32767
NROWS, NCOLS, TARGET = 42, 52, 0


def build():
    s, llr = SEED, []
    for _ in range(NCOLS * ZC):
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        llr.append(((s >> 16) & 0x3FF) - 512)
    edges = [[(c, L.BG2_A5[r][c]) for c in range(NCOLS) if L.BG2_A5[r][c] >= 0]
             for r in range(NROWS)]
    msg = [[[0] * ZC for _ in range(MAXD)] for _ in range(NROWS)]
    for r in range(NROWS):
        for k in range(MAXD):
            if k < len(edges[r]):
                c, sh = edges[r][k]
                msg[r][k] = [llr[c * ZC + (i + sh) % ZC] for i in range(ZC)]
            else:
                msg[r][k] = [SENT] * ZC
    return llr, edges, msg


def cn_pass(msg, edges):
    m1 = [[0] * ZC for _ in range(NROWS)]
    m2 = [[0] * ZC for _ in range(NROWS)]
    sg = [[0] * ZC for _ in range(NROWS)]
    for r in range(NROWS):
        for i in range(ZC):
            col = [msg[r][k][i] for k in range(MAXD)]
            ab = sorted(abs(x) for x in col)
            m1[r][i], m2[r][i] = ab[0], ab[1]
            s = 0
            for x in col:
                s ^= 1 if x < 0 else 0
            sg[r][i] = -1 if s else 0
    return m1, m2, sg


def reconstruct(msg, edges, m1, m2, sg):
    for r in range(NROWS):
        for k in range(len(edges[r])):
            for i in range(ZC):
                a = msg[r][k][i]
                mag = m2[r][i] if abs(a) == m1[r][i] else m1[r][i]
                neg = (a < 0) ^ (sg[r][i] != 0)
                msg[r][k][i] = -mag if neg else mag


def main(path):
    words = []
    for line in open(path):
        p = line.split()
        if len(p) >= 3 and p[0] == "word" and p[2].startswith("0x"):
            w = int(p[2], 16)
            words.append(w & 0xFFFFFFFF)
            words.append((w >> 32) & 0xFFFFFFFF)
    llr, edges, msg = build()
    m1, m2, sg = cn_pass(msg, edges)
    reconstruct(msg, edges, m1, m2, sg)

    mine = [(r, k, edges[r][k][1]) for r in range(NROWS)
            for k in range(len(edges[r])) if edges[r][k][0] == TARGET]
    bad = 0
    for n, (r, k, sh) in enumerate(mine):
        for i in range(ZC):
            got = words[8 + n * ZC + i]
            exp = msg[r][k][i] & 0xFFFFFFFF
            if got != exp:
                bad += 1
                if bad <= 4:
                    print(f"[diff] edge {n} (r={r},k={k}) row {i}: "
                          f"got {got if got < 2**31 else got - 2**32} "
                          f"want {msg[r][k][i]}")
    print(f"[info] block {TARGET}: {len(mine)} edges, hardware edges {words[1]}, "
          f"runs {words[2]}, DMA {words[3]}, errors {words[5]}")
    if bad:
        print(f"[FAIL] reconstructed messages differ in {bad} values")
        return 1
    print("[ OK ] reconstructed per-edge messages match the model")

    # which gather convention does the hardware buffer correspond to?
    for name, fn in (("(i-sh)", lambda sh, i: (i - sh) % ZC),
                     ("(i+sh)", lambda sh, i: (i + sh) % ZC),
                     ("identity", lambda sh, i: i)):
        diff = 0
        for n, (r, k, sh) in enumerate(mine):
            for i in range(ZC):
                got = words[500 + n * ZC + i]
                if got != (msg[r][k][fn(sh, i)] & 0xFFFFFFFF):
                    diff += 1
        print(f"[info] gather convention {name}: {diff} differing values")
    # ---- BN output, signed 32-bit, separate hardware buffers ----
    # input dump at word 500, output dump at word 1020 (no aliasing)
    def sx(v):
        return v - 0x100000000 if v >= 0x80000000 else v
    n = len(mine)
    slots = [[msg[r][k][(i - sh) % ZC] for i in range(ZC)] for (r, k, sh) in mine]
    bad_bn = 0
    for i in range(ZC):
        v = llr[TARGET * ZC + i] + sum(slots[k][i] for k in range(n))
        exp_hard = -1 if v < 0 else 0
        if sx(words[1020 + 25 * ZC + i]) != exp_hard:
            bad_bn += 1
            if bad_bn <= 3:
                print(f"[diff] hard row {i}: got {sx(words[1020 + 25 * ZC + i])} "
                      f"want {exp_hard}")
        for k in range(n):
            exp = v - slots[k][i]
            got = sx(words[1020 + k * ZC + i])
            if got != exp:
                bad_bn += 1
                if bad_bn <= 3:
                    print(f"[diff] BN slot {k} row {i}: got {got} want {exp}")
    print(f"[info] BN output vs model: {bad_bn} differing values")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "pdsch_gather_dump.txt"))
