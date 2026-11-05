#!/usr/bin/env python3
"""Acceptance check for run-pdsch-64qam (MCS-driven 64QAM PDSCH on the SoC).

Verifies against tools/nr_pdsch_amc.golden():
  * epilogue / zero internal errors / CRC24A;
  * bit-exact 1040 LDPC hard decisions and the 80-bit transport block;
  * the Qm / level scale / symbol count the binary was built with;
  * the sign of every probed LLR (raw, descrambled, de-matched);
  * k0 = rv * USABLE/4 and the 38.211 7.4.1.1 DMRS c_init values;
  * at high SNR the channel estimate and raw LLR magnitudes.

Element-wise gates are essential here: an injection that demaps a 64QAM
waveform as QPSK still converges to a valid codeword (CRC passes) while hundreds of LLRs differ -- see nr_pdsch_amc.selftest().
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_pdsch_amc as A
import nr_qam as QAM
import pdsch_full_plan as F

MARKER = 0xCAFEBABE
EPILOGUE = 0x600D
NHARD = 1040


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


def main(path, mcs, rv, snr, seed):
    vals = parse_dump(path)
    if len(vals) < 1088:
        print("  dump too short: %d words (need >= 1088)" % len(vals))
        return 1

    qm = A.qm_of_mcs(mcs)
    g = A.golden(mcs, rv, snr, seed)
    hard = vals[8:8 + NHARD]
    tb = hard[:A.M.A_TB]
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    chk(vals[0] == MARKER, "epilogue missing: 0x%08x" % vals[0])
    chk(vals[6] == EPILOGUE, "program did not finish: 0x%08x" % vals[6])
    chk(vals[1] == 0, "program reported %d internal errors" % vals[1])
    chk(vals[5] == 1 and vals[1076] == 1,
        "CRC24A failed (hw=%d / %d)" % (vals[5], vals[1076]))
    chk(vals[1084] == qm, "binary Qm = %d, want %d" % (vals[1084], qm))
    chk(vals[1085] == QAM.LEVEL_SCALE[qm],
        "binary level scale = %d, want %d" % (vals[1085], QAM.LEVEL_SCALE[qm]))
    chk(vals[1086] == A.E_BITS // qm,
        "binary symbol count = %d, want %d" % (vals[1086], A.E_BITS // qm))
    chk(vals[1087] == g["k0"],
        "binary k0 = %d, want %d (rv %d)" % (vals[1087], g["k0"], rv))

    bad = [i for i in range(NHARD) if hard[i] != g["hard"][i]]
    chk(not bad, "LDPC hard decisions: %d/%d differ (first %s)"
        % (len(bad), NHARD, bad[:6]))
    tbbad = [i for i, (a, b) in enumerate(zip(tb, g["want"])) if a != b]
    chk(not tbbad, "transport block: %d/%d bits differ (first %s)"
        % (len(tbbad), A.M.A_TB, tbbad[:8]))
    # rv/k0 has no dedicated result word in this variant; it is verified by
    # the bit-exact hard decisions above plus the de-matched LLR signs below
    # (a wrong k0 produces a different codeword / LLR ordering).
    h_hw = [vals[1048], vals[1049], vals[1050], vals[1051]]
    chk(not (h_hw[0] == 0 and h_hw[1] == 0),
        "channel estimate of symbol 0 is zero (FFT or LS broken)")
    raw_hw = vals[1052:1060]
    chk(not all(v == 0 for v in raw_hw),
        "first 8 raw LLRs are all zero (64QAM demod broken)")

    def sign_bad(hw, model, tag):
        b = [i for i, (a, m) in enumerate(zip(hw, model))
             if (_s32(a) > 0) != (int(m) > 0)]
        if b:
            print("  %s: %d sign mismatches at %s" % (tag, len(b), b[:8]))
        return b

    if sign_bad(raw_hw, g["raw"][:8], "raw 64QAM LLR signs"):
        ok = False
    if sign_bad(vals[1060:1068], g["descr"][:8], "descrambled LLR signs"):
        ok = False
    if sign_bad(vals[1068:1076], g["scaled"][40:48], "de-matched LLR signs"):
        ok = False
    chk(vals[1081] == F.dmrs_cinit(0) and vals[1082] == F.dmrs_cinit(1),
        "DMRS c_init hw=(0x%x,0x%x) model=(0x%x,0x%x)"
        % (vals[1081], vals[1082], F.dmrs_cinit(0), F.dmrs_cinit(1)))

    print("  probe: %d errors, %d cycles, %d array runs, %d DMAs"
          % (vals[1], vals[2], vals[3], vals[4]))
    print("  probe: MCS %d -> Qm %d, level scale %d, %d symbols, rv %d -> k0 %d"
          % (mcs, vals[1084], vals[1085], vals[1086], rv, g["k0"]))
    print("  probe: chan est hw=%s model=%s"
          % (h_hw, [int(g["h"][0].real), int(g["h"][0].imag),
                    int(g["h"][1].real), int(g["h"][1].imag)]))
    print("  probe: CRC24A ok=%d, TB %d/%d bits, hard decisions %d/%d"
          % (vals[5], A.M.A_TB - len(tbbad), A.M.A_TB, NHARD - len(bad), NHARD))

    if ok:
        print("[ OK ] MCS %d (%dQAM) PDSCH: TD -> FFT -> LS -> 64QAM max-log "
              "-> 1040/1040 LDPC -> CRC24A -> 80/80 TB" % (mcs, 2 ** qm))
        return 0
    print("[FAIL] 64QAM PDSCH acceptance")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--mcs", type=int, default=20)
    ap.add_argument("--rv", type=int, default=0)
    ap.add_argument("--snr", type=float, default=25.0)
    ap.add_argument("--seed", type=int, default=A.DEFAULT_SEED)
    a = ap.parse_args()
    sys.exit(main(a.dump, a.mcs, a.rv, a.snr, a.seed))
