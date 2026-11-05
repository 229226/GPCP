#!/usr/bin/env python3
"""Acceptance check for run-prach (Msg1 PRACH detection on the CGRA).

The gate is per-element: all L_RA=139 correlation values (re, im) that the array
produced must equal the integer model of tools/nr_rach.py bit for bit, and the
peak must decode to the preamble index and timing advance that were transmitted.
The 16 per-PE partials are cross-checked inside the SoC against the array's own
butterfly reduction; this script verifies that the check actually ran (nchk) and
that it reported no mismatch (errors).  Any injected wrong value fails.
"""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cgra
import nr_rach as R
import nr_sib1 as S1

MARKER = 0xCAFEBABE
EPILOGUE = 0x600D
NRES = 20 + 2 * R.L_RA
MARGIN_MIN_DB = 6.0


def parse_dump(path):
    vals = []
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "word" and parts[2].startswith("0x"):
                w = int(parts[2], 16)
                vals.append(w & 0xFFFFFFFF)
                vals.append((w >> 32) & 0xFFFFFFFF)
    return vals


def s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def main(path, preamble, ta, zczc, snr, seed):
    vals = parse_dump(path)
    if len(vals) < NRES:
        print("  dump too short: %d words (need >= %d)" % (len(vals), NRES))
        return 1

    n_cs = R.n_cs_of(dict(S1.default_sib1(), zero_corr_zone=zczc))
    chips = R.zc_chips()
    y = R.msg1_tx(preamble, ta, n_cs, chips, snr, seed)
    exp = R.msg1_correlations(y, chips)
    det = R.msg1_detect(y, chips, n_cs)

    ok = True
    bad = []
    if vals[0] != MARKER:
        print("  marker missing: 0x%08x" % vals[0])
        ok = False
    if vals[4] != EPILOGUE:
        print("  epilogue missing: 0x%08x" % vals[4])
        ok = False
    for name, want, got in (("runs", R.L_RA, vals[1]),
                            ("errors", 0, vals[3]),
                            ("partials checked", R.L_RA * 32, vals[16]),
                            ("N_CS", n_cs, vals[13]),
                            ("L_RA", R.L_RA, vals[14]),
                            ("taps", R.PRACH_TAPS, vals[15]),
                            ("program cycles", cgra.PRACH_CORR_CYCLES, vals[17])):
        if got != want:
            print("  %s = %d, expected %d" % (name, got, want))
            ok = False

    for d, (re, im) in enumerate(exp):
        if s32(vals[20 + 2 * d]) != re or s32(vals[21 + 2 * d]) != im:
            bad.append(d)
    if bad:
        print("  correlations mismatched at %d/%d shifts, first d=%d: hw=(%d,%d) "
              "model=(%d,%d)" % (len(bad), R.L_RA, bad[0],
                                 s32(vals[20 + 2 * bad[0]]),
                                 s32(vals[21 + 2 * bad[0]]),
                                 exp[bad[0]][0], exp[bad[0]][1]))
        ok = False

    for name, want, got in (("delay", det["delay"], vals[5]),
                            ("preamble", det["preamble"], vals[6]),
                            ("TA", det["ta"], vals[7]),
                            ("2nd delay", det["second_delay"], vals[10])):
        if got != want:
            print("  %s = %d, expected %d" % (name, got, want))
            ok = False
    if s32(vals[8]) != det["re"] or s32(vals[9]) != det["im"]:
        print("  peak value hw=(%d,%d) model=(%d,%d)" % (
            s32(vals[8]), s32(vals[9]), det["re"], det["im"]))
        ok = False
    if det["preamble"] != preamble or det["ta"] != ta:
        print("  detected p=%d TA=%d, transmitted p=%d TA=%d" % (
            det["preamble"], det["ta"], preamble, ta))
        ok = False

    margin_db = 10 * math.log10(max(1e-9, det["margin"]))
    if margin_db < MARGIN_MIN_DB:
        print("  peak-to-second margin %.1f dB below %.1f dB" % (
            margin_db, MARGIN_MIN_DB))
        ok = False

    print("  probe: %d/%d shifts bit-exact, peak d=%d -> preamble %d TA %d "
          "(|C|^2=%.4g, 2nd d=%d %.4g, margin %.1f dB)" % (
              R.L_RA - len(bad), R.L_RA, vals[5], vals[6], vals[7],
              det["power"], det["second_delay"], det["second_power"], margin_db))
    print("  probe: %d array runs, %d DMA ops, %d partial/butterfly checks, "
          "%d errors, %d-cycle program" % (vals[1], vals[2], vals[16], vals[3],
                                           vals[17]))

    if ok:
        print("[ OK ] PRACH Msg1: L_RA=%d cyclic-shift search -> preamble %d + "
              "TA %d, all %d correlations bit-exact" % (
                  R.L_RA, preamble, ta, R.L_RA))
        return 0
    print("[FAIL] PRACH Msg1 detection")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--preamble", type=int, default=2)
    ap.add_argument("--ta", type=int, default=9)
    ap.add_argument("--zczc", type=int, default=0)
    ap.add_argument("--snr", type=float, default=R.PRACH_SNR)
    ap.add_argument("--seed", type=int, default=R.DEFAULT_SEED)
    a = ap.parse_args()
    sys.exit(main(a.dump, a.preamble, a.ta, a.zczc, a.snr, a.seed))
