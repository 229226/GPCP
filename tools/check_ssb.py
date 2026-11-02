#!/usr/bin/env python3
"""Check the SoC-level SSB receive chain (sw/soc/ssb_bm.c).

Mirrors tools/ssb_plan.py: same Gold sequences, PSS/SSS/SSS-search, DMRS
channel estimate, QPSK demod + PBCH descrambling, polar SC decoding.
Compares the detected cell ID, the PSS/SSS correlation values, the channel
estimates and the decoded MIB payload bit-for-bit.
"""
import sys
import ssb_plan as sp

REF_SCALE = 32767          # the CGRA correlation uses Q15 references
WORD_SHIFT = 16            # and exports the int64 correlation >> 16


def sx(v):
    """Dump words are unsigned; the program exports signed int64 sums."""
    return v - 0x100000000 if v & 0x80000000 else v


def expect_corr(py_val):
    return sx((int(round(py_val * REF_SCALE)) >> WORD_SHIFT) & 0xFFFFFFFF)


def main(path):
    vals = []
    for line in open(path):
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "word" and parts[2].startswith("0x"):
            w64 = int(parts[2], 16)
            vals.append(w64 & 0xFFFFFFFF)
            vals.append((w64 >> 32) & 0xFFFFFFFF)
    if len(vals) < 89:
        print(f"  dump too short: {len(vals)} words")
        return 1

    grid, payload, bits = sp.build_grid()
    y = sp.transmit(grid, sp.Lcg())

    ok = True
    # --- cell search ---
    pss = [sp.pss_corr(y, c) for c in range(3)]
    nid2 = max(range(3), key=lambda c: abs(pss[c].real) + abs(pss[c].imag))
    sss = {h: sp.sss_corr(y, h, nid2) for h in range(336)}
    nid1 = max(sss, key=lambda h: abs(sss[h]))

    got_pss = [(sx(vals[4 + 2 * c]), sx(vals[5 + 2 * c])) for c in range(3)]
    exp_pss = [(expect_corr(int(pss[c].real)), expect_corr(int(pss[c].imag)))
               for c in range(3)]
    if got_pss != exp_pss:
        print(f"  PSS correlations: got {got_pss} expected {exp_pss}")
        ok = False
    if (vals[0], vals[1]) != (nid2, nid1):
        print(f"  cell ID: got N_ID2={vals[0]} N_ID1={vals[1]}, "
              f"expected {nid2}/{nid1}")
        ok = False
    exp_sss = (expect_corr(int(sss[nid1].real)), expect_corr(int(sss[nid1].imag)))
    if (sx(vals[10]), sx(vals[11])) != exp_sss:
        print(f"  SSS correlation: got {(sx(vals[10]), sx(vals[11]))} expected {exp_sss}")
        ok = False

    # --- channel estimate, demod, decode ---
    h, llr = sp.receiver(y, nid1, nid2)
    got_h = [(sx(vals[12 + 2 * s]), sx(vals[13 + 2 * s])) for s in range(4)]
    exp_h = [(int(c.real), int(c.imag)) for c in h]
    if got_h != exp_h:
        print(f"  channel estimates: got {got_h} expected {exp_h}")
        ok = False

    u = sp.sc_decode(llr)
    dec = [u[i] for i in sp.info_indices()]
    got_dec = vals[20:52]
    got_tx = vals[52:84]
    if got_tx != payload:
        print("  transmitted payload differs from the model")
        ok = False
    if got_dec != dec:
        for i in range(32):
            if got_dec[i] != dec[i]:
                print(f"  decoded bit {i}: got {got_dec[i]} expected {dec[i]}")
                ok = False
                break
    mism = sum(1 for i in range(32) if got_dec[i] != payload[i])
    if got_dec != payload:
        print(f"  decoded MIB differs from the transmitted one ({mism} bits)")
        ok = False
    if vals[88] != mism:
        print(f"  mismatch count: got {vals[88]} expected {mism}")
        ok = False
    if vals[84] != 0:
        print(f"  errors reported by the program: {vals[84]}")
        ok = False
    if vals[87] != 0x600D:
        print(f"  epilogue missing: 0x{vals[87]:08x}")
        ok = False

    if ok:
        print(f"[info] SSB: N_ID1={nid1} N_ID2={nid2}, H={exp_h[0]}, "
              f"{vals[86]} array runs, {vals[85]} cycles")
        print("[ OK ] SSB chain: PSS/SSS, DMRS channel estimate, QPSK demod, "
              "PBCH descrambling and polar SC decode all match the golden model")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "sim/soc/build/ssb_dump.txt"))
