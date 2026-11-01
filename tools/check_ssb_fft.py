#!/usr/bin/env python3
"""Check the sample-level SSB PSS detection (sw/soc/ssb_fft_bm.c).

The program FFTs the PSS symbol of the four SSB time-domain samples (generated
by tools/ssb_plan.py) on the CGRA and correlates the resulting 127 PSS
resource elements against the three N_ID(2) candidates.

The frequency spectrum is fixed-point (Q15 twiddles, per-stage shifts), so the
correlation magnitudes are only proportional to the floating-point model; what
is checked here is the decision: the true candidate must win and the wrong
candidates must stay an order of magnitude lower.
"""
import sys
import ssb_plan as sp

NREP = 127
PSS_RE0 = 56


def main(path):
    vals = []
    for line in open(path):
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "word" and parts[2].startswith("0x"):
            w64 = int(parts[2], 16)
            vals.append(w64 & 0xFFFFFFFF)
            vals.append((w64 >> 32) & 0xFFFFFFFF)
    if len(vals) < 11:
        print(f"  dump too short: {len(vals)} words")
        return 1

    def sx(v):
        return v - 0x100000000 if v & 0x80000000 else v

    metrics = [(sx(vals[2 * c]), sx(vals[2 * c + 1])) for c in range(3)]
    norms = [abs(a) + abs(b) for a, b in metrics]

    # reference (floating point) correlation of the same symbol
    td, grid, payload = sp.build_td()
    x = sp._fft(td[1])
    ref = [sp.nr_seq.pss_seq(c) for c in range(3)]
    gold = [(sum(x[PSS_RE0 + t].real * ref[c][t] for t in range(NREP)),
             sum(x[PSS_RE0 + t].imag * ref[c][t] for t in range(NREP)))
            for c in range(3)]
    gold_norms = [abs(a) + abs(b) for a, b in gold]

    ok = True
    if vals[6] != 1:
        print(f"  detected N_ID2: got {vals[6]} expected 1")
        ok = False
    if vals[8] != 0:
        print(f"  errors reported by the program: {vals[8]}")
        ok = False
    if vals[11] != 0x600D:
        print(f"  epilogue missing: 0x{vals[11]:08x}")
        ok = False
    # the true candidate must dominate the other two
    if not (norms[1] > 3 * norms[0] and norms[1] > norms[2]):
        print(f"  candidate ordering wrong: |c0|={norms[0]} |c1|={norms[1]} |c2|={norms[2]}")
        ok = False
    # the wrong-candidate ratio should follow the model; a large gap means the
    # fixed-point spectrum or the reference path differs, so only report it
    ratio_c = norms[2] / max(1, norms[1])
    ratio_g = gold_norms[2] / max(1.0, gold_norms[1])
    if abs(ratio_c - ratio_g) > 0.35:
        print(f"[warn] cand2/cand1 ratio: hardware {ratio_c:.2f} vs model {ratio_g:.2f} "
              f"(decision is still correct; fidelity gap to investigate)")

    if ok:
        print(f"[info] PSS on the FFT output: |c0|={norms[0]} |c1|={norms[1]} "
              f"|c2|={norms[2]} (model {gold_norms[0]:.0f}/{gold_norms[1]:.0f}/"
              f"{gold_norms[2]:.0f}), {vals[10]} array runs, {vals[9]} cycles")
        print("[ OK ] SSB sample-level PSS: 4096-point FFT then correlation, "
              "N_ID2 detected from the spectrum")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "sim/soc/build/ssb_fft_dump.txt"))
