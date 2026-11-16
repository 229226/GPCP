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
"""Value-by-value check of one variable block through the BN microcode."""
import sys

ZC, BNSLOT, SEED = 20, 24, 0xB1


def vals_in():
    s, out = SEED, []
    for _ in range((BNSLOT + 2) * ZC):
        s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
        out.append(((s >> 16) & 0x3FF) - 512)
    return out


def main(path):
    words = []
    for line in open(path):
        p = line.split()
        if len(p) >= 3 and p[0] == "word" and p[2].startswith("0x"):
            w = int(p[2], 16)
            words.append(w & 0xFFFFFFFF)
            words.append((w >> 32) & 0xFFFFFFFF)
    if len(words) < 8 + (BNSLOT + 2) * ZC:
        print(f"[FAIL] short dump ({len(words)} words)")
        return 1
    src = vals_in()
    inp = [src[k * ZC:(k + 1) * ZC] for k in range(BNSLOT + 2)]
    want = [row[:] for row in inp]
    for i in range(ZC):
        v = inp[BNSLOT][i] + sum(inp[k][i] for k in range(BNSLOT))
        want[BNSLOT + 1][i] = -1 if v < 0 else 0
        for k in range(BNSLOT):
            want[k][i] = v - inp[k][i]
    bad = 0
    for k in range(BNSLOT + 2):
        for i in range(ZC):
            got = words[8 + k * ZC + i]
            exp = want[k][i] & 0xFFFFFFFF
            if got != exp:
                bad += 1
                if bad <= 6:
                    print(f"[diff] bank {k} word {i}: got 0x{got:08x} "
                          f"({got if got < 2**31 else got - 2**32}) "
                          f"want 0x{exp:08x} ({want[k][i]})")
    print(f"[info] runs {words[1]}, DMA {words[2]}, errors {words[3]}")
    if bad:
        print(f"[FAIL] BN block: {bad} of {(BNSLOT + 2) * ZC} values differ")
        return 1
    print(f"[ OK ] BN microcode: all {(BNSLOT + 2) * ZC} values (24 messages, "
          f"LLR, hard) match the golden model")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "pdsch_bn1_dump.txt"))
