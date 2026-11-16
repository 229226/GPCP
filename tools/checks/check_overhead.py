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
"""Check T0 (sw/soc/bench/overhead_bm.c): fit T(c) = a + b*c for one array run.

The bench brackets the unmodified rt_array_run() with mtime() over five cycle
counts and 64 repetitions each.  `a` is the fixed start/poll cost, `b` the
per-cycle cost.  run-ifft measures 1938 cycles/run including the DMA descriptor
ring, so the residual is the DMA/orchestration share.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from checklib import parse_dump, words32              # noqa: E402

NPTS = 5
REPS = 64
META = 24
MARKER = 0x0FF1C1A1
IFIT_CYCLES_PER_RUN = 1938       # run-ifft: 217098 cycles / 112 runs
IFIT_IMEM_CYCLES = 448           # stage image (block image is 336)


def fit(xs, ys):
    n = len(xs)
    xm = sum(xs) / n
    ym = sum(ys) / n
    sxx = sum((x - xm) ** 2 for x in xs)
    b = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / sxx
    a = ym - b * xm
    ss_tot = sum((y - ym) ** 2 for y in ys)
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else 1.0
    return a, b, r2


def main():
    if len(sys.argv) != 2:
        print("usage: check_overhead.py <dump>")
        return 2
    st = parse_dump(sys.argv[1])
    v = words32(st)
    errs = []
    if st.get("timeout", 1):
        print("[FAIL] simulation timed out")
        return 1
    if len(v) < META:
        print(f"[FAIL] dump has {len(v)} words, need {META}")
        return 1
    if v[0] != MARKER:
        errs.append(f"marker: got 0x{v[0]:08x} want 0x{MARKER:08x}")
    if v[1] != REPS or v[2] != NPTS:
        errs.append(f"reps/points: got {v[1]}/{v[2]} want {REPS}/{NPTS}")
    if v[META - 1] != 0x600D:
        errs.append(f"epilogue: got 0x{v[META - 1]:08x} want 0x600d")
    if st.get("exit_code") != 0x600D:
        errs.append(f"exit_code: 0x{st.get('exit_code', 0):x} want 0x600d")

    pts = []
    for p in range(NPTS):
        c, t, bad = v[4 + 3 * p], v[5 + 3 * p], v[6 + 3 * p]
        pts.append((c, t, bad))
        if bad:
            errs.append(f"point c={c}: {bad} array-run timeouts")
        if t <= c:
            errs.append(f"point c={c}: measured {t} cycles <= program length")

    a, b, r2 = fit([c for c, _, _ in pts], [t for _, t, _ in pts])
    if not (0.5 <= b <= 3.0):
        errs.append(f"per-cycle cost {b:.3f} outside [0.5, 3.0]")
    if a < 0 or a > 4000:
        errs.append(f"fixed overhead {a:.1f} outside [0, 4000]")
    if r2 < 0.90:
        errs.append(f"linear fit R^2={r2:.4f} < 0.90")

    for c, t, _ in pts:
        print(f"[info] cycles={c:4d}  measured={t:5d}  "
              f"fit={a + b * c:7.1f}  overhead={t - c:5d}")
    dma = IFIT_CYCLES_PER_RUN - (a + b * IFIT_IMEM_CYCLES)
    print(f"[info] T(c) = {a:.1f} + {b:.4f}*c  (R^2={r2:.5f}); "
          f"run-ifft {IFIT_CYCLES_PER_RUN} cycles/run minus "
          f"{a:.0f}+{b:.2f}*{IFIT_IMEM_CYCLES} for the stage image "
          f"-> ~{dma:.0f} cycles/run of DMA + page orchestration")

    if errs:
        print(f"[FAIL] {len(errs)} contract error(s)")
        for e in errs[:8]:
            print("       " + e)
        return 1
    print(f"[ OK ] T0: fixed overhead {a:.0f} cycles/run, "
          f"{b:.3f} cycles per IMEM cycle, {REPS} repetitions x {NPTS} points")
    return 0


if __name__ == "__main__":
    sys.exit(main())
