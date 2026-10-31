#!/usr/bin/env python3
"""Check the bare-metal DMA + double-buffering test on the GPCP SoC.

The RTL run (sw/soc/dma_bm.c) streams three slots through the CGRA scratchpad
pages with the copy engine and copies the results back to RAM.  This script
recomputes the channel-estimate golden model from the same deterministic
input pattern used by the bare-metal program and compares the 96 results.

Layout of the result area (32-bit words):
    [0..31]   slot 0  H[p].re, H[p].im (p = 0..15)
    [32..63]  slot 1
    [64..95]  slot 2
    [96]      marker 0xCAFEBABE
    [97]      on-chip error count (DMA timeouts + software self-check)
    [98]      slot count
    [99]      epilogue marker 0x600D
"""

import sys
from pathlib import Path

SLOTS = 3
PE = 16


def sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def fill_slot(s):
    """Must match fill_slot() in sw/soc/dma_bm.c."""
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
        hre = (yr * xr + yi * xi) >> 15
        him = (yi * xr - yr * xi) >> 15
        res += [hre & 0xFFFFFFFF, him & 0xFFFFFFFF]
    return res


def parse(path):
    st = {}
    for line in Path(path).read_text().splitlines():
        t = line.split()
        if not t:
            continue
        if t[0] == "word":
            st.setdefault("words", []).append(int(t[2], 16))
        elif t[0] == "cycles":
            st["cycles"] = int(t[1])
        elif t[0] == "timeout":
            st["timeout"] = int(t[1])
        elif t[0] == "exit_code":
            st["exit_code"] = int(t[1], 16)
    return st


def main():
    if len(sys.argv) != 2:
        print("usage: check_dma.py <dump>")
        return 2
    st = parse(sys.argv[1])
    if st.get("timeout", 1):
        print("[FAIL] simulation timed out")
        return 1

    vals = []
    for w in st.get("words", []):
        vals.append(w & 0xFFFFFFFF)
        vals.append((w >> 32) & 0xFFFFFFFF)

    errs = []
    for s in range(SLOTS):
        exp = golden(s)
        for i in range(2 * PE):
            got = vals[s * 32 + i] if s * 32 + i < len(vals) else None
            if got != exp[i]:
                what = "re" if i % 2 == 0 else "im"
                errs.append(f"slot{s} H[{i // 2}].{what}: "
                            f"got {('0x%08x' % got) if got is not None else 'missing'} "
                            f"expected 0x{exp[i]:08x}")

    def word(i):
        return vals[i] if i < len(vals) else None

    if word(96) != 0xCAFEBABE:
        errs.append(f"marker: got {word(96)!r} expected 0xcafebabe")
    if word(98) != SLOTS:
        errs.append(f"slot count: got {word(98)!r} expected {SLOTS}")
    if word(99) != 0x600D:
        errs.append(f"epilogue marker: got {word(99)!r} expected 0x600d")
    if word(97) != 0:
        errs.append(f"on-chip error count: {word(97)!r} (DMA timeout or self-check mismatch)")
    if st.get("exit_code") != 0x600D:
        errs.append(f"exit_code: 0x{st.get('exit_code', 0):x} expected 0x600d")

    print(f"[info] cycles={st.get('cycles', '?')} exit_code=0x{st.get('exit_code', 0):08x}")
    if errs:
        print(f"[FAIL] {len(errs)} mismatch(es)")
        for e in errs[:10]:
            print("       " + e)
        return 1
    print(f"[ OK ] DMA + double buffering: {SLOTS} slots of {2 * PE} H values "
          f"match the golden model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
