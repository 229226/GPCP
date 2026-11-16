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
"""Check the SoC-level NR SSS hypothesis search (sw/soc/bench/sss_bm.c).

Mirrors the program: same SSS sequences, same deterministic noise, same tap
split (8 taps / PE).  All 336 candidates are re-correlated in Python, so the
detected N_ID(1), both metrics and the partials of the true and the detected
candidate are compared exactly.
"""
import sys
import nr_seq

NREP = 127
NHYP = 336
TRUE_NID1 = 200
TRUE_NID2 = 1


class Lcg:
    def __init__(self, seed=0x0BADF00D):
        self.s = seed

    def noise(self):
        self.s = (self.s * 1103515245 + 12345) & 0xFFFFFFFF
        return ((self.s >> 16) & 0x3FF) - 512


def build_rx(nid1, nid2):
    g = Lcg()
    seq = nr_seq.sss_seq(nid1, nid2)
    re, im = [], []
    for t in range(NREP):
        s = seq[t]
        re.append(s * 6000 + g.noise())
        im.append(s * 3000 + g.noise())
    return re, im


def partials_words(rx_re, rx_im, ref):
    part = nr_seq.corr_partials(rx_re, rx_im, ref)
    words = []
    for p in part:
        words.append(p[0] & 0xFFFFFFFF)
        words.append(p[1] & 0xFFFFFFFF)
    return part, words


def metric(part):
    re = sum(p[0] for p in part)
    im = sum(p[1] for p in part)
    return (abs(re) + abs(im)) >> 10


def main(path):
    vals = []
    for line in open(path):
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "word" and parts[2].startswith("0x"):
            w64 = int(parts[2], 16)
            vals.append(w64 & 0xFFFFFFFF)
            vals.append((w64 >> 32) & 0xFFFFFFFF)
    if len(vals) < 73:
        print(f"  dump too short: {len(vals)} words")
        return 1

    rx_re, rx_im = build_rx(TRUE_NID1, TRUE_NID2)

    best, best_metric = 0, 0
    true_metric, true_words = 0, []
    for nid1 in range(NHYP):
        part, words = partials_words(rx_re, rx_im, nr_seq.sss_seq(nid1, TRUE_NID2))
        m = metric(part)
        if m > best_metric:
            best_metric, best = m, nid1
        if nid1 == TRUE_NID1:
            true_metric, true_words = m, words

    _, best_words = partials_words(rx_re, rx_im, nr_seq.sss_seq(best, TRUE_NID2))

    ok = True
    if vals[0] != best:
        print(f"  detected N_ID1: got {vals[0]} expected {best}")
        ok = False
    if vals[3] != best_metric:
        print(f"  best metric: got {vals[3]} expected {best_metric}")
        ok = False
    if vals[4] != true_metric:
        print(f"  true metric: got {vals[4]} expected {true_metric}")
        ok = False
    if vals[1] != TRUE_NID1 or vals[2] != TRUE_NID2:
        print(f"  true N_ID reported wrong: {vals[1]}/{vals[2]}")
        ok = False
    got_best = vals[9:41]
    if got_best != best_words:
        for k in range(32):
            if got_best[k] != best_words[k]:
                print(f"  detected partial {k}: got 0x{got_best[k]:08x} "
                      f"expected 0x{best_words[k]:08x}")
                ok = False
                break
    got_true = vals[41:73]
    if got_true != true_words:
        for k in range(32):
            if got_true[k] != true_words[k]:
                print(f"  true partial {k}: got 0x{got_true[k]:08x} "
                      f"expected 0x{true_words[k]:08x}")
                ok = False
                break
    if vals[5] != 0:
        print(f"  errors reported by the program: {vals[5]}")
        ok = False
    if vals[8] != 0x600D:
        print(f"  epilogue missing: 0x{vals[8]:08x}")
        ok = False

    if ok:
        print(f"[info] SSS: {NHYP} hypotheses, {vals[7]} array runs, {vals[6]} cycles, "
              f"best metric {best_metric} (true candidate {true_metric})")
        print(f"[ OK ] NR SSS search: N_ID1={vals[0]} (true {TRUE_NID1}), "
              f"partials bit-exact for the detected and the true candidate")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "sim/soc/build/sss_dump.txt"))
