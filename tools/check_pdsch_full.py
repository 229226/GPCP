#!/usr/bin/env python3
"""Acceptance check for the PDSCH (SISO) full receive chain on the SoC.

Parses the Verilator --dump output and verifies that the SoC, starting from
time-domain OFDM samples, produced exactly the same 1040 LDPC hard decisions
as the golden model, that CRC24A passes, and that the 80-bit transport block
matches the transmitted payload.

The fixed-point FFT (ssb_fft.inc, shared with run-ssb-fid) has a different
amplitude/phase gain than the floating-point golden model, so absolute LLR
magnitudes are NOT expected to match -- measured ~0.2x on this link.  Absolute
anchors are printed for visibility, while the strict gates are: the sign of
every probed soft bit (proves the whole front end is oriented correctly), the
1040 bit-exact LDPC hard decisions, CRC24A, and the 80-bit transport block.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdsch_full_plan as P

MARKER = 0xCAFEBABE
EPILOGUE = 0x600D
NHARD = 1040


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


def _s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def main(path, snr, seed):
    vals = parse_dump(path)
    if len(vals) < 1081:
        print("  dump too short: %d words (need >= 1081)" % len(vals))
        return 1

    g = P.golden(snr, seed)
    hard = vals[8:8 + NHARD]
    tb = hard[:P.M.A_TB]
    crc_ok = vals[5]
    h_hw = [vals[1048], vals[1049], vals[1050], vals[1051]]
    raw_hw = vals[1052:1060]
    descr_hw = vals[1060:1068]
    dem_hw = vals[1068:1076]

    ok = True
    if vals[0] != MARKER:
        print("  epilogue missing: 0x%08x" % vals[0])
        ok = False
    if vals[6] != EPILOGUE:
        print("  program did not finish: 0x%08x" % vals[6])
        ok = False
    if vals[1] != 0:
        print("  program reported %d internal errors" % vals[1])
        ok = False
    if crc_ok != 1 or vals[1076] != 1:
        print("  CRC24A failed (hw=%d / %d)" % (crc_ok, vals[1076]))
        ok = False

    bad = [i for i in range(NHARD) if hard[i] != g["hard"][i]]
    if bad:
        print("  LDPC hard decisions: %d/%d differ (first %s)" % (len(bad), NHARD, bad[:6]))
        ok = False

    tbbad = [i for i, (a, b) in enumerate(zip(tb, g["want"])) if a != b]
    if tbbad:
        print("  transport block: %d/%d bits differ (first %s)" % (
            len(tbbad), P.M.A_TB, tbbad[:8]))
        ok = False

    # Front-end sanity: a dead FFT / broken LS estimate would show up here.
    if h_hw[0] == 0 and h_hw[1] == 0:
        print("  channel estimate of symbol 0 is zero (FFT or LS broken)")
        ok = False
    if all(v == 0 for v in raw_hw):
        print("  first 8 raw LLRs are all zero (QPSK demod broken)")
        ok = False

    # The fixed-point FFT (ssb_fft.inc, shared with run-ssb-fid) has an
    # amplitude/phase quantisation gain that differs from the floating-point
    # model, so absolute LLR magnitudes are NOT expected to match.  What must
    # match is the SIGN of every soft bit -- that is the real proof that FFT,
    # RE mapping, LS estimate, equalisation, descrambling and de-matching all
    # point the right way.
    def sign_bad(hw, model, tag):
        bad = []
        for i, (a, b) in enumerate(zip(hw, model)):
            av, bv = _s32(a), int(b)
            if (av > 0) != (bv > 0):
                bad.append(i)
        if bad:
            print("  %s: %d sign mismatches at %s" % (tag, len(bad), bad[:8]))
        return bad

    if sign_bad(raw_hw, g["raw"][:8], "raw LLR signs"):
        ok = False
    if sign_bad(descr_hw, g["descr"][:8], "descrambled LLR signs"):
        ok = False
    if sign_bad(dem_hw, g["scaled"][40:48], "de-matched LLR signs"):
        ok = False

    h_model = [int(g["h"][0].real), int(g["h"][0].imag),
               int(g["h"][1].real), int(g["h"][1].imag)]
    print("  probe: %d errors, %d cycles, %d array runs, %d DMAs" % (
        vals[1], vals[2], vals[3], vals[4]))
    print("  probe: chan est hw=%s model=%s" % (h_hw, h_model))
    print("  probe: raw LLR  hw=%s" % raw_hw)
    print("  probe:          model=%s" % [int(v) for v in g["raw"][:8]])
    print("  probe: descrambled hw=%s" % descr_hw)
    print("  probe:             model=%s" % [int(v) for v in g["descr"][:8]])
    print("  probe: de-matched LLR[40:48] hw=%s model=%s" % (
        dem_hw, g["scaled"][40:48]))
    yq0 = g["yq"][0][0]
    print("  probe: FFT bin0 spec hw=(%d,%d) yw=(%d,%d) model=(%d,%d)" % (
        _s32(vals[1077]), _s32(vals[1078]), _s32(vals[1079]), _s32(vals[1080]),
        int(yq0.real), int(yq0.imag)))
    print("  probe: CRC24A ok=%d, TB %d/%d bits, hard decisions %d/%d" % (
        crc_ok, P.M.A_TB - len(tbbad), P.M.A_TB, NHARD - len(bad), NHARD))
    print("  probe: TB = %s" % "".join(str(b) for b in tb))

    if ok:
        print("[ OK ] PDSCH full chain: TD samples -> FFT -> LS/equalise -> LLR "
              "-> descramble -> de-match -> 8 LDPC iterations -> CRC -> 80/80 TB")
        return 0
    print("[FAIL] PDSCH full chain acceptance")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--snr", type=float, default=P.DEFAULT_SNR)
    ap.add_argument("--seed", type=int, default=P.DEFAULT_SEED)
    a = ap.parse_args()
    sys.exit(main(a.dump, a.snr, a.seed))
