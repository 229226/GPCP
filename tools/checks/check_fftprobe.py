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
"""Check the 4096-point FFT *port* used by the sample-level SSB chain
(sw/soc/lib/ssb_fft.inc, program sw/soc/bench/fftprobe_bm.c).

The SSB program includes the FFT engine as a header, so a regression in that
port cannot be caught by the PSS decision alone (the earlier port used a binary
bit-reversal for the output index while the engine is radix-4, i.e. the index
must be reversed in base 4).  This script compares every exported bin with
tools/fft_plan.py's fixed-point reference for the same input, bit-exactly.

Result area: [0..8191] the 4096 complex bins as X[k] = data[rev4(k)],
             [8192] epilogue 0x600D.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fft_plan as P      # noqa: E402
import ssb_plan as sp     # noqa: E402

N = 4096


def sx(v):
    return v - 0x100000000 if v & 0x80000000 else v


def main(path):
    vals = []
    for line in Path(path).read_text().splitlines():
        t = line.split()
        if len(t) >= 3 and t[0] == "word" and t[2].startswith("0x"):
            w = int(t[2], 16)
            vals.append(w & 0xFFFFFFFF)
            vals.append((w >> 32) & 0xFFFFFFFF)
    if len(vals) < 2 * N:
        print(f"  dump too short: {len(vals)} words")
        return 1
    if vals[2 * N] != 0x600D:
        print(f"  epilogue missing: 0x{vals[2 * N]:08x}")
        return 1

    td, _grid, _payload = sp.build_td()
    re = [int(c.real) * sp.TD_SCALE for c in td[1]]
    im = [int(c.imag) * sp.TD_SCALE for c in td[1]]
    gr, gi = P.reference_fft(re, im, N)
    gr = [sx(v & 0xFFFFFFFF) for v in gr]
    gi = [sx(v & 0xFFFFFFFF) for v in gi]
    # reference_fft returns the array in engine order (position i holds
    # X[rev4(i)]), so bin k lives at position rev4(k)
    pos = P.digit_reversed(N)

    bad = []
    for k in range(N):
        if (vals[2 * k] != (gr[pos[k]] & 0xFFFFFFFF)
                or vals[2 * k + 1] != (gi[pos[k]] & 0xFFFFFFFF)):
            if len(bad) < 8:
                bad.append((k, vals[2 * k], gr[pos[k]]))
    count = sum(1 for k in range(N)
                if vals[2 * k] != (gr[pos[k]] & 0xFFFFFFFF)
                or vals[2 * k + 1] != (gi[pos[k]] & 0xFFFFFFFF))
    if count:
        print(f"  {count}/{N} bins differ from the fixed-point reference, first {bad[:3]}")
        return 1
    peak = max(abs(complex(a, b)) for a, b in zip(gr, gi))
    print(f"[info] FFT port: {N} bins bit-exact against the fixed-point reference "
          f"(peak |X| = {peak:.0f}, TD_SCALE = {sp.TD_SCALE})")
    print("[ OK ] 4096-point FFT port (rev4 index order, TD_SCALE pre-scaling)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "sim/soc/build/fftprobe_dump.txt"))
