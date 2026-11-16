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
"""Compare the SoC simulation dump against the golden matrix product."""

import re
import sys
from pathlib import Path

A = [[1, 2, 3, 4],
     [5, 6, 7, 8],
     [9, 1, 2, 3],
     [4, 5, 6, 7]]

B = [[1, 0, -1, 2],
     [3, 2, 1, 0],
     [-2, 1, 4, -3],
     [0, 5, 2, 1]]


def sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def expected():
    out = []
    for i in range(4):
        for j in range(4):
            out.append(sum(sx16(A[i][k]) * sx16(B[k][j]) for k in range(4)) & 0xFFFFFFFF)
    return out


def parse(path):
    st = {}
    for line in Path(path).read_text().splitlines():
        t = line.split()
        if t and t[0] == "word":
            st[int(t[1])] = int(t[2], 16)
        elif t and t[0] == "cycles":
            st["cycles"] = int(t[1])
        elif t and t[0] == "timeout":
            st["timeout"] = int(t[1])
        elif t and t[0] == "exit_code":
            st["exit_code"] = int(t[1], 16)
    return st


def main():
    if len(sys.argv) != 2:
        print("usage: check_soc.py <dump>")
        return 2
    st = parse(sys.argv[1])
    if st.get("timeout", 1):
        print("[FAIL] simulation timed out")
        return 1

    errs = []
    # results are 32-bit words, packed two per 64-bit memory word
    vals = []
    for i in range(12):
        w = st.get(i, 0)
        vals.append(w & 0xFFFFFFFF)
        vals.append((w >> 32) & 0xFFFFFFFF)

    exp = expected()
    for pe, e in enumerate(exp):
        got = vals[pe] if pe < len(vals) else 0
        if got != e:
            errs.append(f"C[{pe // 4}][{pe % 4}]: got 0x{got:08x} expected 0x{e:08x}")

    if vals[16] != 0xCAFEBABE:
        errs.append(f"marker: got 0x{vals[16]:08x} expected 0xcafebabe")
    if vals[18] != 0x600D:
        errs.append(f"epilogue marker: got 0x{vals[18]:08x} expected 0x600d")

    guard = vals[17]
    if guard:
        print(f"[warn] CGRA poll loop spun {guard} times")

    print(f"[info] exit_code=0x{st.get('exit_code', 0):08x} cycles={st.get('cycles', '?')}")
    if errs:
        print(f"[FAIL] {len(errs)} mismatch(es)")
        for e in errs[:10]:
            print("       " + e)
        return 1
    print("[ OK ] CVA6 -> CGRA matmul matches the golden model")
    return 0


if __name__ == "__main__":
    sys.exit(main())
