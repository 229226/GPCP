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
"""Check the multi-stage FFT on the GPCP SoC (256 / 1024 / 4096 points).

sw/soc/bench/fft_bm.c runs every full radix-4 stage tile by tile through the
descriptor ring (double buffered pages) and finishes each size with a fused
64-point block kernel (the radix-16 tail).  This script rebuilds the same
fixed-point transform from tools/fft_plan.py, compares every output word and
checks the scheduling bookkeeping.

Result area (32-bit words):
    [0..511]      FFT-256   (256 points, 2 words each, g-order)
    [512..2559]   FFT-1024
    [2560..10751] FFT-4096
    [10752] marker 0xCAFEBABE   [10753..10755] cycles per size
    [10756] array runs          [10757] descriptors completed
    [10758] errors              [10759] epilogue 0x600D
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fft_plan as P   # noqa: E402
from checklib import parse_dump, words32

SIZES = (256, 1024, 4096)
SEEDS = {256: 0x1357, 1024: 0x2468, 4096: 0x369C}
OFFSET = {256: 0, 1024: 512, 4096: 2560}
META = 10752


def rnd_seq(seed, n, mod=3000):
    """Mirror rnd() in sw/soc/bench/fft_bm.c."""
    rng = seed & 0xFFFFFFFF
    out = []
    for _ in range(2 * n):
        rng = (rng * 1664525 + 1013904223) & 0xFFFFFFFF
        out.append((rng >> 16) % (2 * mod + 1) - mod)
    return out


def golden(n):
    vals = rnd_seq(SEEDS[n], n)
    re = vals[0::2]
    im = vals[1::2]
    gr, gi = P.reference_fft(re, im, n)
    fr, fi = P.float_fft(re, im, n)
    return re, im, gr, gi, fr, fi




def main():
    if len(sys.argv) != 2:
        print("usage: check_fft.py <dump>")
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
    stats = []
    for n in SIZES:
        re, im, gr, gi, fr, fi = golden(n)
        base = OFFSET[n]
        bad = 0
        for i in range(n):
            got_r = word(base + 2 * i)
            got_i = word(base + 2 * i + 1)
            if got_r != (gr[i] & 0xFFFFFFFF):
                if bad < 3:
                    errs.append(f"N={n} X[{i}].re: got 0x{got_r:08x} "
                                f"expected 0x{gr[i] & 0xFFFFFFFF:08x}")
                bad += 1
            if got_i != (gi[i] & 0xFFFFFFFF):
                if bad < 3:
                    errs.append(f"N={n} X[{i}].im: got 0x{got_i:08x} "
                                f"expected 0x{gi[i] & 0xFFFFFFFF:08x}")
                bad += 1
        # independent precision reference (float, same schedule)
        rms = math.sqrt(sum((fr[i] - gr[i]) ** 2 + (fi[i] - gi[i]) ** 2
                            for i in range(n)) / n)
        mag = math.sqrt(sum(fr[i] ** 2 + fi[i] ** 2 for i in range(n)) / n)
        snr = 20.0 * math.log10(mag / max(rms, 1e-9))
        stats.append((n, bad, snr))

    if word(META) != 0xCAFEBABE:
        errs.append(f"marker: got {word(META)!r} expected 0xcafebabe")
    if word(META + 7) != 0x600D:
        errs.append(f"epilogue: got {word(META + 7)!r} expected 0x600d")
    complete = (1 + 8 + 48) + (4 + 16 + 64)          # tiles: full stages + tails
    if word(META + 4) != complete:
        errs.append(f"array runs: got {word(META + 4)!r} expected {complete}")
    n_desc = (1 * 9 + 2 * 4 * 9 + 3 * 16 * 9) + 2 * (4 + 16 + 64)
    if word(META + 5) != n_desc:
        errs.append(f"descriptors: got {word(META + 5)!r} expected {n_desc}")
    if word(META + 6) != 0:
        errs.append(f"errors/timeouts: {word(META + 6)!r}")
    if st.get("exit_code") != 0x600D:
        errs.append(f"exit_code: 0x{st.get('exit_code', 0):x} expected 0x600d")

    for (n, bad, snr) in stats:
        cyc = word(META + 1 + SIZES.index(n)) or 0
        runs = P.tile_count(n) * P.full_stage_count(n) + P.block_count(n)
        print(f"[info] N={n:4d}: {n - bad}/{n} points, {cyc} cycles over {runs} "
              f"array runs ({cyc / max(runs, 1):.0f} cycles/run, "
              f"{cyc / n:.1f} cycles/point), SNR {snr:.1f} dB")

    if errs:
        print(f"[FAIL] {len(errs)} mismatch(es)")
        for e in errs[:10]:
            print("       " + e)
        return 1
    print(f"[ OK ] FFT 256/1024/4096: digit-reversed spectra match the golden "
          f"model bit-exactly, {word(META + 5)} descriptors, "
          f"{word(META + 4)} array runs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
