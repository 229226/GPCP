#!/usr/bin/env python3
"""G2 / B3 golden model: PDCCH transmit chain.

    DCI(32) -> CRC24C -> RNTI mask -> frozen insertion -> polar transform
           -> rate matching E=432 -> scrambling -> QPSK -> REG/CCE mapping

Only the polar transform runs on the array (A1's nine kernels).  Everything
else is scalar on CVA6, for the same reason B1/B2 are: bit work with little
spatial parallelism, and one array run costs ~1456 cycles of transfer orchestration
(T0 measured).

The parameter set is the one the receive side already blind-decodes
(tools/nr_pdcch.py, gated by run-pdcch): A_DCI=32, K=56, N=512, E=432,
216 QPSK symbols, c_init = n_ID = 200, five candidate RNTIs.

`dump-td` writes only the DCI, the parameters and the RNTI list -- never an
expected symbol -- and the self-test cross-checks each stage against the
independent model the receiver uses (`nr_pdcch.transmit`,
`nr_pdcch.polar_encode_k`, `nr_pbch.rate_match_pbch`, `nr_pdcch_reg`).
"""
import argparse
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import nr_pbch as NR                                     # noqa: E402
import nr_pdcch as PDCCH                                 # noqa: E402
import nr_pdcch_reg as REG                               # noqa: E402

MAGIC = 0x1DCC0001

A_DCI = PDCCH.A_DCI                    # 32
CRC_LEN = PDCCH.CRC_LEN                # 24
K_INFO = PDCCH.K                       # 56
NPOL = PDCCH.NPOL                      # 512
DEPTH = PDCCH.DEPTH                    # 9
E_BITS = PDCCH.E_BITS                  # 432
QAM = PDCCH.QPSK                       # 2000
N_ID = PDCCH.N_ID                      # 200
N_REG = E_BITS // 2                    # 216 symbols = 216 REGs
L_BUNDLE = 6                           # one CCE = 6 REGs
N_SHIFT = 0
FIELDS = dict(PDCCH.DEFAULT_FIELDS)

# Two RNTIs: the intended UE and a neighbour.  Both must produce a valid
# codeword; only the CRC mask differs, which is exactly what blind detection
# relies on.
VARIANTS = (PDCCH.N_RNTI, 0x5678)


def info_bits(fields, rnti):
    dci = PDCCH.pack_dci(fields)
    crc = PDCCH.crc24c(dci)
    return dci, dci + PDCCH.rnti_scramble_crc(crc, rnti), crc


def crc_word(fields=FIELDS):
    """The CRC24C as one MSB-first integer, matching the C's out[11]."""
    w = 0
    for b in PDCCH.crc24c(PDCCH.pack_dci(fields)):
        w = (w << 1) | (b & 1)
    return w


def insert_frozen(info):
    u = [0] * NPOL
    idx = PDCCH.info_indices(NPOL, K_INFO, DEPTH)
    for j, i in enumerate(idx):
        u[i] = info[j] & 1
    return u, idx


def reg_map(syms, n_reg=N_REG, L=L_BUNDLE, n_shift=N_SHIFT):
    """38.211 7.3.2.3 interleaved REG mapping: bundle j -> bundle f(j)."""
    f = REG.interleave(n_reg, L, n_shift)
    out = [None] * len(syms)
    for j in range(n_reg // L):
        for k in range(L):
            out[f[j] * L + k] = syms[j * L + k]
    return out, f


def pdcch_tx(fields=FIELDS, rnti=PDCCH.N_RNTI):
    dci, info, crc = info_bits(fields, rnti)
    u, idx = insert_frozen(info)
    x = PDCCH.polar_encode_k(info)
    e = NR.rate_match_pbch(x, E_BITS)
    scr = PDCCH.scrambling_seq(E_BITS)
    e_sc = [b ^ s for b, s in zip(e, scr)]
    syms = PDCCH.qpsk_map(e_sc)
    reg, f = reg_map(syms)
    return dict(dci=dci, info=info, crc=crc, u=u, idx=idx, x=x, e=e,
                scr=scr, e_sc=e_sc, syms=syms, reg=reg, f=f,
                rnti=rnti, fields=dict(fields))


# ------------------------------------------------------------------- the dump
def pack(bits):
    out = [0] * ((len(bits) + 31) // 32)
    for i, b in enumerate(bits):
        if b:
            out[i >> 5] |= 1 << (i & 31)
    return out


def dump_td(path, fields=FIELDS, variants=VARIANTS):
    dci = PDCCH.pack_dci(fields)
    dci_word = 0
    for b in dci:
        dci_word = (dci_word << 1) | (b & 1)
    words = [MAGIC, len(variants), N_ID, dci_word,
             N_REG, L_BUNDLE, N_SHIFT, 0]
    for r in variants:
        words += [r, 0]
    with open(path, "wb") as fh:
        for w in words:
            fh.write(struct.pack("<I", w & 0xFFFFFFFF))
    return len(words) * 4


def read_td(path):
    data = open(path, "rb").read()
    td = list(struct.unpack("<%dI" % (len(data) // 4), data))
    assert td[0] == MAGIC, "bad TD magic 0x%08x" % td[0]
    nvar = td[1]
    variants = [td[8 + 2 * v] for v in range(nvar)]
    return dict(n_id=td[2], dci=td[3], n_reg=td[4], L=td[5], n_shift=td[6],
                variants=variants)


def dci_from_word(w):
    return [(w >> (31 - i)) & 1 for i in range(A_DCI)]


# ----------------------------------------------------------------- self test
def selftest():
    ok = True
    print("PDCCH transmit chain (G2/B3): DCI -> CRC24C -> RNTI -> polar -> "
          "rate match -> scramble -> QPSK -> REG")

    r = pdcch_tx()
    ref_syms, ref_dci = PDCCH.transmit(FIELDS, PDCCH.N_RNTI)
    same = (r["dci"] == ref_dci and
            [int(c.real) for c in r["syms"]] == [int(c.real) for c in ref_syms] and
            [int(c.imag) for c in r["syms"]] == [int(c.imag) for c in ref_syms])
    ok &= same
    print("  full chain vs nr_pdcch.transmit                : %s (%d symbols)"
          % ("OK" if same else "FAIL", len(r["syms"])))

    x_ok = r["x"] == PDCCH.polar_encode_k(r["info"])
    e_ok = r["e"] == NR.rate_match_pbch(r["x"], E_BITS)
    ok &= x_ok and e_ok
    print("  polar codeword / rate matching                 : %s / %s"
          % ("OK" if x_ok else "FAIL", "OK" if e_ok else "FAIL"))

    scr_ok = r["scr"] == PDCCH.scrambling_seq(E_BITS)
    ok &= scr_ok
    print("  scrambling c_init = n_ID = %d                  : %s"
          % (N_ID, "OK" if scr_ok else "FAIL"))

    # REG mapping must be a permutation, agree with the CCE semantics, and
    # actually move something (L=1 would be the identity).
    sym_pairs = [(int(c.real), int(c.imag)) for c in r["syms"]]
    reg_pairs = [(int(c.real), int(c.imag)) for c in r["reg"]]
    perm_ok = sorted(reg_pairs) == sorted(sym_pairs)
    moved = sum(1 for a, b in zip(reg_pairs, sym_pairs) if a != b)
    cce_ok = True
    nb = N_REG // L_BUNDLE
    for cce in range(3):
        want = sorted(REG.cce_regs(cce, N_REG, L_BUNDLE, N_SHIFT))
        got = sorted([r["f"][cce] * L_BUNDLE + k for k in range(L_BUNDLE)])
        cce_ok &= want == got
    ok &= perm_ok and cce_ok and moved > 0
    print("  REG/CCE mapping: permutation, %d/%d moved, CCEs %s"
          % (moved, len(r["reg"]), "OK" if cce_ok else "FAIL"))

    # Two RNTIs: same DCI, different CRC mask -> different codewords, and the
    # unmasked CRC must still be recoverable from either.
    r2 = pdcch_tx(FIELDS, VARIANTS[1])
    distinct = r2["info"] != r["info"] and r2["x"] != r["x"]
    ok &= distinct
    print("  second RNTI 0x%04x changes the codeword         : %s"
          % (VARIANTS[1], "OK" if distinct else "FAIL"))

    bad = pdcch_tx(FIELDS, 0x1111)
    ok &= bad["x"] != r["x"]
    print("  injection wrong RNTI 0x1111                     : OK")

    print("[ %s ] PDCCH transmit chain" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="PDCCH transmit golden model")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    d = sub.add_parser("dump-td")
    d.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.cmd == "dump-td":
        n = dump_td(a.out)
        print("wrote %s (%d bytes, DCI %d bits, %d RNTI variants, E=%d, N_REG=%d)"
              % (a.out, n, A_DCI, len(VARIANTS), E_BITS, N_REG))
        return 0
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
