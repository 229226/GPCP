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
"""Check the L1 receive chain (chnest -> mmse -> LDPC-CN) on the GPCP SoC.

sw/soc/bench/rxchain_bm.c runs one fused 87-cycle microcode image per slot, fed and
drained through the descriptor ring while the array works on the other
scratchpad page.  This script replays the fixed-point chain in Python and checks
both the per-slot results and the scheduling bookkeeping.

Result area (32-bit words):
    [0..191]     4 slots x 16 PEs x {min1, min2, sign}
    [192]        marker 0xCAFEBABE
    [193]        interrupts taken
    [194]        unexpected traps
    [195]        on-chip error count
    [196]        descriptors completed (8)
    [197]        cycles for the whole chain
    [198]        epilogue 0x600D
    [199]        cycles spent waiting for the array
    [200]        cycles spent waiting for descriptors
"""

import sys
from pathlib import Path
from checklib import parse_dump, words32

SLOTS = 4
NPES = 16
WORDS_OUT = 48
LLR_SHIFT = 10
CHAIN_CYCLES = 127   # 15 LS + 20 MMSE + 40 LLR + 52 LDPC-CN


def s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def pseudo(seed, mod):
    """Must match pseudo() in sw/soc/bench/rxchain_bm.c (C '%%' is truncating)."""
    q = abs(seed) // mod
    r = abs(seed) - q * mod
    if seed < 0:
        r = -r
    return r - mod // 2


def fill_slot(s):
    words = []
    for i in range(NPES):
        yr = pseudo(s * 97 + i * 37 + 13, 8001)
        yi = pseudo(s * 53 + i * 29 + 71, 8001)
        xr = 23170 if ((s + i) & 1) else -23170
        xi = 23170 if ((s + i) & 2) else -23170
        words += [yr, yi, xr, xi]
    return words


def golden_slot(s):
    w = fill_slot(s)
    res = []
    for p in range(NPES):
        yr, yi, xr, xi = (sx16(w[p * 4 + k]) for k in range(4))
        hr = (yr * xr + yi * xi) >> 15
        hi = (yi * xr - yr * xi) >> 15
        nr = (hr * yr + hi * yi) >> 15
        ni = (hi * yr - hr * yi) >> 15
        mag = (hr * hr + hi * hi) >> 15

        # 16-QAM max-log demapper, mirroring the microcode (no division)
        msgs = []
        for axis in range(2):
            r = ni if axis else nr
            m = mag
            two_r = s32(r << 1)
            six_r = s32(sx16(two_r) * 3)          # MUL sign-extends operands
            m9 = s32(s32(m << 3) + m)
            mp1, mm1 = s32(m - two_r), s32(m + two_r)
            mp3, mm3 = s32(m9 - six_r), s32(m9 + six_r)
            min_pos = min(mp1, mp3)
            min_neg = min(mm1, mm3)
            llr_msb = s32(min_neg - min_pos) >> LLR_SHIFT
            llr_lsb = s32(min(mm3, mp1) - min(mm1, mp3)) >> LLR_SHIFT
            msgs += [llr_msb, llr_lsb]

        ab = [abs(v) for v in msgs]
        sign = 0
        for v in msgs:
            sign ^= 1 if v < 0 else 0
        s1, h1 = min(ab[0], ab[1]), max(ab[0], ab[1])
        s2, h2 = min(ab[2], ab[3]), max(ab[2], ab[3])
        min1 = min(s1, s2)
        min2 = min(max(s1, s2), min(h1, h2))
        res += [min1 & 0xFFFFFFFF, min2 & 0xFFFFFFFF,
                (0xFFFFFFFF if sign else 0)]
    return res




def main():
    if len(sys.argv) != 2:
        print("usage: check_rxchain.py <dump>")
        return 2
    st = parse_dump(sys.argv[1])
    if st.get("timeout", 1):
        print("[FAIL] simulation timed out")
        return 1

    vals = []
    for w in st.get("words", []):
        vals.append(w & 0xFFFFFFFF)
        vals.append((w >> 32) & 0xFFFFFFFF)

    def word(i):
        return vals[i] if i < len(vals) else None

    errs = []
    for s in range(SLOTS):
        exp = golden_slot(s)
        for i in range(WORDS_OUT):
            got = word(s * WORDS_OUT + i)
            if got != exp[i]:
                p, k = divmod(i, 3)
                name = ("min1", "min2", "sign")[k]
                errs.append(f"slot{s} PE{p} {name}: got {got!r} expected 0x{exp[i]:08x}")

    if word(192) != 0xCAFEBABE:
        errs.append(f"marker: got {word(192)!r} expected 0xcafebabe")
    if word(198) != 0x600D:
        errs.append(f"epilogue: got {word(198)!r} expected 0x600d")
    if word(196) != 8:
        errs.append(f"descriptors completed: got {word(196)!r} expected 8")
    if not word(193):
        errs.append("no completion interrupt was taken")
    if word(194):
        errs.append(f"unexpected traps: {word(194)!r}")
    if word(195) != 0:
        errs.append(f"on-chip error count: {word(195)!r}")
    if st.get("exit_code") != 0x600D:
        errs.append(f"exit_code: 0x{st.get('exit_code', 0):x} expected 0x600d")

    cyc = word(197) or 0
    print(f"[info] cycles={st.get('cycles', '?')} exit_code=0x{st.get('exit_code', 0):08x} "
          f"irqs={word(193)} descs={word(196)}")
    if cyc:
        print(f"[info] chain: {SLOTS} slots in {cyc} cycles "
              f"({cyc / SLOTS:.0f} cycles/slot, {cyc / SLOTS / CHAIN_CYCLES:.2f}x the "
              f"{CHAIN_CYCLES}-cycle array run; "
              f"array-wait={word(199)} ring-wait={word(200)})")
    if errs:
        print(f"[FAIL] {len(errs)} mismatch(es)")
        for e in errs[:10]:
            print("       " + e)
        return 1
    print(f"[ OK ] L1 receive chain (chnest -> mmse -> LDPC-CN): {SLOTS} slots, "
          f"ring drove 8 descriptors, results match the golden model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
