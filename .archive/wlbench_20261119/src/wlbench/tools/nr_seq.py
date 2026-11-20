#!/usr/bin/env python3
"""NR (5G) reference sequences + fixed-point gold models for the CGRA kernels.

Implemented here (self-consistent, following 38.211/38.212 formulas):

  PSS  38.211 7.4.2.2 : m-sequence x(i+7) = x(i+4) + x(i) mod 2,
                        init x(0..6) = [0,0,0,0,0,0,1],
                        d_PSS(n) = 1 - 2*x((n + 43*N_ID2) mod 127)
  SSS  38.211 7.4.2.3 : d_SSS(n) = [1-2x0((n+m0) mod 127)]*[1-2x1((n+m1) mod 127)]
                        m0 = 15*floor(N_ID1/112) + 5*N_ID2, m1 = N_ID1 mod 112
                        x0: x0(i+7) = x0(i+4) + x0(i), init [0..0,1]
                        x1: x1(i+7) = x1(i+1) + x1(i), init [0..0,1]
  Polar 38.212 5.3.1  : x = u * F^(tensor n) with F = [[1,0],[1,1]],
                        computed as the butterfly  x[i] ^= x[i+step]  for
                        step = 1,2,4,... N/2 (no extra permutation)

Bit-exact conventions used by the kernels:

  * BPSK symbols are Q15 "full scale": +1 -> +32767, -1 -> -32767
  * correlation is the raw integer sum of rx * ref (no normalisation)
  * polar LLR node: min-sum f and the g add/sub, int32 two's complement
"""


def _mseq(poly, init, n):
    """Generic 7-bit m-sequence: poly(i) selects the taps."""
    x = list(init) + [0] * (n - 7)
    for i in range(n - 7):
        x[i + 7] = 0
        for tap in poly:
            x[i + 7] ^= x[i + tap]
    return x


def pss_seq(nid2: int):
    """38.211 PSS: 127 BPSK symbols (+1/-1)."""
    assert 0 <= nid2 < 3
    x = _mseq((4, 0), [0, 0, 0, 0, 0, 0, 1], 127 + 127)
    return [1 - 2 * x[(n + 43 * nid2) % 127] for n in range(127)]


def sss_seq(nid1: int, nid2: int):
    """38.211 SSS: 127 BPSK symbols."""
    assert 0 <= nid1 < 336 and 0 <= nid2 < 3
    m0 = 15 * (nid1 // 112) + 5 * nid2
    m1 = nid1 % 112
    x0 = _mseq((4, 0), [0, 0, 0, 0, 0, 0, 1], 2 * 127)
    x1 = _mseq((1, 0), [0, 0, 0, 0, 0, 0, 1], 2 * 127)
    out = []
    for n in range(127):
        a = 1 - 2 * x0[(n + m0) % 127]
        b = 1 - 2 * x1[(n + m1) % 127]
        out.append(a * b)
    return out


def polar_transform(u, n: int):
    """x = u * F^(tensor log2 n), butterfly form (in place on a copy)."""
    x = [b & 1 for b in u[:n]] + [0] * max(0, n - len(u))
    step = 1
    while step < n:
        for i in range(0, n, 2 * step):
            for k in range(step):
                x[i + k] ^= x[i + k + step]
        step *= 2
    return x


def q15_bpsk(seq):
    return [32767 if s > 0 else -32767 for s in seq]


def corr_partials(rx_re, rx_im, ref, npes=16, taps_per_pe=8):
    """Per-PE partial correlations, mirroring kernel_bpsk_corr.

    PE p accumulates taps [p*taps_per_pe, (p+1)*taps_per_pe) with acc += rx_re*ref
    and a plain register accumulator for rx_im*ref.
    """
    ref15 = q15_bpsk(ref)
    out = []
    for p in range(npes):
        ar = 0
        ai = 0
        for k in range(taps_per_pe):
            t = p * taps_per_pe + k
            if t >= len(ref15):
                break
            ar += sx16(rx_re[t]) * sx16(ref15[t])
            ai += sx16(rx_im[t]) * sx16(ref15[t])
            ar = wrap32(ar)
            ai = wrap32(ai)
        out.append((ar, ai))
    return out


def wrap32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def f_node(a, b):
    """min-sum check update: sign(a)*sign(b)*min(|a|,|b|), int32 wrap."""
    sa = -1 if (a & 0x80000000) else 1
    sb = -1 if (b & 0x80000000) else 1
    m = min(abs(a), abs(b))
    return wrap32(sa * sb * m)


def g_node(a, b, beta):
    """b + (1-2*beta)*a."""
    return wrap32(b + (1 - 2 * (beta & 1)) * a)
