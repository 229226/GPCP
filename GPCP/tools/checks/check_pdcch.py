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
"""Acceptance check for run-pdcch (PDCCH / DCI blind detection on the SoC).

Strict gates: epilogue, zero internal errors, the matched RNTI, the 32 DCI
bits, the 56 polar information bits, and the per-candidate CRC verdicts all
have to match tools/models/nr_pdcch.golden().  LLR magnitudes are only probed for
sign agreement: the fixed-point FFT has no AGC, so its gain tracks the
time-domain peak (same finding as the PDSCH chain).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_pdcch as M

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


def s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def main(path, snr, seed):
    vals = parse_dump(path)
    if len(vals) < 136:
        print("  dump too short: %d words (need >= 136)" % len(vals))
        return 1
    g = M.golden(snr_db=snr, seed=seed)
    ok = True

    if vals[0] != MARKER:
        print("  epilogue missing: 0x%08x" % vals[0]); ok = False
    if vals[6] != EPILOGUE:
        print("  program did not finish: 0x%08x" % vals[6]); ok = False
    if vals[1] != 0:
        print("  program reported %d internal errors" % vals[1]); ok = False
    if vals[5] != 1:
        print("  CRC24C did not pass on any candidate"); ok = False
    if vals[4] != g["rnti"]:
        print("  RNTI: hw=0x%04x model=0x%04x" % (vals[4], g["rnti"])); ok = False
    if vals[7] != len(M.CANDIDATES):
        print("  candidate count: %d" % vals[7]); ok = False

    dci = vals[8:40]
    if dci != g["dci"]:
        bad = [i for i, (a, b) in enumerate(zip(dci, g["dci"])) if a != b]
        print("  DCI: %d/32 bits differ (first %s)" % (len(bad), bad[:8])); ok = False

    info = vals[80:136]
    if info != g["info"]:
        bad = [i for i, (a, b) in enumerate(zip(info, g["info"])) if a != b]
        print("  polar info: %d/56 bits differ (first %s)" % (len(bad), bad[:8])); ok = False

    verdicts = vals[40:45]
    want = [1 if v else 0 for _, v in g["tried"]]
    if verdicts != want:
        print("  candidate CRC verdicts: hw=%s model=%s" % (verdicts, want)); ok = False

    hw_llr = [s32(v) for v in vals[45:61]]
    md_llr = [int(v) for v in g["llr"][:16]]
    sign_bad = [i for i, (a, b) in enumerate(zip(hw_llr, md_llr))
                if (a > 0) != (b > 0)]
    if sign_bad:
        print("  raw LLR sign mismatches at %s" % sign_bad[:8]); ok = False

    print("  probe: %d errors, %d cycles, %d array runs" % (
        vals[1], vals[2], vals[3]))
    print("  probe: matched RNTI=0x%04x (model 0x%04x), CRC ok=%d" % (
        vals[4], g["rnti"], vals[5]))
    print("  probe: candidate verdicts %s" % verdicts)
    print("  probe: DCI fields %s" % M.unpack_dci(dci))
    print("  probe: raw LLR  hw=%s" % hw_llr[:8])
    print("  probe:          model=%s" % md_llr[:8])
    print("  probe: polar info 56/56, de-matched LLR hw=%s" %
          [s32(v) for v in vals[61:69]])

    if ok:
        print("[ OK ] PDCCH: TD samples -> FFT -> LLR -> de-match -> polar SC "
              "-> RNTI blind detect -> 32/32 DCI bits")
        return 0
    print("[FAIL] PDCCH / DCI blind detection acceptance")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--snr", type=float, default=M.DEFAULT_SNR)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    sys.exit(main(a.dump, a.snr, a.seed))
