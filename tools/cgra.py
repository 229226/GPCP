#!/usr/bin/env python3
"""GPCP CGRA microcode assembler, kernel library and simulation driver (M0).

The toolchain mirrors the real flow used later for baseband kernels:

    kernel (Python)  ->  microcode words + PE preload + sample stream
                     ->  Verilator RTL simulation (driven over AXI4-Lite)
                     ->  dump + golden-model comparison

Usage:
    python3 tools/cgra.py test                 # build sim (if needed) and run all kernels
    python3 tools/cgra.py test --kernel fir16
    python3 tools/cgra.py build                # only build the Verilator model
"""

import argparse
import glob
import os
import random
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
SIM_DIR = ROOT / "sim" / "cgra"
SIM_BIN = SIM_DIR / "build" / "Vcgra_axi_lite"
BUILD_DIR = ROOT / "build" / "kernels"

CGRA_N = 4
NPES = CGRA_N * CGRA_N

# ---------------------------------------------------------------- instruction set
OPS = {
    "nop": 0, "mov": 1, "add": 2, "sub": 3, "mul": 4, "mac": 5, "lacc": 6,
    "shr": 7, "shl": 8, "and": 9, "xor": 10, "max": 11, "min": 12, "macl": 13,
    "ld": 14, "st": 15,
}
SRC = {
    "r0": 0, "r1": 1, "r2": 2, "r3": 3, "r4": 4, "r5": 5, "r6": 6, "r7": 7,
    "n": 8, "e": 9, "s": 10, "w": 11, "zero": 12, "imm": 13, "acc": 14, "link": 15,
}


def sel(v) -> int:
    if isinstance(v, int):
        if not 0 <= v < 8:
            raise ValueError(f"register index out of range: {v}")
        return v
    return SRC[v]


def enc(op: str, a="zero", b="zero", dst=0, imm=0) -> int:
    word = (OPS[op] << 27) | (sel(a) << 23) | (sel(b) << 19) | ((dst & 7) << 16) | (imm & 0xFFFF)
    return word


class Program:
    """Spatial VLIW microcode: one instruction per (cycle, PE)."""

    def __init__(self, n: int = CGRA_N):
        self.n = n
        self.words: Dict[Tuple[int, int], int] = {}

    def set(self, cycle: int, pe: int, op: str, a="zero", b="zero", dst=0, imm=0):
        if not 0 <= pe < self.n * self.n:
            raise ValueError(f"PE out of range: {pe}")
        self.words[(cycle, pe)] = enc(op, a, b, dst, imm)

    def fill(self, cycle: int, op: str, a="zero", b="zero", dst=0, imm=0):
        for pe in range(self.n * self.n):
            self.set(cycle, pe, op, a, b, dst, imm)

    def build(self, cycles: int) -> List[int]:
        out = []
        for c in range(cycles):
            for pe in range(self.n * self.n):
                out.append(self.words.get((c, pe), enc("nop")))
        return out


@dataclass
class Kernel:
    name: str
    cycles: int
    words: List[int]
    preload: List[Tuple[int, int]] = field(default_factory=list)
    din: List[int] = field(default_factory=list)
    check: object = None
    spad_dump: Tuple[int, int] = (0, 0)   # (byte address, word count)
    dma_mem: List[int] = field(default_factory=list)          # backing store words
    dma_ops: List[Tuple[int, int, int, int, int]] = field(default_factory=list)
    buf_page: int = 0


def sx16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# ---------------------------------------------------------------- kernels
def kernel_matmul(a: List[List[int]], b: List[List[int]], settle: int = 2) -> Kernel:
    """C = A x B on a 4x4 array: PE(i,j) holds A row i in r0..r3 and B col j in r4..r7."""
    prog = Program()
    for k in range(4):
        prog.fill(k, "mac", a=k, b=4 + k)
    cycles = 4 + settle

    preload = []
    for i in range(4):
        for j in range(4):
            pe = i * 4 + j
            for k in range(4):
                preload.append((0x1000 + pe * 32 + k * 4, a[i][k] & 0xFFFF))
            for k in range(4):
                preload.append((0x1000 + pe * 32 + (4 + k) * 4, b[k][j] & 0xFFFF))

    def check(st):
        errs = []
        for i in range(4):
            for j in range(4):
                pe = i * 4 + j
                exp = sum(sx16(a[i][k]) * sx16(b[k][j]) for k in range(4))
                got = st.get((pe, "acc"), None)
                exp32 = exp & 0xFFFFFFFF
                if got != exp32:
                    errs.append(f"PE{i}{j}: acc=0x{got:08x} expected 0x{exp32:08x}")
        return errs

    return Kernel("matmul4x4", cycles, prog.build(cycles), preload, [], check)


PATH = [
    (0, 0), (0, 1), (0, 2), (0, 3),
    (1, 3), (1, 2), (1, 1), (1, 0),
    (2, 0), (2, 1), (2, 2), (2, 3),
    (3, 3), (3, 2), (3, 1), (3, 0),
]


def _prev_dir(idx: int) -> str:
    if idx == 0:
        return "w"  # streaming boundary input
    pi, pj = PATH[idx - 1]
    ci, cj = PATH[idx]
    if pi == ci and pj == cj - 1:
        return "w"  # previous PE is our west neighbour
    if pi == ci and pj == cj + 1:
        return "e"
    if pi == ci - 1 and pj == cj:
        return "n"
    if pi == ci + 1 and pj == cj:
        return "s"
    raise ValueError(f"path is not contiguous at index {idx}")


def kernel_fir16(h: List[int], x: List[int]) -> Kernel:
    """16-tap FIR front-end: a sample stream is shifted along a snake path,
    every PE multiplies its own coefficient by the sample that reaches it.
    Even cycles shift (MOV), odd cycles accumulate (MAC)."""
    k = len(x)
    cycles = 2 * k
    prog = Program()

    for pos, (pi, pj) in enumerate(PATH):
        pe = pi * 4 + pj
        d = _prev_dir(pos)
        for c in range(k):
            prog.set(2 * c, pe, "mov", a=d, dst=1)
            prog.set(2 * c + 1, pe, "mac", a=0, b=1)

    preload = []
    for pos, (pi, pj) in enumerate(PATH):
        pe = pi * 4 + pj
        preload.append((0x1000 + pe * 32 + 0 * 4, h[pos] & 0xFFFF))

    din = []
    for c in range(k):
        din.append(x[c] & 0xFFFF)
        din.append(0)

    def check(st):
        errs = []
        for pos in range(16):
            pi, pj = PATH[pos]
            pe = pi * 4 + pj
            # last sample that reached this PE
            exp_r1 = x[k - 1 - pos] & 0xFFFF if (k - 1 - pos) >= 0 else 0
            got_r1 = st.get((pe, "r1"), None)
            if got_r1 != exp_r1:
                errs.append(f"PE{pi}{pj} r1=0x{got_r1:04x} expected 0x{exp_r1:04x} (path idx {pos})")
            # accumulated product: h[pos] * sum_{m=0}^{k-1-pos} x[m]
            window = sum(sx16(x[m]) for m in range(k - pos)) if k - pos > 0 else 0
            exp_acc = (sx16(h[pos]) * window) & 0xFFFFFFFF
            got_acc = st.get((pe, "acc"), None)
            if got_acc != exp_acc:
                errs.append(f"PE{pi}{pj} acc=0x{got_acc:08x} expected 0x{exp_acc:08x}")
        return errs

    return Kernel("fir16", cycles, prog.build(cycles), preload, din, check)



# ---------------------------------------------------------------- L1 kernels
SPAD_BASE = 0xA000


def spad_addr(bank: int, word: int) -> int:
    """Host byte address of a scratchpad word (used for preload / dump)."""
    return SPAD_BASE + bank * 128 + word * 4


def spad_idx(bank: int, word: int) -> int:
    """Instruction-side address: imm[9:0] = {bank[4:0], word[4:0]}."""
    return ((bank & 0x1F) << 5) | (word & 0x1F)


def sx32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def _q15(v: float) -> int:
    q = int(round(v * 32767.0))
    return max(-32768, min(32767, q))


def _bitrev4(v: int) -> int:
    r = 0
    for b in range(4):
        r = (r << 1) | ((v >> b) & 1)
    return r


def _fft_twiddles():
    import math
    tw = {}
    for s in range(4):
        half = 1 << s
        for i in range(16):
            if (i % (2 * half)) < half:
                k = i % half
                ang = -2.0 * math.pi * k / (2 * half)
                tw[(s, i)] = (_q15(math.cos(ang)), _q15(math.sin(ang)))
    return tw


FFT_TW = _fft_twiddles()


def kernel_fft16(xr: List[int], xi: List[int]) -> Kernel:
    """Radix-2 DIT FFT-16 (Q15, scaled by 1/2 per stage).

    Data layout: bank i, word0 = real(x[i]), word1 = imag(x[i]).
    Bank 16+i, words 2*s / 2*s+1 = twiddle (Wr, Wi) of PE i at stage s.
    Active PEs of stage s: those with (i % 2**(s+1)) < 2**s; partner j = i + 2**s.
    """
    prog = Program()
    stage_cycles = 28
    for s in range(4):
        half = 1 << s
        base = s * stage_cycles
        for i in range(16):
            if (i % (2 * half)) >= half:
                continue
            j = i + half
            wr, wi = FFT_TW[(s, i)]
            # loads: Ar, Ai, Br, Bi, Wr, Wi
            prog.set(base + 0, i, "ld", dst=0, imm=spad_idx(i, 0) & 0xFFFF)
            prog.set(base + 1, i, "ld", dst=1, imm=spad_idx(i, 1) & 0xFFFF)
            prog.set(base + 2, i, "ld", dst=2, imm=spad_idx(j, 0) & 0xFFFF)
            prog.set(base + 3, i, "ld", dst=3, imm=spad_idx(j, 1) & 0xFFFF)
            prog.set(base + 4, i, "ld", dst=4, imm=spad_idx(16 + i, 2 * s) & 0xFFFF)
            prog.set(base + 5, i, "ld", dst=5, imm=spad_idx(16 + i, 2 * s + 1) & 0xFFFF)
            # tr = (Br*Wr - Bi*Wi) >> 15 ; ti = (Br*Wi + Bi*Wr) >> 15
            prog.set(base + 6, i, "mul", a=2, b=4, dst=6)
            prog.set(base + 7, i, "mul", a=3, b=5, dst=7)
            prog.set(base + 8, i, "sub", a=6, b=7, dst=6)
            prog.set(base + 9, i, "shr", a=6, dst=6, imm=15)
            prog.set(base + 10, i, "mul", a=2, b=5, dst=7)
            prog.set(base + 11, i, "mul", a=3, b=4, dst=2)
            prog.set(base + 12, i, "add", a=7, b=2, dst=7)
            prog.set(base + 13, i, "shr", a=7, dst=7, imm=15)
            # X = A + t ; Y = A - t ; then scale by 1/2
            prog.set(base + 14, i, "mov", a=0, dst=2)
            prog.set(base + 15, i, "mov", a=1, dst=3)
            prog.set(base + 16, i, "add", a=0, b=6, dst=0)
            prog.set(base + 17, i, "add", a=1, b=7, dst=1)
            prog.set(base + 18, i, "sub", a=2, b=6, dst=2)
            prog.set(base + 19, i, "sub", a=3, b=7, dst=3)
            prog.set(base + 20, i, "shr", a=0, dst=0, imm=1)
            prog.set(base + 21, i, "shr", a=1, dst=1, imm=1)
            prog.set(base + 22, i, "shr", a=2, dst=2, imm=1)
            prog.set(base + 23, i, "shr", a=3, dst=3, imm=1)
            prog.set(base + 24, i, "st", a=0, imm=spad_idx(i, 0) & 0xFFFF)
            prog.set(base + 25, i, "st", a=1, imm=spad_idx(i, 1) & 0xFFFF)
            prog.set(base + 26, i, "st", a=2, imm=spad_idx(j, 0) & 0xFFFF)
            prog.set(base + 27, i, "st", a=3, imm=spad_idx(j, 1) & 0xFFFF)
    cycles = 4 * stage_cycles + 2

    preload = []
    for i in range(16):
        r = _bitrev4(i)
        # sign-extend: the FFT A-path uses add/sub on the raw 32-bit register
        preload.append((spad_addr(i, 0), sx16(xr[r]) & 0xFFFFFFFF))
        preload.append((spad_addr(i, 1), sx16(xi[r]) & 0xFFFFFFFF))
    for s in range(4):
        half = 1 << s
        for i in range(16):
            wr, wi = FFT_TW.get((s, i), (0, 0))
            preload.append((spad_addr(16 + i, 2 * s), wr & 0xFFFF))
            preload.append((spad_addr(16 + i, 2 * s + 1), wi & 0xFFFF))

    golden = _fft_golden(xr, xi)

    def check(st):
        errs = []
        for i in range(16):
            got_r = st.get(("spad", spad_addr(i, 0)))
            got_i = st.get(("spad", spad_addr(i, 1)))
            exp_r = golden[0][i] & 0xFFFFFFFF
            exp_i = golden[1][i] & 0xFFFFFFFF
            if got_r != exp_r:
                errs.append(f"FFT X[{i}].re: got 0x{got_r:08x} expected 0x{exp_r:08x}")
            if got_i != exp_i:
                errs.append(f"FFT X[{i}].im: got 0x{got_i:08x} expected 0x{exp_i:08x}")
        return errs

    return Kernel("fft16", cycles, prog.build(cycles), preload, [], check,
                  spad_dump=(spad_addr(0, 0), 16 * 32))


def _fft_golden(xr: List[int], xi: List[int]):
    n = 16
    re = [0] * n
    im = [0] * n
    for i in range(n):
        r = _bitrev4(i)
        re[i] = sx16(xr[r])
        im[i] = sx16(xi[r])
    for s in range(4):
        half = 1 << s
        for i in range(n):
            if (i % (2 * half)) >= half:
                continue
            j = i + half
            wr, wi = FFT_TW[(s, i)]
            ar, ai = re[i], im[i]
            br, bi = sx16(re[j]), sx16(im[j])
            tr = (br * wr - bi * wi) >> 15
            ti = (br * wi + bi * wr) >> 15
            re[i] = (ar + tr) >> 1
            im[i] = (ai + ti) >> 1
            re[j] = (ar - tr) >> 1
            im[j] = (ai - ti) >> 1
    return re, im


def kernel_chnest(yr: List[int], yi: List[int], xr: List[int], xi: List[int]) -> Kernel:
    """LS channel estimation: H = Y * conj(X), one subcarrier per PE."""
    prog = Program()
    for i in range(16):
        a = spad_idx
        prog.set(0, i, "ld", dst=0, imm=a(i, 0) & 0xFFFF)   # Yr
        prog.set(1, i, "ld", dst=1, imm=a(i, 1) & 0xFFFF)   # Yi
        prog.set(2, i, "ld", dst=2, imm=a(i, 2) & 0xFFFF)   # Xr
        prog.set(3, i, "ld", dst=3, imm=a(i, 3) & 0xFFFF)   # Xi
        prog.set(4, i, "mul", a=0, b=2, dst=4)              # Yr*Xr
        prog.set(5, i, "mul", a=1, b=3, dst=5)              # Yi*Xi
        prog.set(6, i, "add", a=4, b=5, dst=4)
        prog.set(7, i, "shr", a=4, dst=4, imm=15)           # Hr
        prog.set(8, i, "mul", a=1, b=2, dst=5)              # Yi*Xr
        prog.set(9, i, "mul", a=0, b=3, dst=6)              # Yr*Xi
        prog.set(10, i, "sub", a=5, b=6, dst=5)
        prog.set(11, i, "shr", a=5, dst=5, imm=15)          # Hi
        prog.set(12, i, "st", a=4, imm=a(i, 4) & 0xFFFF)
        prog.set(13, i, "st", a=5, imm=a(i, 5) & 0xFFFF)
    cycles = 15

    preload = []
    for i in range(16):
        preload += [(spad_addr(i, 0), yr[i] & 0xFFFF), (spad_addr(i, 1), yi[i] & 0xFFFF),
                    (spad_addr(i, 2), xr[i] & 0xFFFF), (spad_addr(i, 3), xi[i] & 0xFFFF)]

    def check(st):
        errs = []
        for i in range(16):
            hr = (sx16(yr[i]) * sx16(xr[i]) + sx16(yi[i]) * sx16(xi[i])) >> 15
            hi = (sx16(yi[i]) * sx16(xr[i]) - sx16(yr[i]) * sx16(xi[i])) >> 15
            g_r = st.get(("spad", spad_addr(i, 4)))
            g_i = st.get(("spad", spad_addr(i, 5)))
            if g_r != (hr & 0xFFFFFFFF):
                errs.append(f"H[{i}].re: got 0x{g_r:08x} expected 0x{hr & 0xFFFFFFFF:08x}")
            if g_i != (hi & 0xFFFFFFFF):
                errs.append(f"H[{i}].im: got 0x{g_i:08x} expected 0x{hi & 0xFFFFFFFF:08x}")
        return errs

    return Kernel("chnest16", cycles, prog.build(cycles), preload, [], check,
                  spad_dump=(spad_addr(0, 0), 16 * 32))


def kernel_mmse(hr: List[int], hi: List[int], yr: List[int], yi: List[int]) -> Kernel:
    """MMSE equalizer numerator and |H|^2 (division is left to the CPU).

    w = conj(H) / (|H|^2 + sigma^2).  The array computes
      Nr = Hr*Yr + Hi*Yi, Ni = Hi*Yr - Hr*Yi, mag = Hr^2 + Hi^2.
    """
    prog = Program()
    for i in range(16):
        a = spad_idx
        prog.set(0, i, "ld", dst=0, imm=a(i, 0) & 0xFFFF)   # Hr
        prog.set(1, i, "ld", dst=1, imm=a(i, 1) & 0xFFFF)   # Hi
        prog.set(2, i, "ld", dst=2, imm=a(i, 2) & 0xFFFF)   # Yr
        prog.set(3, i, "ld", dst=3, imm=a(i, 3) & 0xFFFF)   # Yi
        prog.set(4, i, "mul", a=0, b=2, dst=4)              # Hr*Yr
        prog.set(5, i, "mul", a=1, b=3, dst=5)              # Hi*Yi
        prog.set(6, i, "add", a=4, b=5, dst=4)
        prog.set(7, i, "shr", a=4, dst=4, imm=15)           # Nr
        prog.set(8, i, "mul", a=1, b=2, dst=5)              # Hi*Yr
        prog.set(9, i, "mul", a=0, b=3, dst=6)              # Hr*Yi
        prog.set(10, i, "sub", a=5, b=6, dst=5)
        prog.set(11, i, "shr", a=5, dst=5, imm=15)          # Ni
        prog.set(12, i, "mul", a=0, b=0, dst=6)             # Hr^2
        prog.set(13, i, "mul", a=1, b=1, dst=7)             # Hi^2
        prog.set(14, i, "add", a=6, b=7, dst=6)
        prog.set(15, i, "shr", a=6, dst=6, imm=15)          # mag
        prog.set(16, i, "st", a=4, imm=a(i, 4) & 0xFFFF)
        prog.set(17, i, "st", a=5, imm=a(i, 5) & 0xFFFF)
        prog.set(18, i, "st", a=6, imm=a(i, 6) & 0xFFFF)
    cycles = 20

    preload = []
    for i in range(16):
        preload += [(spad_addr(i, 0), hr[i] & 0xFFFF), (spad_addr(i, 1), hi[i] & 0xFFFF),
                    (spad_addr(i, 2), yr[i] & 0xFFFF), (spad_addr(i, 3), yi[i] & 0xFFFF)]

    def check(st):
        errs = []
        for i in range(16):
            nr = (sx16(hr[i]) * sx16(yr[i]) + sx16(hi[i]) * sx16(yi[i])) >> 15
            ni = (sx16(hi[i]) * sx16(yr[i]) - sx16(hr[i]) * sx16(yi[i])) >> 15
            mag = (sx16(hr[i]) * sx16(hr[i]) + sx16(hi[i]) * sx16(hi[i])) >> 15
            for k, exp in ((4, nr), (5, ni), (6, mag)):
                got = st.get(("spad", spad_addr(i, k)))
                if got != (exp & 0xFFFFFFFF):
                    errs.append(f"MMSE[{i}] w{k}: got 0x{got:08x} expected 0x{exp & 0xFFFFFFFF:08x}")
        return errs

    return Kernel("mmse16", cycles, prog.build(cycles), preload, [], check,
                  spad_dump=(spad_addr(0, 0), 16 * 32))


def kernel_ldpc_cn(a, b, c, d) -> Kernel:
    """Min-sum check node (4 messages per PE): min1/min2/sign product."""
    prog = Program()

    def setc(cyc, pe, op, **kw):
        prog.set(cyc, pe, op, **kw)

    for i in range(16):
        A = spad_idx
        setc(0, i, "ld", dst=0, imm=A(i, 0) & 0xFFFF)
        setc(1, i, "ld", dst=1, imm=A(i, 1) & 0xFFFF)
        setc(2, i, "ld", dst=2, imm=A(i, 2) & 0xFFFF)
        setc(3, i, "ld", dst=3, imm=A(i, 3) & 0xFFFF)
        # abs(x) = (x ^ (x>>31)) - (x>>31) ; sign bits accumulate via XOR
        cyc = 4
        for src, word in ((0, 8), (1, 9), (2, 10), (3, 11)):
            setc(cyc + 0, i, "shr", a=src, dst=4, imm=31)
            setc(cyc + 1, i, "xor", a=src, b=4, dst=5)
            setc(cyc + 2, i, "sub", a=5, b=4, dst=5)
            setc(cyc + 3, i, "st", a=5, imm=A(i, word) & 0xFFFF)
            cyc += 4
        # sign product
        setc(20, i, "shr", a=0, dst=6, imm=31)
        setc(21, i, "shr", a=1, dst=7, imm=31)
        setc(22, i, "xor", a=6, b=7, dst=6)
        setc(23, i, "shr", a=2, dst=7, imm=31)
        setc(24, i, "xor", a=6, b=7, dst=6)
        setc(25, i, "shr", a=3, dst=7, imm=31)
        setc(26, i, "xor", a=6, b=7, dst=6)
        setc(27, i, "st", a=6, imm=A(i, 6) & 0xFFFF)
        # pair min/max of |a|,|b| and |c|,|d|
        setc(28, i, "ld", dst=0, imm=A(i, 8) & 0xFFFF)
        setc(29, i, "ld", dst=1, imm=A(i, 9) & 0xFFFF)
        setc(30, i, "ld", dst=2, imm=A(i, 10) & 0xFFFF)
        setc(31, i, "ld", dst=3, imm=A(i, 11) & 0xFFFF)
        setc(32, i, "min", a=0, b=1, dst=4)
        setc(33, i, "max", a=0, b=1, dst=5)
        setc(34, i, "st", a=4, imm=A(i, 12) & 0xFFFF)
        setc(35, i, "st", a=5, imm=A(i, 13) & 0xFFFF)
        setc(36, i, "min", a=2, b=3, dst=6)
        setc(37, i, "max", a=2, b=3, dst=7)
        setc(38, i, "st", a=6, imm=A(i, 14) & 0xFFFF)
        setc(39, i, "st", a=7, imm=A(i, 15) & 0xFFFF)
        # min1 = min(lo1, lo2)
        setc(40, i, "ld", dst=0, imm=A(i, 12) & 0xFFFF)
        setc(41, i, "ld", dst=1, imm=A(i, 14) & 0xFFFF)
        setc(42, i, "min", a=0, b=1, dst=4)
        setc(43, i, "st", a=4, imm=A(i, 4) & 0xFFFF)
        # min2 = min(max(lo1,lo2), min(hi1,hi2))
        setc(44, i, "max", a=0, b=1, dst=5)
        setc(45, i, "ld", dst=2, imm=A(i, 13) & 0xFFFF)
        setc(46, i, "ld", dst=3, imm=A(i, 15) & 0xFFFF)
        setc(47, i, "min", a=2, b=3, dst=6)
        setc(48, i, "min", a=5, b=6, dst=7)
        setc(49, i, "st", a=7, imm=A(i, 5) & 0xFFFF)
    cycles = 52

    preload = []
    for i in range(16):
        # sign-extend to 32 bit: the min-sum kernel uses 32-bit abs/xor ops
        preload += [(spad_addr(i, 0), sx16(a[i]) & 0xFFFFFFFF),
                    (spad_addr(i, 1), sx16(b[i]) & 0xFFFFFFFF),
                    (spad_addr(i, 2), sx16(c[i]) & 0xFFFFFFFF),
                    (spad_addr(i, 3), sx16(d[i]) & 0xFFFFFFFF)]

    def check(st):
        errs = []
        for i in range(16):
            va, vb, vc, vd = sx16(a[i]), sx16(b[i]), sx16(c[i]), sx16(d[i])
            absv = [abs(va), abs(vb), abs(vc), abs(vd)]
            sign = 0
            for v in (va, vb, vc, vd):
                sign ^= (v >> 15) & 1
            sign_total = -1 if sign else 0
            s1 = min(absv[0], absv[1]); h1 = max(absv[0], absv[1])
            s2 = min(absv[2], absv[3]); h2 = max(absv[2], absv[3])
            min1 = min(s1, s2)
            min2 = min(max(s1, s2), min(h1, h2))
            for k, exp in ((4, min1), (5, min2), (6, sign_total)):
                got = st.get(("spad", spad_addr(i, k)))
                if got != (exp & 0xFFFFFFFF):
                    errs.append(f"LDPC CN{i} w{k}: got 0x{got:08x} expected 0x{exp & 0xFFFFFFFF:08x}")
        return errs

    return Kernel("ldpc_cn", cycles, prog.build(cycles), preload, [], check,
                  spad_dump=(spad_addr(0, 0), 16 * 32))


def kernel_ldpc_bn(ch, msgs) -> Kernel:
    """LDPC variable (bit) node: v = ch + sum(m_cv); m_vc = v - m_cv.

    Per PE = one variable node:
        bank i word 0        channel LLR
        bank i words 1..4    four incoming check-node messages
        bank i words 8..11   four outgoing messages
        bank i word 12       hard decision (-1 = bit 1, 0 = bit 0)
    One PE therefore mirrors one lifted variable node (Zc parallel nodes map to
    the 16 PEs by running the kernel once per Zc/16 block).
    """
    prog = Program()
    for i in range(16):
        prog.set(0, i, "ld", dst=0, imm=spad_idx(i, 0))
        for k in range(4):
            prog.set(1 + k, i, "ld", dst=1 + k, imm=spad_idx(i, 1 + k))
        prog.set(5, i, "add", a=0, b=1, dst=5)
        prog.set(6, i, "add", a=5, b=2, dst=5)
        prog.set(7, i, "add", a=5, b=3, dst=5)
        prog.set(8, i, "add", a=5, b=4, dst=5)          # v = ch + sum
        for k in range(4):
            prog.set(9 + 2 * k, i, "sub", a=5, b=1 + k, dst=6)
            prog.set(10 + 2 * k, i, "st", a=6, imm=spad_idx(i, 8 + k))
        prog.set(17, i, "shr", a=5, dst=6, imm=31)      # -1 if the bit is 1
        prog.set(18, i, "st", a=6, imm=spad_idx(i, 12))
    cycles = 19

    preload = []
    for i in range(16):
        preload.append((spad_addr(i, 0), sx16(ch[i]) & 0xFFFFFFFF))
        for k in range(4):
            preload.append((spad_addr(i, 1 + k), sx16(msgs[k][i]) & 0xFFFFFFFF))

    def check(st):
        errs = []
        for i in range(16):
            v = sx16(ch[i]) + sum(sx16(msgs[k][i]) for k in range(4))
            for k in range(4):
                exp = v - sx16(msgs[k][i])
                got = st.get(("spad", spad_addr(i, 8 + k)))
                if got != (exp & 0xFFFFFFFF):
                    errs.append(f"BN{i} m{k}: got 0x{got:08x} want 0x{exp & 0xFFFFFFFF:08x}")
            exp = -1 if v < 0 else 0
            got = st.get(("spad", spad_addr(i, 12)))
            if got != (exp & 0xFFFFFFFF):
                errs.append(f"BN{i} hard: got 0x{got:08x} want 0x{exp & 0xFFFFFFFF:08x}")
        return errs

    return Kernel("ldpc_bn", cycles, prog.build(cycles), preload, [], check,
                  spad_dump=(spad_addr(0, 0), 16 * 32))


def kernel_dma_pingpong() -> Kernel:
    """Copy engine fills both scratchpad pages; the array runs LS on page 1.

    Demonstrates double buffering: page 0 keeps the vector loaded by the first
    descriptor, page 1 is loaded by the second descriptor and consumed by the
    kernel (BUF_PAGE = 1).
    """
    rnd = random.Random(61)

    def make():
        yr = [rnd.randint(-2000, 2000) for _ in range(16)]
        yi = [rnd.randint(-2000, 2000) for _ in range(16)]
        xr = [rnd.choice([-1, 1]) for _ in range(16)]
        xi = [rnd.choice([-1, 1]) for _ in range(16)]
        words = []
        for i in range(16):
            words += [yr[i] & 0xFFFF, yi[i] & 0xFFFF, xr[i] & 0xFFFF, xi[i] & 0xFFFF]
        return words, (yr, yi, xr, xi)

    w0, d0 = make()
    w1, d1 = make()
    mem = w0 + w1

    base = kernel_chnest([0] * 16, [0] * 16, [0] * 16, [0] * 16)
    # src bytes, dst, len, page, dir, stride, row: source word 4i+k -> bank i, word k
    ops = [(0, 0, 64, 0, 0, 32, 4), (256, 0, 64, 1, 0, 32, 4)]

    def check(st):
        errs = []
        # page 0 must still hold the first vector (buffers are isolated)
        for i in range(16):
            for k in range(4):
                got = st.get(("spad", spad_addr(i, k)))
                exp = w0[i * 4 + k] & 0xFFFFFFFF
                if got != exp:
                    errs.append(f"page0 bank{i} w{k}: got 0x{got:08x} expected 0x{exp:08x}")
        # page 1 results live in banks 32..47 (host addresses)
        yr, yi, xr, xi = d1
        for i in range(16):
            hr = (sx16(yr[i]) * sx16(xr[i]) + sx16(yi[i]) * sx16(xi[i])) >> 15
            hi = (sx16(yi[i]) * sx16(xr[i]) - sx16(yr[i]) * sx16(xi[i])) >> 15
            got_r = st.get(("spad", spad_addr(32 + i, 4)))
            got_i = st.get(("spad", spad_addr(32 + i, 5)))
            if got_r != (hr & 0xFFFFFFFF):
                errs.append(f"page1 H[{i}].re: got 0x{got_r:08x} expected 0x{hr & 0xFFFFFFFF:08x}")
            if got_i != (hi & 0xFFFFFFFF):
                errs.append(f"page1 H[{i}].im: got 0x{got_i:08x} expected 0x{hi & 0xFFFFFFFF:08x}")
        return errs

    return Kernel("dma_pingpong", base.cycles, base.words, [], [], check,
                  spad_dump=(spad_addr(0, 0), 64 * 32),
                  dma_mem=mem, dma_ops=ops, buf_page=1)




# ---------------------------------------------------------------- FFT stage
# Microcode for one tiled radix-4 stage (identical to sw/soc/fft_bm.c):
#   data      word (bi*4 + j)*2 + h   (banks 0..15)
#   twiddles  word 512 + (m-1)*128 + bi*2 + h
# 16 rounds of 4 butterflies, one PE = one (butterfly, output m), 28 cycles.
FFT_TW_BASE = 512
FFT_TW_WORDS = 384
FFT_STAGE_CYCLES = 448


def _emit_round(prog: Program, base: int, pe: int, m: int,
                j0: int, jstep: int, st0: int, mstep: int, tw: int):
    prog.set(base + 0, pe, "ld", dst=4, imm=j0 & 0xFFFF)
    prog.set(base + 1, pe, "ld", dst=5, imm=(j0 + 1) & 0xFFFF)
    prog.set(base + 2, pe, "mov", a=4, dst=0)
    prog.set(base + 3, pe, "mov", a=5, dst=1)
    for j in (1, 2, 3):
        w = j0 + j * jstep
        f = (j * m) & 3
        prog.set(base + 4 * j + 0, pe, "ld", dst=4, imm=w & 0xFFFF)
        prog.set(base + 4 * j + 1, pe, "ld", dst=5, imm=(w + 1) & 0xFFFF)
        if f == 0:
            prog.set(base + 4 * j + 2, pe, "add", a=0, b=4, dst=0)
            prog.set(base + 4 * j + 3, pe, "add", a=1, b=5, dst=1)
        elif f == 1:                       # -i*v = ( v_i, -v_r)
            prog.set(base + 4 * j + 2, pe, "add", a=0, b=5, dst=0)
            prog.set(base + 4 * j + 3, pe, "sub", a=1, b=4, dst=1)
        elif f == 2:
            prog.set(base + 4 * j + 2, pe, "sub", a=0, b=4, dst=0)
            prog.set(base + 4 * j + 3, pe, "sub", a=1, b=5, dst=1)
        else:                              # +i*v = (-v_i,  v_r)
            prog.set(base + 4 * j + 2, pe, "sub", a=0, b=5, dst=0)
            prog.set(base + 4 * j + 3, pe, "add", a=1, b=4, dst=1)
    out = st0 + m * mstep
    if tw >= 0:
        prog.set(base + 16, pe, "ld", dst=6, imm=tw & 0xFFFF)
        prog.set(base + 17, pe, "ld", dst=7, imm=(tw + 1) & 0xFFFF)
        prog.set(base + 18, pe, "mul", a=0, b=6, dst=2)
        prog.set(base + 19, pe, "mul", a=1, b=7, dst=3)
        prog.set(base + 20, pe, "sub", a=2, b=3, dst=2)
        prog.set(base + 21, pe, "mul", a=0, b=7, dst=3)
        prog.set(base + 22, pe, "mul", a=1, b=6, dst=4)
        prog.set(base + 23, pe, "add", a=3, b=4, dst=3)
        sh = 17
    else:
        prog.set(base + 22, pe, "mov", a=0, dst=2)
        prog.set(base + 23, pe, "mov", a=1, dst=3)
        sh = 2
    prog.set(base + 24, pe, "shr", a=2, dst=2, imm=sh)
    prog.set(base + 25, pe, "shr", a=3, dst=3, imm=sh)
    prog.set(base + 26, pe, "st", a=2, imm=out & 0xFFFF)
    prog.set(base + 27, pe, "st", a=3, imm=(out + 1) & 0xFFFF)


def kernel_fft_stage(xr, xi, n=256, s=0, q=0):
    """One tiled radix-4 stage of the FFT, scratchpad-resident (no DMA)."""
    import fft_plan as fp
    prog = Program()
    for r in range(16):
        for pe in range(16):
            b, m = pe >> 2, pe & 3
            bi = 4 * r + b
            j0 = 2 * bi                     # word = 128*j + 2*bi + h
            tw = (FFT_TW_BASE + (m - 1) * 128 + bi * 2) if m else -1
            _emit_round(prog, r * 28, pe, m, j0, 128, j0, 128, tw)

    L = fp.stage_L(n, s)
    sp = [0] * 1024
    for (ram_w, sp_w, words, row, stride) in fp.tile_transfers(n, L, q):
        for i, dst in enumerate(fp.walk(sp_w, words, row, stride)):
            g = (ram_w + i) // 2
            h = (ram_w + i) % 2
            sp[dst] = (xr[g] if h == 0 else xi[g])
    sp[FFT_TW_BASE:FFT_TW_BASE + FFT_TW_WORDS] = fp.twiddle_words(n, L, q)

    preload = []
    for w in range(1024):
        preload.append((spad_addr(w >> 5, w & 31), sp[w] & 0xFFFFFFFF))

    exp = list(sp)
    for bi in range(64):
        vr = [exp[j * 128 + 2 * bi] for j in range(4)]
        vi = [exp[j * 128 + 2 * bi + 1] for j in range(4)]
        for m in range(4):
            ar, ai = fp.dft4([fp.wrap32(v) for v in vr], [fp.wrap32(v) for v in vi], m)
            if m == 0:
                ar, ai = ar >> 2, ai >> 2
            else:
                ar, ai = fp.twiddled(ar, ai,
                                     fp.wrap32(sp[FFT_TW_BASE + (m - 1) * 128 + bi * 2]),
                                     fp.wrap32(sp[FFT_TW_BASE + (m - 1) * 128 + bi * 2 + 1]))
            exp[m * 128 + 2 * bi] = ar
            exp[m * 128 + 2 * bi + 1] = ai

    def check(st):
        errs = []
        for w in range(512):
            got = st.get(("spad", spad_addr(w >> 5, w & 31)))
            if got != (exp[w] & 0xFFFFFFFF):
                if len(errs) < 8:
                    errs.append(f"spad word {w} (bank {w >> 5} w{w & 31}): got "
                                f"0x{got:08x} expected 0x{exp[w] & 0xFFFFFFFF:08x}")
        return errs

    return Kernel("fft_stage", FFT_STAGE_CYCLES, prog.build(FFT_STAGE_CYCLES),
                  preload, [], check, spad_dump=(spad_addr(0, 0), 32 * 32))




FFT_BLOCK_CYCLES = 336


def kernel_fft_block(xr, xi):
    """Fused 64-point block kernel: the radix-16 tail (L = 64, 16, 4)."""
    import fft_plan as fp
    prog = Program()
    # pass A: L = 64, M = 16, twiddles at 512 + (m-1)*32 + t*2
    for r in range(4):
        for pe in range(16):
            b, m = pe >> 2, pe & 3
            t = 4 * r + b
            site = t * 2
            tw = (FFT_TW_BASE + (m - 1) * 32 + t * 2) if m else -1
            _emit_round(prog, r * 28, pe, m, site, 32, site, 32, tw)
    # pass B: L = 16, M = 4, twiddles at 512 + 96 + (m-1)*8 + t2*2
    for r in range(4):
        for pe in range(16):
            b, m = pe >> 2, pe & 3
            beta = 4 * r + b
            sb, t2 = beta >> 2, beta & 3
            site = (sb * 16 + t2) * 2
            tw = (FFT_TW_BASE + 96 + (m - 1) * 8 + t2 * 2) if m else -1
            _emit_round(prog, 112 + r * 28, pe, m, site, 8, site, 8, tw)
    # pass C: L = 4, M = 1, no twiddles
    for r in range(4):
        for pe in range(16):
            b, m = pe >> 2, pe & 3
            sbn = 4 * r + b
            site = sbn * 4 * 2
            _emit_round(prog, 224 + r * 28, pe, m, site, 2, site, 2, -1)

    tw = fp.tail_twiddle_words()
    preload = [(spad_addr(i >> 5, i & 31), xr[i] & 0xFFFFFFFF) for i in range(64)]
    preload += [(spad_addr(i >> 5, i & 31), xi[i] & 0xFFFFFFFF) for i in range(0)]
    preload = []
    for i in range(64):
        preload.append((spad_addr((2 * i) >> 5, (2 * i) & 31), xr[i] & 0xFFFFFFFF))
        preload.append((spad_addr((2 * i + 1) >> 5, (2 * i + 1) & 31), xi[i] & 0xFFFFFFFF))
    for i in range(len(tw)):
        w = FFT_TW_BASE + i
        preload.append((spad_addr(w >> 5, w & 31), tw[i]))

    gr, gi = fp.reference_fft(xr, xi, 64)

    def check(st):
        errs = []
        for i in range(64):
            got_r = st.get(("spad", spad_addr((2 * i) >> 5, (2 * i) & 31)))
            got_i = st.get(("spad", spad_addr((2 * i + 1) >> 5, (2 * i + 1) & 31)))
            if got_r != (gr[i] & 0xFFFFFFFF):
                if len(errs) < 8:
                    errs.append(f"block X[{i}].re: got 0x{got_r:08x} "
                                f"expected 0x{gr[i] & 0xFFFFFFFF:08x}")
            if got_i != (gi[i] & 0xFFFFFFFF):
                if len(errs) < 8:
                    errs.append(f"block X[{i}].im: got 0x{got_i:08x} "
                                f"expected 0x{gi[i] & 0xFFFFFFFF:08x}")
        return errs

    return Kernel("fft_block", FFT_BLOCK_CYCLES, prog.build(FFT_BLOCK_CYCLES),
                  preload, [], check, spad_dump=(spad_addr(0, 0), 32 * 32))


def build_fft_block_kernel() -> Kernel:
    rnd = random.Random(52)
    xr = [rnd.randint(-3000, 3000) for _ in range(64)]
    xi = [rnd.randint(-3000, 3000) for _ in range(64)]
    return kernel_fft_block(xr, xi)

def build_fft_stage_kernel() -> Kernel:
    rnd = random.Random(51)
    xr = [rnd.randint(-3000, 3000) for _ in range(256)]
    xi = [rnd.randint(-3000, 3000) for _ in range(256)]
    return kernel_fft_stage(xr, xi, 256, 0, 0)


def build_fft_kernel() -> Kernel:
    rnd = random.Random(11)
    xr = [rnd.randint(-3000, 3000) for _ in range(16)]
    xi = [rnd.randint(-3000, 3000) for _ in range(16)]
    return kernel_fft16(xr, xi)


def build_chnest_kernel() -> Kernel:
    rnd = random.Random(21)
    xr = [rnd.choice([-1, 1]) for _ in range(16)]          # QPSK pilots
    xi = [rnd.choice([-1, 1]) for _ in range(16)]
    yr = [rnd.randint(-2000, 2000) for _ in range(16)]
    yi = [rnd.randint(-2000, 2000) for _ in range(16)]
    return kernel_chnest(yr, yi, xr, xi)


def build_mmse_kernel() -> Kernel:
    rnd = random.Random(31)
    hr = [rnd.randint(-1500, 1500) for _ in range(16)]
    hi = [rnd.randint(-1500, 1500) for _ in range(16)]
    yr = [rnd.randint(-2000, 2000) for _ in range(16)]
    yi = [rnd.randint(-2000, 2000) for _ in range(16)]
    return kernel_mmse(hr, hi, yr, yi)


def build_ldpc_bn_kernel() -> Kernel:
    rnd = random.Random(43)
    ch = [rnd.randint(-800, 800) for _ in range(16)]
    msgs = [[rnd.randint(-400, 400) for _ in range(16)] for _ in range(4)]
    return kernel_ldpc_bn(ch, msgs)


def build_ldpc_kernel() -> Kernel:
    rnd = random.Random(41)
    a = [rnd.randint(-300, 300) for _ in range(16)]
    b = [rnd.randint(-300, 300) for _ in range(16)]
    c = [rnd.randint(-300, 300) for _ in range(16)]
    d = [rnd.randint(-300, 300) for _ in range(16)]
    return kernel_ldpc_cn(a, b, c, d)


# ---------------------------------------------------------------- driver
def find_verilator() -> str:
    """Locate Verilator, preferring the copy that belongs to this project.

    third_party/eda/oss-cad-suite (fetched by tools/fetch_eda.sh) is the
    reproducible choice; the other candidates keep the flow working on machines
    where someone already has an oss-cad-suite lying around.
    """
    if os.environ.get("VERILATOR"):
        return os.environ["VERILATOR"]
    root = Path(__file__).resolve().parent.parent
    local = root / "third_party/eda/oss-cad-suite/bin/verilator"
    if local.exists():
        return str(local)
    for p in sorted(glob.glob(os.path.expanduser("~/Downloads/oss-cad-suite*/oss-cad-suite/bin/verilator"))):
        return p
    for p in glob.glob("/opt/oss-cad-suite/bin/verilator"):
        return p
    return "verilator"


def clean_env() -> dict:
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)  # keep the host g++/libstdc++ from being shadowed
    vdir = str(Path(find_verilator()).parent)
    env["PATH"] = vdir + ":" + env.get("PATH", "")
    return env


def build_sim(force: bool = False) -> None:
    if SIM_BIN.exists() and not force:
        return
    print(f"[build] verilator model -> {SIM_BIN}")
    subprocess.run(["make", "-j8"], cwd=SIM_DIR, env=clean_env(), check=True)


def write_kernel(k: Kernel, d: Path) -> Dict[str, Path]:
    d.mkdir(parents=True, exist_ok=True)
    prog = d / "prog.hex"
    prog.write_text("\n".join(f"0x{w:08x}" for w in k.words) + "\n")
    pre = d / "preload.txt"
    pre.write_text("\n".join(f"0x{a:04x} 0x{v:08x}" for a, v in k.preload) + "\n")
    din = d / "din.txt"
    din.write_text("\n".join(f"0x{v & 0xFFFF:04x}" for v in k.din) + "\n")
    paths = {"prog": prog, "preload": pre, "din": din, "dump": d / "dump.txt", "dir": d}
    if k.dma_mem:
        p = d / "dma_mem.txt"
        p.write_text("\n".join(f"0x{v & 0xFFFFFFFF:08x}" for v in k.dma_mem) + "\n")
        paths["dma_mem"] = p
    if k.dma_ops:
        p = d / "dma_ops.txt"
        p.write_text("\n".join(" ".join(str(x) for x in op) for op in k.dma_ops) + "\n")
        paths["dma_ops"] = p
    return paths


def parse_dump(path: Path) -> dict:
    st = {}
    for line in path.read_text().splitlines():
        t = line.split()
        if not t:
            continue
        if t[0] == "cycles":
            st["cycles"] = int(t[1])
        elif t[0] == "pe":
            st[(int(t[1]), t[2])] = int(t[3], 16)
        elif t[0] == "spad":
            st[("spad", int(t[1], 16))] = int(t[2], 16)
    return st


def run_kernel(k: Kernel, verbose: bool = True) -> bool:
    paths = write_kernel(k, BUILD_DIR / k.name)
    cmd = [str(SIM_BIN), "--prog", str(paths["prog"]), "--cycles", str(k.cycles),
           "--preload", str(paths["preload"]), "--dump", str(paths["dump"])]
    if k.din:
        cmd += ["--din", str(paths["din"])]
    if k.spad_dump[1]:
        cmd += ["--spad-dump", hex(k.spad_dump[0]), str(k.spad_dump[1])]
    if "dma_mem" in paths:
        cmd += ["--dma-mem", str(paths["dma_mem"])]
    if "dma_ops" in paths:
        cmd += ["--dma-ops", str(paths["dma_ops"])]
    if k.buf_page:
        cmd += ["--buf-page", str(k.buf_page)]
    r = subprocess.run(cmd, env=clean_env(), capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[FAIL] {k.name}: simulator error rc={r.returncode}\n{r.stderr}")
        return False
    st = parse_dump(paths["dump"])
    errs = k.check(st) if k.check else []
    if errs:
        print(f"[FAIL] {k.name}: {len(errs)} mismatch(es)")
        for e in errs[:10]:
            print("        " + e)
        return False
    if verbose:
        print(f"[ OK ] {k.name}: {k.cycles} cycles, {len(k.words)} microcode words, "
              f"{len(k.preload)} preload writes, {len(k.din)} samples")
    return True


# ---------------------------------------------------------------- test vectors
def build_matmul_kernel() -> Kernel:
    rnd = random.Random(7)
    a = [[rnd.randint(-8, 8) for _ in range(4)] for _ in range(4)]
    b = [[rnd.randint(-8, 8) for _ in range(4)] for _ in range(4)]
    return kernel_matmul(a, b)


def build_fir_kernel() -> Kernel:
    h = [1, -2, 3, -4, 5, -6, 7, -8, 9, -10, 11, -12, 13, -14, 15, -16]
    x = [3, -1, 4, -1, 5, -9, 2, 6]
    return kernel_fir16(h, x)




def main() -> int:
    ap = argparse.ArgumentParser(description="GPCP CGRA M0 toolchain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--force", action="store_true")
    p_test = sub.add_parser("test")
    p_test.add_argument("--kernel", default="all", choices=["all"] + list(kernel_registry()))
    p_test.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    if args.cmd == "build":
        build_sim(force=args.force)
        return 0

    build_sim(force=getattr(args, "rebuild", False))
    kreg = kernel_registry()
    names = list(kreg) if args.kernel == "all" else [args.kernel]
    ok = True
    for n in names:
        ok &= run_kernel(kreg[n]())
    print("ALL TESTS PASSED" if ok else "SOME TESTS FAILED")
    return 0 if ok else 1





def kernel_registry():
    return {"matmul": build_matmul_kernel, "fir16": build_fir_kernel,
           "pss_corr": build_pss_kernel, "sss_corr": build_sss_kernel,
           "prach_corr": build_prach_kernel,
           "polar_enc": build_polar_enc_kernel, "polar_node": build_polar_node_kernel,
           "fft16": build_fft_kernel, "fft_stage": build_fft_stage_kernel,
           "fft_block": build_fft_block_kernel,
           "chnest16": build_chnest_kernel,
           "mmse16": build_mmse_kernel, "ldpc_cn": build_ldpc_kernel,
           "ldpc_bn": build_ldpc_bn_kernel,
           "dma_pingpong": kernel_dma_pingpong}


# ------------------------------------------------------- NR5G sync + polar
# PSS/SSS detection: 16 PEs, 8 BPSK taps each (127 of 128 used). Per PE the
# real part accumulates in `acc` (MAC) and the imaginary part in a register.
#   rx point t : bank t>>4, word 2*(t&15) (re), +1 (im)   banks 0..7
#   ref  point t : bank 8+(t>>4), word t&15               banks 8..15
#   result: bank 16+p word 0/1 = (re, im) partial        banks 16..31
NR_CORR_CYCLES = 52


def kernel_bpsk_corr(rx_re, rx_im, ref, name="bpsk_corr"):
    import nr_seq as nr
    prog = Program()
    for p in range(16):
        prog.set(0, p, "lacc", a="zero")
        prog.set(1, p, "mov", a="zero", dst=2)
        for k in range(8):
            t = p * 8 + k
            base = 2 + k * 6
            if t >= 127:
                continue
            prog.set(base + 0, p, "ld", dst=4, imm=spad_idx(t >> 4, 2 * (t & 15)))
            prog.set(base + 1, p, "ld", dst=5, imm=spad_idx(t >> 4, 2 * (t & 15) + 1))
            prog.set(base + 2, p, "ld", dst=6, imm=spad_idx(8 + (t >> 4), t & 15))
            prog.set(base + 3, p, "mac", a=4, b=6)
            prog.set(base + 4, p, "mul", a=5, b=6, dst=7)
            prog.set(base + 5, p, "add", a=2, b=7, dst=2)
        prog.set(50, p, "st", a="acc", imm=spad_idx(16 + p, 0))
        prog.set(51, p, "st", a=2, imm=spad_idx(16 + p, 1))

    preload = []
    for t in range(127):
        preload.append((spad_addr(t >> 4, 2 * (t & 15)), sx16(rx_re[t]) & 0xFFFFFFFF))
        preload.append((spad_addr(t >> 4, 2 * (t & 15) + 1), sx16(rx_im[t]) & 0xFFFFFFFF))
    ref15 = nr.q15_bpsk(ref)
    for t in range(127):
        preload.append((spad_addr(8 + (t >> 4), t & 15), ref15[t] & 0xFFFFFFFF))

    exp = nr.corr_partials(rx_re, rx_im, ref)

    def check(st):
        errs = []
        for p in range(16):
            got_r = st.get(("spad", spad_addr(16 + p, 0)))
            got_i = st.get(("spad", spad_addr(16 + p, 1)))
            if got_r != (exp[p][0] & 0xFFFFFFFF):
                errs.append(f"PE{p} re: got 0x{got_r:08x} expected 0x{exp[p][0] & 0xFFFFFFFF:08x}")
            if got_i != (exp[p][1] & 0xFFFFFFFF):
                errs.append(f"PE{p} im: got 0x{got_i:08x} expected 0x{exp[p][1] & 0xFFFFFFFF:08x}")
        return errs

    return Kernel(name, NR_CORR_CYCLES, prog.build(NR_CORR_CYCLES), preload, [], check,
                  spad_dump=(spad_addr(0, 0), 32 * 32))


def build_pss_kernel() -> Kernel:
    import nr_seq as nr
    rnd = random.Random(71)
    rx_re = [rnd.randint(-8000, 8000) for _ in range(127)]
    rx_im = [rnd.randint(-8000, 8000) for _ in range(127)]
    return kernel_bpsk_corr(rx_re, rx_im, nr.pss_seq(1), name="pss_corr")


def build_sss_kernel() -> Kernel:
    import nr_seq as nr
    rnd = random.Random(72)
    rx_re = [rnd.randint(-8000, 8000) for _ in range(127)]
    rx_im = [rnd.randint(-8000, 8000) for _ in range(127)]
    return kernel_bpsk_corr(rx_re, rx_im, nr.sss_seq(42, 1), name="sss_corr")


# ------------------------------------------------------- PRACH preamble correlate
# 38.321 Msg1: the gNB searches the cyclic shift of one ZC root sequence.  Both
# the received chips and the reference are complex, so the real-only
# kernel_bpsk_corr cannot be reused -- this variant keeps the real part in `acc`
# and the imaginary part in a register, and finishes with a 16-lane butterfly so
# the array hands back ONE (re, im) pair per candidate shift.
#   spad layout (page-relative, linear word = bank*32 + word):
#     0..253    rx chips y[i] = (re, im) at 2i, 2i+1        banks 0..7
#     256..509  reference taps r[i] = (re, im)              banks 8..15
#     512..543  per-PE partials (re, im)                    bank 16
#     544..575  butterfly stages, final sum at 572/573      bank 17
#   PE p owns taps 8p .. 8p+7 (only the first PRACH_TAPS of them).
# The candidate rotation lives in RAM: the copy engine reloads the 254 reference
# words per shift, so one microcode program serves the whole L_RA-wide search.
PRACH_TAPS = 127
PRACH_REF_OFF = 256
PRACH_PART_OFF = 512
PRACH_RED_OFF = 544
PRACH_CORR_CYCLES = 128


def zc_corr_partials(y, ref, taps=PRACH_TAPS):
    """Per-PE (re, im) partials and the total -- the arithmetic of msg1_detect.

        re = sum_i (y_re*r_re + y_im*r_im),  im = sum_i (y_im*r_re - y_re*r_im)

    i.e. sum_i y[i] * conj(ref[i]).
    """
    part = []
    for p in range(16):
        re = im = 0
        for k in range(8):
            t = p * 8 + k
            if t >= taps:
                break
            yr, yi = y[t]
            cr, ci = ref[t]
            re += yr * cr + yi * ci
            im += yi * cr - yr * ci
        part.append((re, im))
    return part, (sum(v[0] for v in part), sum(v[1] for v in part))


def kernel_zc_corr(y, ref, taps=PRACH_TAPS, name="zc_corr"):
    prog = Program()
    for p in range(16):
        prog.set(0, p, "lacc", a="zero")
        prog.set(1, p, "mov", a="zero", dst=6)
        for k in range(8):
            t = p * 8 + k
            if t >= taps:
                break
            b = 2 + k * 10
            prog.set(b + 0, p, "ld", dst=0, imm=2 * t)                        # y re
            prog.set(b + 1, p, "ld", dst=1, imm=2 * t + 1)                    # y im
            prog.set(b + 2, p, "ld", dst=2, imm=PRACH_REF_OFF + 2 * t)        # r re
            prog.set(b + 3, p, "ld", dst=3, imm=PRACH_REF_OFF + 2 * t + 1)    # r im
            prog.set(b + 4, p, "mac", a=0, b=2)          # acc += y_re*r_re
            prog.set(b + 5, p, "mac", a=1, b=3)          # acc += y_im*r_im
            prog.set(b + 6, p, "mul", a=1, b=2, dst=4)   # y_im*r_re
            prog.set(b + 7, p, "mul", a=0, b=3, dst=5)   # y_re*r_im
            prog.set(b + 8, p, "add", a=6, b=4, dst=6)
            prog.set(b + 9, p, "sub", a=6, b=5, dst=6)
        prog.set(82, p, "st", a="acc", imm=PRACH_PART_OFF + 2 * p)
        prog.set(83, p, "st", a=6, imm=PRACH_PART_OFF + 2 * p + 1)

    # 16 -> 8 -> 4 -> 2 -> 1 butterfly on both components, PEs 0..(width-1)
    stages = [(PRACH_PART_OFF, 8, PRACH_RED_OFF),
              (PRACH_RED_OFF, 4, PRACH_RED_OFF + 16),
              (PRACH_RED_OFF + 16, 2, PRACH_RED_OFF + 24),
              (PRACH_RED_OFF + 24, 1, PRACH_RED_OFF + 28)]
    for j, (src, width, dst) in enumerate(stages):
        c = 88 + j * 10
        for i in range(width):
            prog.set(c + 0, i, "ld", dst=0, imm=src + 2 * i)
            prog.set(c + 1, i, "ld", dst=1, imm=src + 2 * i + 1)
            prog.set(c + 2, i, "ld", dst=2, imm=src + 2 * (i + width))
            prog.set(c + 3, i, "ld", dst=3, imm=src + 2 * (i + width) + 1)
            prog.set(c + 4, i, "add", a=0, b=2, dst=0)
            prog.set(c + 5, i, "add", a=1, b=3, dst=1)
            prog.set(c + 6, i, "st", a=0, imm=dst + 2 * i)
            prog.set(c + 7, i, "st", a=1, imm=dst + 2 * i + 1)

    preload = []
    for t in range(taps):
        preload.append((spad_addr(0, 0) + 8 * t, y[t][0] & 0xFFFFFFFF))
        preload.append((spad_addr(0, 0) + 8 * t + 4, y[t][1] & 0xFFFFFFFF))
    for t in range(taps):
        preload.append((spad_addr(8, 0) + 8 * t, ref[t][0] & 0xFFFFFFFF))
        preload.append((spad_addr(8, 0) + 8 * t + 4, ref[t][1] & 0xFFFFFFFF))

    part, total = zc_corr_partials(y, ref, taps)

    def check(st):
        errs = []
        for p in range(16):
            for q, want in enumerate(part[p]):
                got = st.get(("spad", spad_addr(16, 2 * p + q)))
                if got != (want & 0xFFFFFFFF):
                    errs.append(f"PE{p} {'re' if q == 0 else 'im'}: got "
                                f"0x{got:08x} expected 0x{want & 0xFFFFFFFF:08x}")
        for q, want in enumerate(total):
            got = st.get(("spad", spad_addr(17, 28 + q)))
            if got != (want & 0xFFFFFFFF):
                errs.append(f"total {'re' if q == 0 else 'im'}: got 0x{got:08x} "
                            f"expected 0x{want & 0xFFFFFFFF:08x}")
        return errs

    return Kernel(name, PRACH_CORR_CYCLES, prog.build(PRACH_CORR_CYCLES), preload,
                  [], check, spad_dump=(spad_addr(0, 0), 32 * 32))


def build_prach_kernel() -> Kernel:
    """One candidate shift of a real L_RA=139 ZC root against a noisy Rx block."""
    import nr_ul as UL
    rnd = random.Random(73)
    ref = [(int(round(c.real * 2048)), int(round(c.imag * 2048)))
           for c in UL.zc_root(UL.ROOT_U, 139)]
    d = 39                                  # preamble 2 * N_CS 15 + TA 9
    y = []
    for t in range(PRACH_TAPS):
        c = ref[(t + d) % 139]
        n = rnd.randint(-90, 90)
        y.append((c[0] + n, c[1] - n))       # the peak should land on this shift
    return kernel_zc_corr(y, ref[:PRACH_TAPS], name="prach_corr")


# Polar encoder: 16 words of 32 bits = 512 bits = N.  Steps 1..16 are inside a
# word (masked shift+xor), steps 32..256 are word-to-word xor.
NR_POLAR_CYCLES = 40
POLAR_MASKS = (0x55555555, 0x33333333, 0x0F0F0F0F, 0x00FF00FF, 0x0000FFFF)


def kernel_polar_enc(u, n=512):
    import nr_seq as nr
    prog = Program()
    cyc = 0
    for stage, mask in enumerate(POLAR_MASKS):
        step = 1 << stage
        for pe in range(16):
            prog.set(cyc + 0, pe, "ld", dst=0, imm=spad_idx(0, pe))
            prog.set(cyc + 1, pe, "shr", a=0, dst=1, imm=step)
            prog.set(cyc + 2, pe, "and", a=1, b=3 + stage, dst=1)
            prog.set(cyc + 3, pe, "xor", a=0, b=1, dst=0)
            prog.set(cyc + 4, pe, "st", a=0, imm=spad_idx(0, pe))
        cyc += 5
    for stage in range(5, 9):
        d = 1 << (stage - 5)
        for pe in range(16):
            prog.set(cyc + 0, pe, "ld", dst=0, imm=spad_idx(0, pe))
            if (pe & d) == 0:
                prog.set(cyc + 1, pe, "ld", dst=1, imm=spad_idx(0, pe + d))
                prog.set(cyc + 2, pe, "xor", a=0, b=1, dst=0)
                prog.set(cyc + 3, pe, "st", a=0, imm=spad_idx(0, pe))
        cyc += 4
    cycles = cyc + 2

    words = []
    for w in range(16):
        v = 0
        for b in range(32):
            idx = w * 32 + b
            if idx < len(u):
                v |= (u[idx] & 1) << b        # bit b = element w*32+b (LSB-first)
        words.append(v)
    preload = []
    for w in range(16):
        preload.append((spad_addr(0, w), words[w] & 0xFFFFFFFF))
    for stage, mask in enumerate(POLAR_MASKS):
        for pe in range(16):
            preload.append((0x1000 + pe * 32 + (3 + stage) * 4, mask))

    exp = nr.polar_transform(u, n)
    exp_words = []
    for w in range(16):
        v = 0
        for b in range(32):
            v |= (exp[w * 32 + b] & 1) << b
        exp_words.append(v)

    def check(st):
        errs = []
        for w in range(16):
            got = st.get(("spad", spad_addr(0, w)))
            if got != (exp_words[w] & 0xFFFFFFFF):
                errs.append(f"word {w}: got 0x{got:08x} expected 0x{exp_words[w] & 0xFFFFFFFF:08x}")
        return errs

    return Kernel("polar_enc", cycles, prog.build(cycles), preload, [], check,
                  spad_dump=(spad_addr(0, 0), 32 * 32))


def build_polar_enc_kernel() -> Kernel:
    rnd = random.Random(73)
    return kernel_polar_enc([rnd.randint(0, 1) for _ in range(512)])


# Polar SC node update: 16 (a, b, beta) triples -> f (min-sum) and g.
#   a: bank 0 word t   b: bank 1 word t   beta: bank 2 word t
#   f: bank 16 word t  g: bank 17 word t
NR_NODE_CYCLES = 22


def kernel_polar_node(a, b, beta):
    import nr_seq as nr
    prog = Program()
    for pe in range(16):
        prog.set(0, pe, "ld", dst=0, imm=spad_idx(0, pe))
        prog.set(1, pe, "ld", dst=1, imm=spad_idx(1, pe))
        prog.set(2, pe, "ld", dst=2, imm=spad_idx(2, pe))
        prog.set(3, pe, "shr", a=0, dst=3, imm=31)        # sign mask of a
        prog.set(4, pe, "xor", a=0, b=3, dst=4)
        prog.set(5, pe, "sub", a=4, b=3, dst=4)           # |a|
        prog.set(6, pe, "shr", a=1, dst=5, imm=31)        # sign mask of b
        prog.set(7, pe, "xor", a=1, b=5, dst=6)
        prog.set(8, pe, "sub", a=6, b=5, dst=6)           # |b|
        prog.set(9, pe, "min", a=4, b=6, dst=7)           # min(|a|,|b|)
        prog.set(10, pe, "xor", a=3, b=5, dst=3)          # sign(a)*sign(b)
        prog.set(11, pe, "xor", a=7, b=3, dst=4)
        prog.set(12, pe, "sub", a=4, b=3, dst=4)          # f
        prog.set(13, pe, "sub", a="zero", b=2, dst=6)     # -beta
        prog.set(14, pe, "and", a=0, b=6, dst=7)          # beta*a
        prog.set(15, pe, "shl", a=7, dst=7, imm=1)        # 2*beta*a
        prog.set(16, pe, "add", a=1, b=0, dst=6)          # b + a
        prog.set(17, pe, "sub", a=6, b=7, dst=6)          # g
        prog.set(18, pe, "st", a=4, imm=spad_idx(16, pe))
        prog.set(19, pe, "st", a=6, imm=spad_idx(17, pe))

    preload = []
    for t in range(16):
        preload.append((spad_addr(0, t), a[t] & 0xFFFFFFFF))
        preload.append((spad_addr(1, t), b[t] & 0xFFFFFFFF))
        preload.append((spad_addr(2, t), beta[t] & 1))
    exp_f = [nr.f_node(a[t], b[t]) for t in range(16)]
    exp_g = [nr.g_node(a[t], b[t], beta[t]) for t in range(16)]

    def check(st):
        errs = []
        for t in range(16):
            got = st.get(("spad", spad_addr(16, t)))
            if got != (exp_f[t] & 0xFFFFFFFF):
                errs.append(f"f[{t}]: got 0x{got:08x} expected 0x{exp_f[t] & 0xFFFFFFFF:08x}")
            got = st.get(("spad", spad_addr(17, t)))
            if got != (exp_g[t] & 0xFFFFFFFF):
                errs.append(f"g[{t}]: got 0x{got:08x} expected 0x{exp_g[t] & 0xFFFFFFFF:08x}")
        return errs

    return Kernel("polar_node", NR_NODE_CYCLES, prog.build(NR_NODE_CYCLES), preload, [], check,
                  spad_dump=(spad_addr(0, 0), 32 * 32))


def build_polar_node_kernel() -> Kernel:
    rnd = random.Random(74)
    a = [rnd.randint(-1000000, 1000000) for _ in range(16)]
    b = [rnd.randint(-1000000, 1000000) for _ in range(16)]
    beta = [rnd.randint(0, 1) for _ in range(16)]
    return kernel_polar_node(a, b, beta)




if __name__ == "__main__":
    sys.exit(main())
