#!/usr/bin/env python3
"""Acceptance check for run-csi (CSI-RS + CQI report on the SoC).

Gates:
  * epilogue, zero internal errors, NCSI / c_init identity;
  * SNR estimate within 2 dB of the model (0.5-1.5 dB in practice, the
    residual is the finite-sample bias of an 80-point estimator);
  * CQI within +-1 of the model (the fixed-point estimate may straddle a
    threshold; a wrong front end moves it by many steps);
  * the sign of the first four LS estimates must match;
  * Ps and Pn must be non-zero (a dead FFT/LS shows up here immediately).
"""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_csi as C

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
    if len(vals) < 20:
        print("  dump too short: %d words" % len(vals))
        return 1
    g = C.golden(snr, seed)
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    chk(vals[0] == MARKER, "epilogue missing: 0x%08x" % vals[0])
    chk(vals[6] == EPILOGUE, "program did not finish: 0x%08x" % vals[6])
    chk(vals[1] == 0, "program reported %d errors" % vals[1])
    chk(vals[18] == C.NCSI, "NCSI = %d, want %d" % (vals[18], C.NCSI))
    chk(vals[19] == C.csi_cinit(), "c_init = 0x%08x, want 0x%08x"
        % (vals[19], C.csi_cinit()))
    chk(vals[8] != 0 and vals[9] != 0, "Ps or Pn is zero (dead FFT/LS)")

    snr_hw = 10.0 * math.log10(vals[7] / 256.0) if vals[7] else -99.0
    chk(abs(snr_hw - g["snr_db"]) < 2.0,
        "SNR estimate %.2f dB vs model %.2f dB" % (snr_hw, g["snr_db"]))
    chk(abs(vals[5] - g["cqi"]) <= 1,
        "CQI %d vs model %d" % (vals[5], g["cqi"]))

    sign_bad = []
    for i in range(4):
        for k in (0, 1):
            hw = _s32(vals[10 + 2 * i + k])
            md = g["a"][i].real if k == 0 else g["a"][i].imag
            if (hw > 0) != (md > 0):
                sign_bad.append((i, k))
    chk(not sign_bad, "LS estimate sign mismatches at %s" % sign_bad)

    print("  probe: %d errors, %d cycles" % (vals[1], vals[2]))
    print("  probe: SNR hw=%.2f dB model=%.2f dB (true %.1f), CQI hw=%d model=%d"
          % (snr_hw, g["snr_db"], snr, vals[5], g["cqi"]))
    print("  probe: Ps>>40=%d Pn>>10=%d, A[0]=%s"
          % (vals[8], vals[9],
             complex(_s32(vals[10]), _s32(vals[11]))))
    if ok:
        print("[ OK ] CSI-RS + CQI: FFT -> 80 CSI-RS REs -> LS -> "
              "difference noise -> SNR -> CQI %d" % vals[5])
        return 0
    print("[FAIL] CSI-RS / CQI acceptance")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--snr", type=float, default=C.DEFAULT_SNR)
    ap.add_argument("--seed", type=int, default=C.DEFAULT_SEED)
    a = ap.parse_args()
    sys.exit(main(a.dump, a.snr, a.seed))
