#!/usr/bin/env python3
"""Check the SoC-level Polar SC decoder (sw/soc/polar_sc_bm.c).

Mirrors the program: same frozen rule (bitrev6(i) < N-K), same LCG for the
information bits and for the channel noise, min-sum f/g, and the same tree
order.  The decoded word is compared bit-for-bit, and the decode is also
required to recover the transmitted information bits.
"""
import sys

N = 64
K = 32
DEPTH = 6
AMP = 3000


class Lcg:
    def __init__(self, seed=0x5EED0042):
        self.s = seed

    def step(self):
        self.s = (self.s * 1103515245 + 12345) & 0xFFFFFFFF
        return self.s


def bitrev6(v):
    r = 0
    for b in range(6):
        r |= ((v >> b) & 1) << (5 - b)
    return r


def is_frozen(i):
    return bitrev6(i % N) < (N - K)


def polar_encode(u):
    x = list(u)
    step = 1
    while step < N:
        for i in range(0, N, 2 * step):
            for k in range(step):
                x[i + k] ^= x[i + k + step]
        step *= 2
    return x


def f_node(a, b):
    m = min(abs(a), abs(b))
    s = -m if ((a < 0) != (b < 0)) else m
    return wrap32(s)


def g_node(a, b, beta):
    return wrap32(b + (1 - 2 * (beta & 1)) * a)


def wrap32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def sc_decode(llr):
    lbuf = [[0] * N for _ in range(DEPTH + 1)]
    bbuf = [[0] * N for _ in range(DEPTH + 1)]
    lbuf[0] = list(llr)

    def rec(d, o, size):
        if size == 1:
            bbuf[d][o] = 0 if is_frozen(o) else (1 if lbuf[d][o] < 0 else 0)
            return
        half = size // 2
        for i in range(half):
            lbuf[d + 1][o + i] = f_node(lbuf[d][o + i], lbuf[d][o + half + i])
        rec(d + 1, o, half)
        for i in range(half):
            lbuf[d + 1][o + half + i] = g_node(lbuf[d][o + i],
                                               lbuf[d][o + half + i],
                                               bbuf[d + 1][o + i])
        rec(d + 1, o + half, half)
        for i in range(half):
            bbuf[d][o + i] = bbuf[d + 1][o + i] ^ bbuf[d + 1][o + half + i]
            bbuf[d][o + half + i] = bbuf[d + 1][o + half + i]

    rec(0, 0, N)
    return bbuf[DEPTH]          # leaf partial sums = decoded information bits


def main(path):
    vals = []
    for line in open(path):
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "word" and parts[2].startswith("0x"):
            w64 = int(parts[2], 16)
            vals.append(w64 & 0xFFFFFFFF)
            vals.append((w64 >> 32) & 0xFFFFFFFF)
    if len(vals) < 135:
        print(f"  dump too short: {len(vals)} words")
        return 1

    # information bits exactly as the program builds them
    g = Lcg()
    u = []
    for i in range(N):
        self_s = g.step()
        u.append(0 if is_frozen(i) else (self_s >> 16) & 1)
    x = polar_encode(u)

    gn = Lcg(seed=0x5EED0042)          # noise restarts from the same seed state
    noise_seq = []
    # the program draws the info bits from the same LCG stream, so rebuild the
    # state after those 64 draws before the channel noise
    for _ in range(N):
        gn.step()
    for _ in range(N):
        s = gn.step()
        noise_seq.append(((s >> 16) & 0x3FF) - 512)

    llr = [(AMP if x[i] == 0 else -AMP) + noise_seq[i] for i in range(N)]
    uhat_ref = sc_decode(llr)

    ok = True
    uhat_got = vals[6:6 + N]
    u_got = vals[70:70 + N]
    if u_got != u:
        print("  transmitted bits differ from the model")
        ok = False
    if uhat_got != uhat_ref:
        for i in range(N):
            if uhat_got[i] != uhat_ref[i]:
                print(f"  bit {i}: got {uhat_got[i]} expected {uhat_ref[i]}")
                ok = False
                break
    mism = sum(1 for i in range(N) if uhat_got[i] != u[i])
    if vals[5] != mism:
        print(f"  mismatch count: got {vals[5]} expected {mism}")
        ok = False
    if vals[4] != 0:
        print(f"  errors reported by the program: {vals[4]}")
        ok = False
    if vals[134] != 0x600D:
        print(f"  epilogue missing: 0x{vals[134]:08x}")
        ok = False

    info_ok = all(uhat_got[i] == u[i] for i in range(N) if not is_frozen(i))
    if ok:
        print(f"[info] SC: N={vals[0]} K={vals[1]}, {vals[2]} node runs, "
              f"{vals[3]} cycles, {N - mism}/{N} bits equal the transmitted word")
        if info_ok:
            print(f"[ OK ] Polar SC decode on the CGRA: tree walk bit-exact, "
                  f"all {K} information bits recovered")
        else:
            print("[FAIL] decode did not recover every information bit")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "sim/soc/build/polar_dump.txt"))
