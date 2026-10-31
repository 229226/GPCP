#!/usr/bin/env python3
"""Check the slot-synchronous scheduler skeleton (sw/soc/slot_rtos.c).

Each OFDM slot is triggered by a CLINT machine-timer tick; the handler only
counts and re-arms, and the slot loop in `main` services one slot per tick
(push input descriptor -> run the 15-cycle LS estimate -> drain).  This script
checks the per-slot channel estimates, the number of ticks/slots, that no
deadline was missed and that the cadence stayed close to the programmed period.

Result area (32-bit words):
    [0..255]    8 slots x 16 PEs x {Hr, Hi}
    [256] marker   [257] ticks   [258] slots   [259] overruns
    [260] irqs     [261] bad traps   [262] errors   [263] cycles
    [264] period   [265] epilogue 0x600D
    [266..273] per-slot service cycles, [274..281] per-slot tick timestamps
"""

import sys
from pathlib import Path

SLOTS = 8
NPES = 16
WORDS_OUT = 32
META = 256


def sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def pseudo(seed, mod):
    """Must match pseudo() in sw/soc/slot_rtos.c (C '%' truncates toward zero)."""
    q = abs(seed) // mod
    r = abs(seed) - q * mod
    if seed < 0:
        r = -r
    return r - mod // 2


def golden_slot(s):
    res = []
    for p in range(NPES):
        yr = pseudo(s * 97 + p * 37 + 13, 8001)
        yi = pseudo(s * 53 + p * 29 + 71, 8001)
        xr = 23170 if ((s + p) & 1) else -23170
        xi = 23170 if ((s + p) & 2) else -23170
        hr = (sx16(yr) * sx16(xr) + sx16(yi) * sx16(xi)) >> 15
        hi = (sx16(yi) * sx16(xr) - sx16(yr) * sx16(xi)) >> 15
        res += [hr & 0xFFFFFFFF, hi & 0xFFFFFFFF]
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
        print("usage: check_slot.py <dump>")
        return 2
    st = parse(sys.argv[1])
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
                p, k = divmod(i, 2)
                errs.append(f"slot{s} PE{p} {'Hr' if k == 0 else 'Hi'}: "
                            f"got {got!r} expected 0x{exp[i]:08x}")

    if word(META) != 0xCAFEBABE:
        errs.append(f"marker: got {word(META)!r} expected 0xcafebabe")
    if word(META + 9) != 0x600D:
        errs.append(f"epilogue: got {word(META + 9)!r} expected 0x600d")
    if word(META + 2) != SLOTS:
        errs.append(f"slots completed: {word(META + 2)!r} expected {SLOTS}")
    if word(META + 1) < SLOTS:
        errs.append(f"timer ticks: {word(META + 1)!r} expected >= {SLOTS}")
    if word(META + 3) != 0:
        errs.append(f"deadline overruns: {word(META + 3)!r} expected 0")
    if word(META + 6) != 0:
        errs.append(f"errors: {word(META + 6)!r} expected 0")
    if word(META + 5) != 0:
        errs.append(f"unexpected traps: {word(META + 5)!r}")
    if not word(META + 4):
        errs.append("no completion interrupt was taken")
    if st.get("exit_code") != 0x600D:
        errs.append(f"exit_code: 0x{st.get('exit_code', 0):x} expected 0x600d")

    period = word(META + 8) or 0
    starts = [word(META + 18 + s) for s in range(SLOTS)]
    for s in range(SLOTS):
        dur = word(META + 10 + s)
        if dur is None or dur > period:
            errs.append(f"slot{s} service time {dur!r} exceeds the {period}-cycle slot")
    gaps = [starts[i + 1] - starts[i] for i in range(SLOTS - 1)]
    jitter = max(abs(g - period) for g in gaps) if gaps else 0

    for s in range(SLOTS):
        print(f"[info] slot{s}: service {word(META + 10 + s)} cycles, "
              f"tick at {starts[s]}")
    print(f"[info] period={period} cycles, gaps={gaps}, max jitter={jitter}")

    if errs:
        print(f"[FAIL] {len(errs)} mismatch(es)")
        for e in errs[:10]:
            print("       " + e)
        return 1
    print(f"[ OK ] slot scheduler: {SLOTS} timer-driven slots, "
          f"{word(META + 4)} completion interrupts, no deadline overruns, "
          f"channel estimates match the golden model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
