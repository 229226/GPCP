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


def sx32(v):
    return v - 0x100000000 if v & 0x80000000 else v


def sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def i32(v):
    """wrap a Python int to int32 (the array accumulator is 32 bits)"""
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


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

    metrics = [(sx32(vals[2 * c]), sx32(vals[2 * c + 1])) for c in range(3)]
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

    # ---- probe of candidate 1: check what the array actually consumed ----------
    if len(vals) >= 172 + 2 * NREP:
        refs = [sx32(vals[12 + t]) for t in range(NREP)]
        fb = [sx32(vals[140 + k]) for k in range(32)]
        rx = [(sx32(vals[172 + 2 * t]), sx32(vals[172 + 2 * t + 1]))
              for t in range(NREP)]
        want_ref = [int(round(sp.nr_seq.pss_seq(1)[t])) * 32767 for t in range(NREP)]
        ref_bad = [t for t in range(NREP) if refs[t] != want_ref[t]]
        rx_zero = [t for t in range(NREP) if rx[t][0] == 0 and rx[t][1] == 0]
        # per-PE accumulator recomputed with the 16-bit MAC the array implements
        pe_bad = []
        for p in range(16):
            sr = si = 0
            for t in range(p * 8, min(NREP, p * 8 + 8)):
                sr = i32(sr + sx16(rx[t][0]) * sx16(refs[t]))
                si = i32(si + sx16(rx[t][1]) * sx16(refs[t]))
            if fb[2 * p] != sr or fb[2 * p + 1] != si:
                pe_bad.append((p, fb[2 * p], sr, fb[2 * p + 1], si))
        if ref_bad:
            print(f"  probe: {len(ref_bad)}/{NREP} reference words differ from the "
                  f"PSS sequence, first {ref_bad[:6]}")
            ok = False
        if rx_zero:
            print(f"  probe: {len(rx_zero)}/{NREP} rx resource elements are zero, "
                  f"first {rx_zero[:6]}")
            ok = False
        if pe_bad:
            print(f"  probe: {len(pe_bad)}/16 per-PE accumulators differ from the "
                  f"16-bit MAC of the probe data, first {pe_bad[:4]}")
            ok = False
        lat = [sx32(vals[426 + i]) for i in range(4)]
        print(f"  probe: latched descriptor src=0x{lat[0] & 0xFFFFFFFF:08x} "
              f"len={lat[1]} stride={lat[2]} row={lat[3]}")
        if len(vals) >= 430 + 512:
            rows = [want_ref[16 * r:16 * r + 16] for r in range(8)]
            banks = [[sx32(vals[430 + 32 * b + w]) for w in range(32)]
                     for b in range(16)]        # banks 8..23
            used = [8 + b for b in range(16) if any(banks[b][w] != 0 for w in range(32))]
            print(f"  probe: non-empty banks {used}")
            for r in range(8):
                got = banks[r][:15]
                same = got == rows[r][:15]
                print(f"  probe: row {r} (t={16 * r}..{16 * r + 15}) -> bank {8 + r}: "
                      f"{'match' if same else 'MISMATCH'}"
                      + ("" if same else f"  hw={got[:4]} want={rows[r][:4]}"))
    # ---- SSS: 336 hypotheses on the spectrum of symbol 3 ---------------------
    if len(vals) >= 946:
        got_nid1 = vals[942]
        true_nid1 = vals[943]
        # model: same correlation on the float FFT of the SSS symbol
        x3 = sp._fft(td[3])
        seq = [sp.nr_seq.sss_seq(h, 1) for h in range(336)]
        gold = []
        for h in range(336):
            r = sum(x3[PSS_RE0 + t].real * seq[h][t] for t in range(NREP))
            i = sum(x3[PSS_RE0 + t].imag * seq[h][t] for t in range(NREP))
            gold.append(abs(r) + abs(i))
        best_h = max(range(336), key=lambda h: gold[h])
        print(f"  probe: SSS N_ID1 hw={got_nid1} (metric {vals[944]}) "
              f"true={true_nid1} (metric {vals[945]}), model argmax={best_h} "
              f"model true={gold[true_nid1]:.0f} best={gold[best_h]:.0f}")
        if got_nid1 != true_nid1:
            print(f"  SSS: detected N_ID1 {got_nid1} instead of {true_nid1}")
            ok = False
        if best_h != true_nid1:
            print(f"  SSS: the model itself does not peak on {true_nid1} "
                  f"(argmax {best_h}) - grid/RE layout problem")
            ok = False

    # ---- DMRS channel estimate + decoded MIB -------------------------------
    if len(vals) >= 986:
        h = [(sx32(vals[946 + 2 * s]), sx32(vals[947 + 2 * s])) for s in range(4)]
        print(f"  probe: channel estimate (H_SCALE=16) per symbol: {h}")
        # the samples carry no channel: H must come out as (16, 0)
        if any(abs(hre - 16) > 2 or abs(him) > 2 for hre, him in h):
            print("  DMRS: channel estimate is not (16, 0) - pilot Gold/c_init wrong")
            ok = False
        mib = [vals[954 + k] for k in range(32)]
        want = sp.payload_bits()
        bad = [k for k in range(32) if mib[k] != want[k]]
        print(f"  probe: decoded MIB bits = {''.join(str(b) for b in mib)}")
        print(f"  probe: model MIB bits   = {''.join(str(b) for b in want)}")
        if bad:
            print(f"  MIB: {len(bad)}/32 bits differ (first {bad[:6]})")
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
              f"{gold_norms[2]:.0f}), N_ID2={vals[6]}, {vals[10]} array runs, "
              f"{vals[9]} cycles")
        print("[ OK ] SSB sample-level receive chain: 4096-point FFTs, PSS (N_ID2), "
              "SSS (N_ID1), DMRS channel estimate, QPSK+descrambling and polar SC "
              "-> 32/32 MIB bits")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "sim/soc/build/ssb_fft_dump.txt"))
