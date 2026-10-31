#!/usr/bin/env python3
"""Check the interrupt-driven DMA/double-buffering test on the GPCP SoC.

The bare-metal program (sw/soc/irq_bm.c) streams two slots through the CGRA
scratchpad pages and waits for completion *interrupts* (CGRA -> PLIC -> CVA6
machine-external) instead of polling.  This script re-computes the golden
channel estimates and checks both the data and the interrupt bookkeeping.

Layout of the result area (32-bit words):
    [0..31]   slot 0  H[p].re, H[p].im (p = 0..15)
    [32..63]  slot 1
    [64]      marker 0xCAFEBABE
    [65]      interrupts taken
    [66]      spurious / unexpected traps
    [67]      on-chip error count (timeouts + software self-check)
    [68]      slot count
    [69]      epilogue marker 0x600D
"""

import sys
from pathlib import Path

SLOTS = 2
PE = 16


def sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def fill_slot(s):
    """Must match fill_slot() in sw/soc/irq_bm.c."""
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
        res += [(yr * xr + yi * xi) >> 15 & 0xFFFFFFFF,
                (yi * xr - yr * xi) >> 15 & 0xFFFFFFFF]
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
        print("usage: check_irq.py <dump>")
        return 2
    st = parse(sys.argv[1])
    if st.get("timeout", 1):
        print("[FAIL] simulation timed out (wfi never returned?)")
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
                errs.append(f"slot{s} H[{i // 2}].{what}: "
                            f"got {('0x%08x' % got) if got is not None else 'missing'} "
                            f"expected 0x{exp[i]:08x}")

    if word(64) != 0xCAFEBABE:
        errs.append(f"marker: got {word(64)!r} expected 0xcafebabe")
    if word(68) != SLOTS:
        errs.append(f"slot count: got {word(68)!r} expected {SLOTS}")
    if word(69) != 0x600D:
        errs.append(f"epilogue marker: got {word(69)!r} expected 0x600d")
    if not word(65):
        errs.append("no completion interrupt was taken")
    if word(66):
        errs.append(f"unexpected traps: {word(66)!r}")
    if word(67) != 0:
        errs.append(f"on-chip error count: {word(67)!r} (timeout or self-check mismatch)")
    if st.get("exit_code") != 0x600D:
        errs.append(f"exit_code: 0x{st.get('exit_code', 0):x} expected 0x600d")

    print(f"[info] cycles={st.get('cycles', '?')} exit_code=0x{st.get('exit_code', 0):08x} "
          f"irqs={word(65)}")
    if errs:
        print(f"[FAIL] {len(errs)} mismatch(es)")
        for e in errs[:10]:
            print("       " + e)
        return 1
    print(f"[ OK ] interrupt-driven DMA + double buffering: {SLOTS} slots, "
          f"{word(65)} completion interrupt(s), H values match the golden model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
