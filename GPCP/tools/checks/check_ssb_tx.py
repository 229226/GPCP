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
"""Check A4's four-symbol SSB transmitter and export the loopback TD.

Golden model: tools/models/nr_ofdm_tx.py::ssb_tx_expected(), i.e. the same
fft_plan.planned_fft fixed-point reference used by run-fft / run-ifft, applied
to each of the four P1 SSB grids independently.  On success the verified
result-area words are written verbatim as the P1 binary TD
(ssb_plan_p1.dump_td_bin layout) that ssb_fid_bm.bin consumes -- so the
loopback is driven by *hardware-generated* samples, not by a model dump.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_ofdm_tx as TX                                 # noqa: E402
from checklib import parse_dump, words32                # noqa: E402

N = TX.NFFT
CP = TX.CP_LEN
NSYM = TX.NSYM
SYM_WORDS = 2 * (CP + N)
META = NSYM * SYM_WORDS
NWORDS = META + 16
MARKER = 0x1FF7A4B1
EXPECTED_RUNS = NSYM * 112


def main():
    if len(sys.argv) != 4:
        print("usage: check_ssb_tx.py <dump> <conj.td> <loop.td>")
        return 2
    st = parse_dump(sys.argv[1])
    v = words32(st)
    errs = []
    if st.get("timeout", 1):
        print("[FAIL] simulation timed out")
        return 1
    if len(v) < NWORDS:
        print(f"[FAIL] dump has {len(v)} words, need {NWORDS}")
        return 1

    re, im, hdr = TX.read_conj_td(sys.argv[2], magic=TX.MAGIC4)
    assert hdr[4] == NSYM
    exp = TX.ssb_tx_expected(re, im, NSYM)

    bad = 0
    for k, (er, ei) in enumerate(exp):
        for j, want in enumerate((er, ei)):
            off = 2 * k + j
            if v[off] != (want & 0xFFFFFFFF):
                bad += 1
                if len(errs) < 6:
                    errs.append(f"symbol {k // (CP + N)} sample "
                                f"{k % (CP + N)} off={off}: got 0x{v[off]:08x} "
                                f"want 0x{want & 0xFFFFFFFF:08x}")

    def check(cond, msg):
        if not cond:
            errs.append(msg)

    check(v[META] == MARKER, f"marker: got 0x{v[META]:08x} want 0x{MARKER:08x}")
    check(v[META + 3] == 0, f"errors/timeouts: {v[META + 3]}")
    check(v[META + 2] == EXPECTED_RUNS,
          f"array runs: got {v[META + 2]} want {EXPECTED_RUNS}")
    check(v[META + 1] > 0, "cycle counter is zero")
    check(v[META + 15] == 0x600D,
          f"epilogue: got 0x{v[META + 15]:08x} want 0x600d")
    check(st.get("exit_code") == 0x600D,
          f"exit_code: 0x{st.get('exit_code', 0):x} want 0x600d")

    if bad or errs:
        print(f"[FAIL] {bad} differing sample words, {len(errs)} contract error(s)")
        for e in errs[:10]:
            print("       " + e)
        return 1

    # P1 TD layout: NSYM x (CP+N) complex int32 pairs, re then im
    os.makedirs(os.path.dirname(sys.argv[3]) or ".", exist_ok=True)
    with open(sys.argv[3], "wb") as f:
        for w in v[:META]:
            f.write(struct.pack("<I", w & 0xFFFFFFFF))
    print(f"[info] A4 TX: {NSYM} symbols x {CP + N} samples = {META} words, "
          f"{v[META + 1]} cycles over {v[META + 2]} array runs; "
          f"wrote hardware-generated loopback TD {sys.argv[3]}")
    print(f"[ OK ] 4-symbol SSB TX: all {META} sample words match the golden "
          f"model, 0 differences")
    return 0


if __name__ == "__main__":
    sys.exit(main())
