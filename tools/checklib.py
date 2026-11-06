#!/usr/bin/env python3
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
