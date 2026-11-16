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
"""Check the SoC-level NR PSS detection (sw/soc/bench/sync_bm.c).

Mirrors the program exactly: same m-sequence, same deterministic noise, same
tap split (8 taps / PE), so the 16 per-PE complex partials come out bit-exact.
"""
import sys
import nr_seq

META = 0
NCAND = 3
NREP = 127


class Lcg:
    def __init__(self, seed=0x12345678):
        self.s = seed

    def noise(self):
        self.s = (self.s * 1103515245 + 12345) & 0xFFFFFFFF
        v = ((self.s >> 16) & 0x3FF) - 512
        return v


def pss_seq(nid2):
    return nr_seq.pss_seq(nid2)


def build_rx(nid2):
    g = Lcg()
    re, im = [], []
    for t in range(NREP):
        s = pss_seq(nid2)[t]
        re.append(s * 6000 + g.noise())
        im.append(s * 3000 + g.noise())
    return re, im


def main(path):
    vals = []
    for line in open(path):
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "word" and parts[2].startswith("0x"):
            w64 = int(parts[2], 16)
            vals.append(w64 & 0xFFFFFFFF)
            vals.append((w64 >> 32) & 0xFFFFFFFF)
    w = vals[META:]
    if len(w) < 105:
        print(f"  dump too short: {len(w)} words")
        return 1

    rx_re, rx_im = build_rx(1)
    ok = True
    metrics = []

    for c in range(NCAND):
        ref = pss_seq(c)
        part = nr_seq.corr_partials(rx_re, rx_im, ref)          # same split as the kernel
        exp_words = []
        for p in range(16):
            exp_words.append(part[p][0] & 0xFFFFFFFF)
            exp_words.append(part[p][1] & 0xFFFFFFFF)
        got_words = w[5 + c * 32: 5 + c * 32 + 32]
        for k in range(32):
            if got_words[k] != exp_words[k]:
                print(f"  cand {c} partial word {k}: got 0x{got_words[k]:08x} "
                      f"expected 0x{exp_words[k]:08x}")
                ok = False
        re = sum(p[0] for p in part)
        im = sum(p[1] for p in part)
        mag = abs(re) + abs(im)
        metrics.append(mag >> 10)
        if metrics[c] != w[c]:
            print(f"  cand {c} metric: got {w[c]} expected {metrics[c]}")
            ok = False

    best = max(range(NCAND), key=lambda c: metrics[c])
    if w[3] != best:
        print(f"  detected N_ID2: got {w[3]} expected {best}")
        ok = False
    if w[4] != 1:
        print(f"  true N_ID2 not reported: {w[4]}")
        ok = False
    if w[101] != 0:
        print(f"  errors reported by the program: {w[101]}")
        ok = False
    if w[104] != 0x600D:
        print(f"  epilogue missing: 0x{w[104]:08x}")
        ok = False

    if ok:
        print(f"[info] PSS: metrics ({metrics[0]}, {metrics[1]}, {metrics[2]}) -> "
              f"N_ID2={w[3]} (true 1), {w[103]} array runs, {w[102]} cycles")
        print("[ OK ] NR PSS detection: 3 candidates correlated on the CGRA, "
              "partials bit-exact, argmax = true N_ID2")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "sim/soc/build/sync_dump.txt"))
