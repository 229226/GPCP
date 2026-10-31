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


def build_ldpc_kernel() -> Kernel:
    rnd = random.Random(41)
    a = [rnd.randint(-300, 300) for _ in range(16)]
    b = [rnd.randint(-300, 300) for _ in range(16)]
    c = [rnd.randint(-300, 300) for _ in range(16)]
    d = [rnd.randint(-300, 300) for _ in range(16)]
    return kernel_ldpc_cn(a, b, c, d)


# ---------------------------------------------------------------- driver
def find_verilator() -> str:
    if os.environ.get("VERILATOR"):
        return os.environ["VERILATOR"]
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
    return {"prog": prog, "preload": pre, "din": din, "dump": d / "dump.txt", "dir": d}


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


KERNELS = {"matmul": build_matmul_kernel, "fir16": build_fir_kernel,
           "fft16": build_fft_kernel, "chnest16": build_chnest_kernel,
           "mmse16": build_mmse_kernel, "ldpc_cn": build_ldpc_kernel}


def main() -> int:
    ap = argparse.ArgumentParser(description="GPCP CGRA M0 toolchain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--force", action="store_true")
    p_test = sub.add_parser("test")
    p_test.add_argument("--kernel", default="all", choices=["all"] + list(KERNELS))
    p_test.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    if args.cmd == "build":
        build_sim(force=args.force)
        return 0

    build_sim(force=getattr(args, "rebuild", False))
    names = list(KERNELS) if args.kernel == "all" else [args.kernel]
    ok = True
    for n in names:
        ok &= run_kernel(KERNELS[n]())
    print("ALL TESTS PASSED" if ok else "SOME TESTS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
