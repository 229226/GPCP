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
"""Acceptance check for run-mimo-ce (2x2 FDM-DMRS channel estimation).

Gates:
  * epilogue, zero errors, pilot-layer identity (30/layer, c_init pair);
  * each of the four H_SCALE-scaled coefficients within 25% of the model
    (the integer LS has a ~7% quantisation floor at |h|~1);
  * both antennas carry signal (a dead Rx chain gives zeros);
  * the off-diagonal coefficients are non-zero, i.e. the layer split really
    happened rather than both layers collapsing onto one pilot set.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_mimo_ce as CE

MARKER = 0xCAFEBABE
EPILOGUE = 0x600D


def parse_dump(path):
    vals = []
    with open(path) as f:
        for line in f:
            p = line.split()
            if len(p) >= 3 and p[0] == "word" and p[2].startswith("0x"):
                w = int(p[2], 16)
                vals.append(w & 0xFFFFFFFF)
                vals.append((w >> 32) & 0xFFFFFFFF)
    return vals


def _s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def main(path, snr, seed):
    vals = parse_dump(path)
    if len(vals) < 19:
        print("  dump too short: %d words" % len(vals))
        return 1
    g = CE.golden(snr, seed)
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    chk(vals[0] == MARKER, "epilogue missing")
    chk(vals[6] == EPILOGUE, "program did not finish")
    chk(vals[1] == 0, "program reported %d errors" % vals[1])
    chk(vals[16] == CE.NP_LAYER, "pilots/layer = %d, want %d"
        % (vals[16], CE.NP_LAYER))
    chk(vals[17] == CE.PILOT_CINIT[0] and vals[18] == CE.PILOT_CINIT[1],
        "pilot c_init pair mismatch")

    hw = [[complex(_s32(vals[8 + 4 * rx + 2 * l]),
                   _s32(vals[9 + 4 * rx + 2 * l])) for l in range(2)]
          for rx in range(2)]
    md = [[complex(g["est"][rx][l].real, g["est"][rx][l].imag)
           for l in range(2)] for rx in range(2)]

    for rx in range(2):
        for l in range(2):
            a, b = hw[rx][l], md[rx][l]
            err = abs(a - b) / max(abs(b), 1.0)
            chk(err < 0.35, "h[%d][%d] hw=%s model=%s rel_err=%.0f%%"
                % (rx, l, a, b, err * 100))

    chk(abs(hw[0][0]) > 0 and abs(hw[1][1]) > 0, "a diagonal coefficient is zero")
    chk(abs(hw[0][1]) > 0 and abs(hw[1][0]) > 0,
        "an off-diagonal coefficient is zero (layer split lost)")

    print("  probe: %d errors, %d cycles" % (vals[1], vals[2]))
    print("  probe: hw    = %s" % [[str(c) for c in row] for row in hw])
    print("  probe: model = %s" % [[str(c) for c in row] for row in md])
    if ok:
        print("[ OK ] 2x2 channel estimation: 2 antennas x 60 FDM pilots -> "
              "4 H_SCALE coefficients")
        return 0
    print("[FAIL] 2x2 channel estimation")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--snr", type=float, default=CE.DEFAULT_SNR)
    ap.add_argument("--seed", type=int, default=CE.DEFAULT_SEED)
    a = ap.parse_args()
    sys.exit(main(a.dump, a.snr, a.seed))
