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
"""Shared plumbing for the run-* checkers.

Six checkers (dma / fft / irq / ring / rxchain / slot) carried a byte-identical
copy of parse(); the dump format therefore had six definitions.  It lives here
now.  New checkers should import this instead of copying a neighbour.
"""

from pathlib import Path


def parse_dump(path):
    """Parse a Verilator dump into {words: [u64], cycles, timeout, exit_code}.

    Dump format (one record per line):
        word <index> 0x<hex64>
        cycles <n>
        timeout <0|1>
        exit_code 0x<hex>
    """
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


def words32(st):
    """Flatten 64-bit dump words into 32-bit halves (low half first)."""
    out = []
    for w in st.get("words", []):
        out.append(w & 0xFFFFFFFF)
        out.append((w >> 32) & 0xFFFFFFFF)
    return out
