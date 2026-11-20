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
"""Check the OFDM IFFT + CP gate (sw/soc/bench/ifft_bm.c), gate run-ifft.

Golden model: tools/models/nr_ofdm_tx.py, independently built from ssb_plan_p1's real
SSB grid and fft_plan.planned_fft (the bit-exact fixed-point FFT reference that
run-fft already proved equivalent to hardware).  Two independent comparisons:

  1. raw spectrum [0 .. 2N-1]       vs planned_fft(conjugate input)  -> 0 diff
  2. TX symbol    [2N .. 2N+2(CP+N)-1] vs conj + rev4 + TD_SCALE + CP  -> 0 diff

Result-area contract (32-bit words):
  META+0 marker 0x1FF7A3B1, +1 cycles, +2 array runs, +3 errors, +15 0x600D.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_ofdm_tx as TX                                 # noqa: E402
from checklib import parse_dump, words32                # noqa: E402

N = TX.NFFT
CP = TX.CP_LEN
META = 2 * N + 2 * (CP + N)
NWORDS = META + 16
MARKER = 0x1FF7A3B1
EXPECTED_RUNS = 112          # 48 stage tiles + 64 radix-16 tail blocks


def main():
    if len(sys.argv) != 3:
        print("usage: check_ifft.py <dump> <ifft.td>")
        return 2
    st = parse_dump(sys.argv[1])
    vals = words32(st)
    errs = []

    if st.get("timeout", 1):
        print("[FAIL] simulation timed out")
        return 1
    if len(vals) < NWORDS:
        print(f"[FAIL] dump has {len(vals)} words, need {NWORDS}")
        return 1

    re, im, hdr = TX.read_conj_td(sys.argv[2])
    active = sum(1 for a, b in zip(re, im) if a or b)
    raw_re, raw_im = TX.hw_ifft_raw(re, im)
    exp_td = TX.raw_to_td(raw_re, raw_im)

    bad_raw = 0
    for i in range(N):
        for off, want in ((2 * i, raw_re[i]), (2 * i + 1, raw_im[i])):
            if vals[off] != (want & 0xFFFFFFFF):
                bad_raw += 1
                if len(errs) < 6:
                    errs.append(f"raw[{i}] off={off}: got 0x{vals[off]:08x} "
                                f"want 0x{want & 0xFFFFFFFF:08x}")

    bad_td = 0
    for k in range(CP + N):
        for j, want in enumerate(exp_td[k]):
            off = 2 * N + 2 * k + j
            if vals[off] != (want & 0xFFFFFFFF):
                bad_td += 1
                if len(errs) < 6:
                    errs.append(f"td[{k}] off={off}: got 0x{vals[off]:08x} "
                                f"want 0x{want & 0xFFFFFFFF:08x}")

    def check(cond, msg):
        if not cond:
            errs.append(msg)

    check(vals[META] == MARKER,
          f"marker: got 0x{vals[META]:08x} want 0x{MARKER:08x}")
    check(vals[META + 3] == 0, f"errors/timeouts: {vals[META + 3]}")
    check(vals[META + 2] == EXPECTED_RUNS,
          f"array runs: got {vals[META + 2]} want {EXPECTED_RUNS}")
    check(vals[META + 1] > 0, "cycle counter is zero")
    check(vals[META + 15] == 0x600D,
          f"epilogue: got 0x{vals[META + 15]:08x} want 0x600d")
    check(st.get("exit_code") == 0x600D,
          f"exit_code: 0x{st.get('exit_code', 0):x} want 0x600d")

    print(f"[info] A3: {active} active REs, {vals[META + 1]} cycles over "
          f"{vals[META + 2]} array runs "
          f"({vals[META + 1] // max(vals[META + 2], 1)} cycles/run), "
          f"raw {2 * N} words + TX symbol {2 * (CP + N)} words")

    if bad_raw or bad_td or errs:
        print(f"[FAIL] raw {bad_raw} / symbol {bad_td} differing words, "
              f"{len(errs)} contract error(s)")
        for e in errs[:10]:
            print("       " + e)
        return 1
    print(f"[ OK ] IFFT + CP: {2 * N} raw spectrum words and "
          f"{2 * (CP + N)} TX sample words match the golden model, 0 differences")
    return 0


if __name__ == "__main__":
    sys.exit(main())
