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
"""FFT planning model for the GPCP CGRA (radix-4 / radix-16, Q15, tiled).

The CGRA page holds 1024 scratchpad words and one array run is capped at 512
cycles, so a 4096-point complex FFT is tiled: the array works on 64 radix-4
butterflies (256 points) while the copy engine fills/drains the other page.

Stage decomposition (Gentleman-Sande DIF, in place, scaled by 1/4):

    stage L : M = L/4, butterfly beta = block*M + t
              v_j = x[B + j*M + t]          j = 0..3
              U_m = sum_j v_j * W4^(j*m)
              x[B + m*M + t] = U_m * W_L^(m*t) >> 17     (m > 0)
              x[B + 0*M + t] = U_0 >> 2                  (m = 0)

The last three stages (L = 64, 16, 4) all live inside one 64-point block, so
they are fused into a single "block" microcode image (radix-16 = radix-4 x
radix-4, then radix-4): one array run per block, twiddles are stage constants
(preloaded, not streamed).  The remaining stages (L >= 256, M >= 64) share one
"stage" image.

Scratchpad layout (identical for every stage image, so one image suffices):

    data      word (bi*4 + j)*2 + h        bi = 0..63 butterfly, j = 0..3,
                                           h = 0/1 real/imaginary (banks 0..15)
    twiddles  word 512 + (m-1)*128 + bi*2 + h   (banks 16..27)

RAM layout is the natural index order g = 0..N-1 with two words per point, and
every stage is in place, so the array stays in g-order; the result is in
base-4 digit-reversed order (position i holds X[rev4(i)]).

This module is the common reference for the microcode (sw/soc/bench/fft_bm.c), the
twiddle ROM image (tools/fft_plan.py lut) and the checker (tools/checks/check_fft.py).
"""

import math
import struct
import sys
from pathlib import Path

FULL_MIN_L = 256           # stages with L >= 256 use the tiled "stage" image
TILE_BFLIES = 64           # butterflies per array run
DATA_WORDS = 512           # 64 butterflies x 4 points x 2 halves
TW_BASE = 512              # first twiddle word (banks 16..27)
TW_WORDS = 384             # 3 m's x 64 butterflies x 2 halves

TAIL_WORDS = 120           # 48 W_64 pairs + 12 W_16 pairs
TAIL_BLOCK = 64            # points per fused tail run
TAIL_BLOCK_WORDS = 128


# ---------------------------------------------------------------- ring layout
def n_stages(n: int) -> int:
    """log4(n)."""
    k = 0
    while (1 << (2 * k)) < n:
        k += 1
    return k


def stage_L(n: int, s: int) -> int:
    return n >> (2 * s)


def full_stage_count(n: int) -> int:
    c = 0
    while stage_L(n, c) >= FULL_MIN_L:
        c += 1
    return c


def tile_count(n: int) -> int:
    return n // 256


def block_count(n: int) -> int:
    return n // TAIL_BLOCK


# LUT layout: for every size, the streamed twiddle blocks of its full stages,
# then one shared tail constant block.
LUT_OFFSET = {}
_w = 0
for _n in (256, 1024, 4096):
    LUT_OFFSET[_n] = _w
    _w += full_stage_count(_n) * tile_count(_n) * TW_WORDS
LUT_TAIL = _w
_w += TAIL_WORDS
LUT_WORDS = _w


# ------------------------------------------------------------------ helpers
def wrap32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def sx16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def mul16(a: int, b: int) -> int:
    """OP_MUL: sext16 of both operands, 32-bit product (registers wrap)."""
    return wrap32(sx16(a) * sx16(b))


def q15(v: float) -> int:
    q = int(round(v * 32768.0))
    return 32767 if q >= 32768 else (-32768 if q < -32768 else q)


def twiddle(k: int, L: int):
    """W_L^k = exp(-2*pi*i*k/L) in Q15 (LUT and golden model share this)."""
    k %= L
    ang = -2.0 * math.pi * k / L
    return q15(math.cos(ang)), q15(math.sin(ang))


# ------------------------------------------------------------- tiling model
def tile_beta(L: int, q: int, bi: int) -> int:
    beta = q * TILE_BFLIES + bi
    return beta


def butterfly_site(L: int, q: int, bi: int, j: int) -> int:
    """Global point index g of sub-block j of butterfly bi in tile q."""
    M = L // 4
    beta = tile_beta(L, q, bi)
    blk = beta // M
    t = beta % M
    return blk * L + j * M + t


def tile_transfers(n: int, L: int, q: int):
    """Copy-engine transfers of one tile (words, not bytes).

    ``(ram_word, spad_word, words, row, stride)``; the same list is used for
    the input (RAM -> spad) and the output (spad -> RAM) direction.

    M >= 64 always holds here: the four sub-blocks j are four runs of 64
    consecutive points in g-order, 128 words each.  Each chunk is one linear
    copy into its own 128-word scratchpad region (banks 4j..4j+3), which keeps
    the microcode address map trivial: word = 128*j + 2*bi + h.
    """
    M = L // 4
    assert M >= 64
    out = []
    for j in range(4):
        g0 = butterfly_site(L, q, 0, j)
        out.append((2 * g0, j * 128, 128, 0, 0))
    return out


def twiddle_words(n: int, L: int, q: int):
    """The 384-word twiddle block of one tile, in scratchpad layout."""
    w = [0] * TW_WORDS
    for bi in range(TILE_BFLIES):
        beta = tile_beta(L, q, bi)
        t = beta % (L // 4)
        for m in (1, 2, 3):
            wr, wi = twiddle(m * t, L)
            w[(m - 1) * 128 + bi * 2 + 0] = wr & 0xFFFF
            w[(m - 1) * 128 + bi * 2 + 1] = wi & 0xFFFF
    return w


def tail_twiddle_words():
    """Stage-constant twiddles of the fused 64-point block kernel.

    words   0..95 : W_64^(m*t), m = 1..3, t = 0..15  at (m-1)*32 + t*2 + h
    words  96..119: W_16^(m*t), m = 1..3, t = 0..3   at  96 + (m-1)*8 + t*2 + h
    """
    w = [0] * TAIL_WORDS
    for m in (1, 2, 3):
        for t in range(16):
            wr, wi = twiddle(m * t, 64)
            w[(m - 1) * 32 + t * 2 + 0] = wr & 0xFFFF
            w[(m - 1) * 32 + t * 2 + 1] = wi & 0xFFFF
        for t in range(4):
            wr, wi = twiddle(m * t, 16)
            w[96 + (m - 1) * 8 + t * 2 + 0] = wr & 0xFFFF
            w[96 + (m - 1) * 8 + t * 2 + 1] = wi & 0xFFFF
    return w


def lut_stage_word(n: int, s: int, q: int, idx: int) -> int:
    """RAM word index of one word of a streamed twiddle block."""
    return LUT_OFFSET[n] + (s * tile_count(n) + q) * TW_WORDS + idx


# --------------------------------------------------------------- the FFT math
def dft4(vr, vi, m):
    ar = ai = 0
    for j in range(4):
        f = (j * m) % 4
        if f == 0:
            ar += vr[j]; ai += vi[j]
        elif f == 1:      # -i * v = ( v_i, -v_r)
            ar += vi[j]; ai -= vr[j]
        elif f == 2:
            ar -= vr[j]; ai -= vi[j]
        else:             # +i * v = (-v_i,  v_r)
            ar -= vi[j]; ai += vr[j]
    return wrap32(ar), wrap32(ai)


def twiddled(ar, ai, wr, wi):
    tr = mul16(ar, wr) - mul16(ai, wi)
    ti = mul16(ar, wi) + mul16(ai, wr)
    return wrap32(tr) >> 17, wrap32(ti) >> 17


def reference_fft(re, im, n: int):
    """In-place radix-4 DIF over the whole array, exactly as the microcode."""
    re = [wrap32(sx16(v)) for v in re]
    im = [wrap32(sx16(v)) for v in im]
    L = n
    while L >= 4:
        M = L // 4
        for B in range(0, n, L):
            for t in range(M):
                vr = [re[B + j * M + t] for j in range(4)]
                vi = [im[B + j * M + t] for j in range(4)]
                for m in range(4):
                    ar, ai = dft4(vr, vi, m)
                    # the fused tail uses the shift-only path for L = 4 (every
                    # twiddle there is W_4^0 = 1, so the multiply is dropped)
                    if m == 0 or L == 4:
                        ar, ai = ar >> 2, ai >> 2
                    else:
                        wr, wi = twiddle(m * t, L)
                        ar, ai = twiddled(ar, ai, wr, wi)
                    re[B + m * M + t] = ar
                    im[B + m * M + t] = ai
        L = M
    return re, im


def walk(dst0: int, words: int, row: int, stride: int):
    """Emulate the copy engine's 2D scratchpad walk for one transfer."""
    out = []
    cur, cnt = dst0, 0
    for _ in range(words):
        out.append(cur)
        if row == 0:
            cur, cnt = cur + 1, 0
        elif cnt == row - 1:
            cur, cnt = cur + stride - row + 1, 0
        else:
            cur, cnt = cur + 1, cnt + 1
    return out


def stage_on_tiles(re, im, n: int, s: int):
    """One full stage (L >= 256), tile by tile through the scratchpad model."""
    L = stage_L(n, s)
    for q in range(tile_count(n)):
        sp = [0] * 1024
        for (ram_w, sp_w, words, row, stride) in tile_transfers(n, L, q):
            for i, dst in enumerate(walk(sp_w, words, row, stride)):
                g = (ram_w + i) // 2
                w = (ram_w + i) % 2
                sp[dst] = (re[g] if w == 0 else im[g]) & 0xFFFFFFFF
        tw = twiddle_words(n, L, q)
        sp[TW_BASE:TW_BASE + TW_WORDS] = tw
        for bi in range(TILE_BFLIES):
            vr = [wrap32(sp[j * 128 + 2 * bi]) for j in range(4)]
            vi = [wrap32(sp[j * 128 + 2 * bi + 1]) for j in range(4)]
            for m in range(4):
                ar, ai = dft4(vr, vi, m)
                if m == 0:
                    ar, ai = ar >> 2, ai >> 2
                else:
                    ar, ai = twiddled(ar, ai,
                                      wrap32(sp[TW_BASE + (m - 1) * 128 + bi * 2]),
                                      wrap32(sp[TW_BASE + (m - 1) * 128 + bi * 2 + 1]))
                sp[m * 128 + 2 * bi] = ar & 0xFFFFFFFF
                sp[m * 128 + 2 * bi + 1] = ai & 0xFFFFFFFF
        for (ram_w, sp_w, words, row, stride) in tile_transfers(n, L, q):
            for i, src in enumerate(walk(sp_w, words, row, stride)):
                g = (ram_w + i) // 2
                w = (ram_w + i) % 2
                v = wrap32(sp[src])
                if w == 0:
                    re[g] = v
                else:
                    im[g] = v
    return re, im


def tail_on_block(re, im, base: int):
    """The fused last three stages of one 64-point block (3 passes)."""
    sp = [0] * 1024
    for i in range(TAIL_BLOCK):
        sp[2 * i] = re[base + i] & 0xFFFFFFFF
        sp[2 * i + 1] = im[base + i] & 0xFFFFFFFF
    tw = tail_twiddle_words()
    sp[TW_BASE:TW_BASE + TAIL_WORDS] = tw

    # (L, M, sub-block count, twiddle base, twiddle m-stride)
    passes = ((64, 16, 1, 0, 32), (16, 4, 4, 96, 8), (4, 1, 16, None, 0))
    for (L, M, nsub, tw_base, tw_stride) in passes:
        for sb in range(nsub):
            for t in range(M):
                site = sb * L + t                    # points of this butterfly
                vr = [wrap32(sp[(site + j * M) * 2]) for j in range(4)]
                vi = [wrap32(sp[(site + j * M) * 2 + 1]) for j in range(4)]
                for m in range(4):
                    ar, ai = dft4(vr, vi, m)
                    if m == 0 or tw_base is None:
                        ar, ai = ar >> 2, ai >> 2
                    else:
                        idx = TW_BASE + tw_base + (m - 1) * tw_stride + t * 2
                        ar, ai = twiddled(ar, ai, wrap32(sp[idx]), wrap32(sp[idx + 1]))
                    sp[(site + m * M) * 2] = ar & 0xFFFFFFFF
                    sp[(site + m * M) * 2 + 1] = ai & 0xFFFFFFFF
    for i in range(TAIL_BLOCK):
        re[base + i] = wrap32(sp[2 * i])
        im[base + i] = wrap32(sp[2 * i + 1])
    return re, im


def planned_fft(re, im, n: int):
    for s in range(full_stage_count(n)):
        re, im = stage_on_tiles(re, im, n, s)
    for b in range(block_count(n)):
        re, im = tail_on_block(re, im, b * TAIL_BLOCK)
    return re, im


def digit_reversed(n: int) -> list:
    """Position i holds X[rev4(i)]."""
    k = n_stages(n)
    out = []
    for i in range(n):
        v, r = i, 0
        for _ in range(k):
            r = r * 4 + (v & 3)
            v >>= 2
        out.append(r)
    return out


def float_fft(xr, xi, n: int):
    """Same schedule in floating point (independent precision reference)."""
    re = [float(sx16(v)) for v in xr]
    im = [float(sx16(v)) for v in xi]
    L = n
    while L >= 4:
        M = L // 4
        for B in range(0, n, L):
            for t in range(M):
                vr = [re[B + j * M + t] for j in range(4)]
                vi = [im[B + j * M + t] for j in range(4)]
                for m in range(4):
                    ar, ai = dft4f(vr, vi, m)
                    if m == 0 or L == 4:
                        ar, ai = ar / 4, ai / 4
                    else:
                        wr, wi = twiddle(m * t, L)
                        wr, wi = wr / 32768.0, wi / 32768.0
                        ar, ai = (ar * wr - ai * wi) / 4, (ar * wi + ai * wr) / 4
                    re[B + m * M + t] = ar
                    im[B + m * M + t] = ai
        L = M
    return re, im


def dft4f(vr, vi, m):
    ar = ai = 0.0
    for j in range(4):
        f = (j * m) % 4
        if f == 0:
            ar += vr[j]; ai += vi[j]
        elif f == 1:
            ar += vi[j]; ai -= vr[j]
        elif f == 2:
            ar -= vr[j]; ai -= vi[j]
        else:
            ar -= vi[j]; ai += vr[j]
    return ar, ai


# ------------------------------------------------------------------ self-test
def self_test(n: int, seed: int = 5) -> bool:
    rnd = __import__("random").Random(seed + n)
    xr = [rnd.randint(-3000, 3000) for _ in range(n)]
    xi = [rnd.randint(-3000, 3000) for _ in range(n)]
    gr, gi = reference_fft(xr, xi, n)
    pr, pi = planned_fft(list(xr), list(xi), n)   # the plan works in place
    bad = 0
    for i in range(n):
        if gr[i] != pr[i] or gi[i] != pi[i]:
            if bad < 5:
                print(f"  N={n} g={i}: ref {gr[i]},{gi[i]} plan {pr[i]},{pi[i]}")
            bad += 1
    fr, fi = float_fft(xr, xi, n)
    err = max(max(abs(fr[i] - gr[i]), abs(fi[i] - gi[i])) for i in range(n))
    rms = math.sqrt(sum((fr[i] - gr[i]) ** 2 + (fi[i] - gi[i]) ** 2
                        for i in range(n)) / n)
    mag = math.sqrt(sum(fr[i] ** 2 + fi[i] ** 2 for i in range(n)) / n)
    snr = 20.0 * math.log10(mag / max(rms, 1e-9))
    print(f"[info] N={n}: {n - bad}/{n} points match the tiled plan, "
          f"max |float - fixed| = {err:.1f}, SNR {snr:.1f} dB "
          f"(full stages {full_stage_count(n)}, tiles {tile_count(n)}, "
          f"blocks {block_count(n)})")
    return bad == 0


SEEDS = {256: 0x1357, 1024: 0x2468, 4096: 0x369C}


def test_vector(n: int, seed: int = None, mod: int = 3000):
    """Same LCG as fill_input() in sw/soc/bench/fft_bm.c."""
    if seed is None:
        seed = SEEDS[n]
    rng = seed & 0xFFFFFFFF
    vals = []
    for _ in range(2 * n):
        rng = (rng * 1664525 + 1013904223) & 0xFFFFFFFF
        vals.append((rng >> 16) % (2 * mod + 1) - mod)
    return vals[0::2], vals[1::2]


def write_expect(n: int, stages: int, path: Path):
    """Write the expected 2*n word array after `stages` full radix-4 stages."""
    re, im = test_vector(n)
    for s in range(stages):
        re, im = stage_on_tiles(re, im, n, s)
    words = []
    for k in range(n):
        words.append(re[k] & 0xFFFFFFFF)
        words.append(im[k] & 0xFFFFFFFF)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(struct.pack("<I", w) for w in words))
    print(f"[exp] {path}: {len(words)} words (N={n}, {stages} stages)")


def write_lut(path: Path):
    words = []
    for n in (256, 1024, 4096):
        for s in range(full_stage_count(n)):
            for q in range(tile_count(n)):
                words += twiddle_words(n, stage_L(n, s), q)
    words += tail_twiddle_words()
    assert len(words) == LUT_WORDS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(struct.pack("<I", w & 0xFFFFFFFF) for w in words))
    print(f"[lut] {path}: {len(words)} words ({len(words) * 4} bytes), "
          f"tail block at word {LUT_TAIL}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "lut":
        write_lut(Path(sys.argv[2]))
        return 0
    if len(sys.argv) > 3 and sys.argv[1] == "exp":
        write_expect(int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4]))
        return 0
    ok = True
    for n in (256, 1024, 4096):
        ok &= self_test(n)
    print(f"[info] LUT words={LUT_WORDS} ({LUT_WORDS * 4} B) offsets={LUT_OFFSET} "
          f"tail={LUT_TAIL}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
