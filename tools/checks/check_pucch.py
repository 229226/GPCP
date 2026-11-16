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
"""Acceptance check for run-pucch (PUCCH format 0 SR / HARQ-ACK on the CGRA).

Verifies, against tools/models/nr_pucch.py's integer golden model:

  * epilogue / zero internal errors / 2 array runs / 128-cycle program;
  * both candidate correlations (re, im) and |C|^2 bit-for-bit;
  * all 16 PE partial sums per candidate bit-for-bit (the array's butterfly is
    cross-checked by the CPU inside the benchmark, surfaced through errs);
  * the winner, its m_CS, and the detected 1-bit SR/ACK value;
  * consistency with the independent floating-point model in nr_ul.py.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_pucch as P

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


def _u64(lo, hi):
    return (hi << 32) | (lo & 0xFFFFFFFF)


def partials(y, ref):
    """The 16 PE partial sums of the prach_corr kernel (PE p owns taps 8p..8p+7)."""
    out = []
    for p in range(16):
        re = im = 0
        for t in range(p * 8, min((p + 1) * 8, P.TAPS)):
            yr, yi = y[t]
            cr, ci = ref[t]
            re += yr * cr + yi * ci
            im += yi * cr - yr * ci
        out.append((re, im))
    return out


def main(path, ack, snr, seed):
    vals = parse_dump(path)
    if len(vals) < 48 + 32 * P.NCAND:
        print("  dump too short: %d words (need >= %d)"
              % (len(vals), 48 + 32 * P.NCAND))
        return 1

    g = P.golden(ack, snr, seed)
    d = g["detect"]
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    chk(vals[0] == MARKER, "epilogue missing: 0x%08x" % vals[0])
    chk(vals[4] == EPILOGUE, "program did not finish: 0x%08x" % vals[4])
    chk(vals[3] == 0, "program reported %d internal errors" % vals[3])
    chk(vals[1] == P.NCAND, "array runs = %d, want %d" % (vals[1], P.NCAND))
    chk(vals[8] == P.NCAND, "NCAND = %d, want %d" % (vals[8], P.NCAND))
    chk(vals[9] == P.TAPS, "TAPS = %d, want %d" % (vals[9], P.TAPS))
    chk(vals[19] == 129, "program cycles = %d, want 129" % vals[19])
    chk(vals[2] == 2 * P.NCAND + 1, "DMA ops = %d, want %d"
        % (vals[2], 2 * P.NCAND + 1))

    # per-candidate correlations, powers and PE partials
    for c in range(P.NCAND):
        re, im = d["corr"][c]
        chk(_s32(vals[20 + 4 * c]) == re,
            "C[%d].re hw=%d model=%d" % (c, _s32(vals[20 + 4 * c]), re))
        chk(_s32(vals[21 + 4 * c]) == im,
            "C[%d].im hw=%d model=%d" % (c, _s32(vals[21 + 4 * c]), im))
        want_pw = d["power"][c]
        got_pw = _u64(vals[22 + 4 * c], vals[23 + 4 * c])
        chk(got_pw == want_pw,
            "|C[%d]|^2 hw=%d model=%d" % (c, got_pw, want_pw))
        exp_part = partials(g["y"], g["ref"][c])
        bad = []
        for p in range(16):
            hr = _s32(vals[48 + 32 * c + 2 * p])
            hi = _s32(vals[49 + 32 * c + 2 * p])
            if (hr, hi) != exp_part[p]:
                bad.append(p)
        chk(not bad, "candidate %d PE partials differ at %s" % (c, bad[:8]))

    chk(vals[6] == d["cand"], "winner hw=%d model=%d" % (vals[6], d["cand"]))
    chk(vals[7] == d["cs"], "m_CS hw=%d model=%d" % (vals[7], d["cs"]))
    chk(vals[5] == ack, "detected bit = %d, want %d" % (vals[5], ack))
    chk(g["consistent"], "integer model disagrees with nr_ul.py float model")
    chk(vals[18] == 32 * P.NCAND, "partial cross-checks = %d, want %d"
        % (vals[18], 32 * P.NCAND))

    print("  probe: %d errors, %d array runs, %d DMAs, %d partial checks, "
          "%d-cycle program" % (vals[3], vals[1], vals[2], vals[18], vals[19]))
    print("  probe: C[0]=(%d,%d) C[6]=(%d,%d), margin %.1f dB"
          % (d["corr"][0][0], d["corr"][0][1], d["corr"][1][0], d["corr"][1][1],
             10 * __import__("math").log10(d["margin"])))
    print("  probe: winner m_CS=%d, bit=%d (want %d), float model %s"
          % (vals[7], vals[5], ack, "OK" if g["consistent"] else "BAD"))

    if ok:
        print("[ OK ] PUCCH format 0: 2 CGRA correlations -> energy compare "
              "-> %d-bit SR/ACK, all partials bit-exact" % vals[5])
        return 0
    print("[FAIL] PUCCH format 0 acceptance")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--ack", type=int, default=1)
    ap.add_argument("--snr", type=float, default=P.DEFAULT_SNR)
    ap.add_argument("--seed", type=int, default=P.DEFAULT_SEED)
    a = ap.parse_args()
    sys.exit(main(a.dump, a.ack, a.snr, a.seed))
