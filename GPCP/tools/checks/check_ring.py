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
"""Check the descriptor-ring + burst-copy test on the GPCP SoC.

sw/soc/bench/ring_bm.c programs a ring in RAM; the copy engine walks it, writes each
descriptor's status word and advances RING_HEAD on its own.  This script
recomputes the golden channel estimates and checks the ring bookkeeping.

Layout of the result area (32-bit words):
    [0..31]   slot 0 H[p].re, H[p].im        [32..63] slot 1
    [64]      marker 0xCAFEBABE
    [65]      interrupts taken
    [66]      spurious / unexpected traps
    [67]      on-chip error count
    [68]      RING_HEAD at the end (must equal tail = 4)
    [69]      epilogue marker 0x600D
    [70..73]  descriptor status words 0..3 (0xC0DE0000 | len)
    [74]      RING_CTRL snapshot
    [75]      cycles for the 2 input descriptors (2 x 64 words)
    [76]      cycles for the 2 output descriptors (2 x 32 words)
    [77]      cycles for one linear 1024-word copy (throughput probe)
"""

import sys
from pathlib import Path
from checklib import parse_dump, words32

SLOTS = 2
PE = 16
LENS = [64, 64, 32, 32]      # words per descriptor, in ring order


def sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def fill_slot(s):
    """Must match fill_slot() in sw/soc/bench/ring_bm.c."""
    words = []
    for i in range(64):
        v = ((s * 31 + i * 17 + 11) % 20001) - 10000
        k = i & 3
        words.append((23170 if v >= 0 else -23170) if k >= 2 else v)
    return words


def golden(s):
    w = fill_slot(s)
    res = []
    for p in range(PE):
        yr, yi, xr, xi = (sx16(w[p * 4 + k]) for k in range(4))
        res += [((yr * xr + yi * xi) >> 15) & 0xFFFFFFFF,
                ((yi * xr - yr * xi) >> 15) & 0xFFFFFFFF]
    return res




def main():
    if len(sys.argv) != 2:
        print("usage: check_ring.py <dump>")
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
        exp = golden(s)
        for i in range(2 * PE):
            got = word(s * 32 + i)
            if got != exp[i]:
                what = "re" if i % 2 == 0 else "im"
                errs.append(f"slot{s} H[{i // 2}].{what}: got {got!r} expected 0x{exp[i]:08x}")

    if word(64) != 0xCAFEBABE:
        errs.append(f"marker: got {word(64)!r} expected 0xcafebabe")
    if word(69) != 0x600D:
        errs.append(f"epilogue: got {word(69)!r} expected 0x600d")
    if word(68) != 4:
        errs.append(f"RING_HEAD: got {word(68)!r} expected 4 (ring fully consumed)")
    if not word(65):
        errs.append("no completion interrupt was taken")
    if word(66):
        errs.append(f"unexpected traps: {word(66)!r}")
    if word(67) != 0:
        errs.append(f"on-chip error count: {word(67)!r}")
    for d, ln in enumerate(LENS):
        got = word(70 + d)
        exp = 0xC0DE0000 | ln
        if got != exp:
            errs.append(f"descriptor {d} status: got {got!r} expected 0x{exp:08x}")
    # RING_CTRL: enabled | idle | !busy
    if word(74) != 0x3:
        errs.append(f"RING_CTRL: got {word(74)!r} expected 0x3 (enabled+idle)")
    if st.get("exit_code") != 0x600D:
        errs.append(f"exit_code: 0x{st.get('exit_code', 0):x} expected 0x600d")

    print(f"[info] cycles={st.get('cycles', '?')} exit_code=0x{st.get('exit_code', 0):08x} "
          f"irqs={word(65)} head={word(68)}")
    if word(77):
        print(f"[info] copy timing: 2*64 word in = {word(75)} cycles, "
              f"2*32 word out = {word(76)} cycles, "
              f"1024 word linear = {word(77)} cycles "
              f"({word(77) / 1024.0:.2f} cycles/word)")
    if errs:
        print(f"[FAIL] {len(errs)} mismatch(es)")
        for e in errs[:10]:
            print("       " + e)
        return 1
    print(f"[ OK ] descriptor ring + burst copy: {len(LENS)} descriptors consumed, "
          f"{word(65)} interrupts, H values match the golden model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
