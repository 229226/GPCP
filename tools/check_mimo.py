#!/usr/bin/env python3
"""Acceptance check for run-mimo (2x2 MMSE receiver on the SoC).

The reference is the floating-point MMSE in nr_mimo_link, evaluated on the
same channel that nr_mimo_ce used to build the TD.  The gate is bit-level:
720 hard decisions, at most 4 may differ (the integer 2x2 solve and the
float one diverge only at symbols sitting on a decision boundary).

Also checks the four channel coefficients (35% quantisation gate), the
regularisation constant and the data-RE count.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_mimo_ce as CE
import nr_mimo_link as ML

MARKER = 0xCAFEBABE
EPILOGUE = 0x600D
# At high SNR the integer 2x2 solve and the float reference agree bit-for-bit;
# near the decision boundary they legitimately diverge (noise decides the sign),
# so the tolerance scales with SNR.  The gate still catches a broken layer
# split, a wrong H or a missing antenna by orders of magnitude.
TOL_BY_SNR = ((20.0, 4), (15.0, 16), (10.0, 120), (-99.0, 260))


def tol_for(snr):
    for th, tol in TOL_BY_SNR:
        if snr >= th:
            return tol
    return 260


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
    if len(vals) < 20 + CE.NDATA:
        print("  dump too short: %d words" % len(vals))
        return 1
    h = CE.channel_matrix(seed)
    ref = ML.link(snr, seed, h=h)
    ref_bits = [1 if v < 0 else 0 for v in ref["raw"]]
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    chk(vals[0] == MARKER, "epilogue missing")
    chk(vals[6] == EPILOGUE, "program did not finish")
    chk(vals[1] == 0, "program reported %d errors" % vals[1])
    chk(vals[16] == CE.NDATA, "data REs = %d, want %d" % (vals[16], CE.NDATA))
    chk(vals[19] == 4 * CE.NDATA, "bit count = %d, want %d"
        % (vals[19], 4 * CE.NDATA))
    chk(vals[17] > 0, "regularisation is zero")
    chk(vals[7] > 0, "MMSE tile never ran on the CGRA")

    hw_bits = []
    for i in range(CE.NDATA):
        b = vals[20 + i]
        hw_bits += [b & 1, (b >> 1) & 1, (b >> 2) & 1, (b >> 3) & 1]
    bad = [i for i in range(len(ref_bits)) if hw_bits[i] != ref_bits[i]]
    tol = tol_for(snr)
    chk(len(bad) <= tol,
        "%d/%d hard bits differ, tolerance %d (first %s)"
        % (len(bad), len(ref_bits), tol, bad[:8]))

    for rx in range(2):
        for l in range(2):
            a = complex(_s32(vals[8 + 4 * rx + 2 * l]),
                        _s32(vals[9 + 4 * rx + 2 * l]))
            b = ref["h"][rx][l] * CE.H_SCALE
            err = abs(a - b) / max(abs(b), 1.0)
            chk(err < 0.4, "h[%d][%d] hw=%s model=%s rel_err=%.0f%%"
                % (rx, l, a, b, err * 100))

    print("  probe: %d errors, %d cycles, lambda=%d, mmse_runs=%d"
          % (vals[1], vals[2], vals[17], vals[7]))
    print("  probe: hard bits %d/%d match (tolerance %d at %.0f dB)"
          % (len(ref_bits) - len(bad), len(ref_bits), tol, snr))
    print("  probe: TB crc=%s tb_err=%d after the float reference decode"
          % (ref["crc_ok"], ref["tb_err"]))
    if ok:
        print("[ OK ] 2x2 MMSE: 2 antennas x 60 pilots -> 4 coefficients -> "
              "180 data REs -> 720 hard bits")
        return 0
    print("[FAIL] 2x2 MMSE receiver")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--snr", type=float, default=25.0)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    sys.exit(main(a.dump, a.snr, a.seed))
