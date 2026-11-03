#!/usr/bin/env python3
"""Check the PDSCH LDPC iteration loop (sw/soc/pdsch_bm.c).

Mirrors the benchmark exactly: same LCG seed / channel LLRs, same graph, same
integer min-sum arithmetic (including the "an edge that produced min1 gets
min2" rule and the -1/0 sign convention), 8 iterations.

Graph:
    variables v0..v7 (degree 2), checks c0..c3 (degree 4)
    c0: 0 1 2 3    c1: 4 5 6 7    c2: 0 1 6 7    c3: 2 3 4 5
"""
import sys

NVAR, NCHK, DEG = 8, 4, 4
ITERS = 8
LCG_SEED = 1

CHK_VAR = [[0, 1, 2, 3], [4, 5, 6, 7], [0, 1, 6, 7], [2, 3, 4, 5]]
# variable -> [(check, edge), (check, edge)]
VAR_EDGE = [[[(0, 0), (2, 0)], [(0, 1), (2, 1)], [(0, 2), (3, 0)], [(0, 3), (3, 1)],
              [(1, 0), (3, 2)], [(1, 1), (3, 3)], [(1, 2), (2, 2)], [(1, 3), (2, 3)]][v]
             for v in range(NVAR)]


def channel_llrs():
    s, out = LCG_SEED, []
    for _ in range(NVAR):
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        out.append(((s >> 16) & 0x3FF) - 512)
    return out


def model():
    ch = channel_llrs()
    msg = [[ch[CHK_VAR[c][k]] for k in range(DEG)] for c in range(NCHK)]
    hard = [0] * NVAR
    for _ in range(ITERS):
        # ---- check nodes (same sequence as the microcode) -------
        cmsg = [[0] * DEG for _ in range(NCHK)]
        for c in range(NCHK):
            a = msg[c]
            ab = [abs(x) for x in a]
            s1 = min(ab[0], ab[1]); h1 = max(ab[0], ab[1])
            s2 = min(ab[2], ab[3]); h2 = max(ab[2], ab[3])
            min1 = min(s1, s2)
            min2 = min(max(s1, s2), min(h1, h2))
            sign = 1 if (a[0] < 0) ^ (a[1] < 0) ^ (a[2] < 0) ^ (a[3] < 0) else 0
            for k in range(DEG):
                out_mag = min2 if ab[k] == min1 else min1
                neg = (a[k] < 0) ^ bool(sign)
                cmsg[c][k] = -out_mag if neg else out_mag
        # ---- variable nodes -------------------------------------
        for v in range(NVAR):
            m0 = cmsg[VAR_EDGE[v][0][0]][VAR_EDGE[v][0][1]]
            m1 = cmsg[VAR_EDGE[v][1][0]][VAR_EDGE[v][1][1]]
            tot = ch[v] + m0 + m1
            hard[v] = -1 if tot < 0 else 0
            msg[VAR_EDGE[v][0][0]][VAR_EDGE[v][0][1]] = tot - m0
            msg[VAR_EDGE[v][1][0]][VAR_EDGE[v][1][1]] = tot - m1
    return hard


def main(path):
    vals = []
    for line in open(path):
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "word" and parts[2].startswith("0x"):
            # the harness prints one 64-bit word per line: two 32-bit results
            w = int(parts[2], 16)
            vals.append(w & 0xFFFFFFFF)
            vals.append((w >> 32) & 0xFFFFFFFF)
    if len(vals) < 14:
        print(f"[FAIL] short dump ({len(vals)} words)")
        return 1

    want = model()
    got = [(-1 if (vals[v] & 0xFFFFFFFF) == 0xFFFFFFFF else 0) for v in range(NVAR)]
    bad = [v for v in range(NVAR) if got[v] != want[v]]

    print(f"[info] iterations {vals[9]}, array runs {vals[10]}, "
          f"cycles {vals[11]}, errors {vals[12]}")
    chan_signs = [-1 if x < 0 else 0 for x in channel_llrs()]
    flipped = [v for v in range(NVAR) if got[v] != chan_signs[v]]
    print(f"[info] decisions changed by the iterations: {flipped} "
          f"(raw channel signs {chan_signs})")
    print(f"[info] hard decisions: got {got}")
    print(f"[info] hard decisions: want {want}")
    if bad or vals[12] != 0 or vals[8] != 0xCAFEBABE:
        print(f"[FAIL] PDSCH LDPC loop: mismatches at variables {bad}, "
              f"marker 0x{vals[8]:08x}, errors {vals[12]}")
        return 1
    print(f"[ OK ] PDSCH LDPC iteration loop (CN 50 + BN 12 cycles x {ITERS}): "
          f"hard decisions match the golden model, {vals[11]} cycles")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "pdsch_dump.txt"))
