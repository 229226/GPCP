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
"""G2 / B2 golden model: downlink modulation chain, transmitter side.

    TB(80)  ->  CRC24A  ->  LDPC BG2 Zc=20 (the B1 encoder, shared)
            ->  rate matching      38.212 5.4.2.1 (puncture 2*Zc, skip filler,
                                                   k0 = rv * 246)
            ->  scrambling         38.211 7.3.1.1 (c_init = n_RNTI << 15)
            ->  bit interleaving   38.212 5.4.2.2 (Qm columns)
            ->  QAM mapping        38.211 7.3.1.2 (a = 2000 / 894 / 436)
            ->  layer mapping + precoding, 1 layer / 1 port (identity, but
                                                   written out explicitly)

The parameter set is exactly the one the hardware receive chain already decodes
(tools/pdsch_plan.py, tools/pdsch_full_plan.py):

    KB=6, ZC=20 -> K = 120 = 80 TB + 24 CRC24A + 16 filler
    NCODED=1040, PUNCT=40, buffer=1000, filler at buffer 64..79, USABLE=984
    E=720, K0 = rv * 246

so B2's symbols can be fed straight back into that receiver.  E stays 720 even
for Qm=4/6, which means 180 / 120 QAM symbols instead of 360 -- the same
deliberate simplification nr_pdsch_amc.py documents (the resource allocation is
not widened, the symbol count shrinks).

No expected values are stored in the TD: `dump-td` writes only the transport
block and the variant table, and the checker recomputes every stage here.  The
self-test cross-checks each stage against a *different* implementation that the
hardware already validated (ldpc_nr.encode, pdsch_full_plan.interleave,
pdsch_plan.qpsk_map, nr_pdsch_amc.deinterleave), so the model cannot pass by
agreeing with itself.
"""
import argparse
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import ldpc5g as L5                                      # noqa: E402
import ldpc_nr as LN                                     # noqa: E402
import nr_pdsch_amc as AMC                               # noqa: E402
import nr_qam as QAM                                     # noqa: E402
import pdsch_full_plan as F                              # noqa: E402
import pdsch_plan as M                                   # noqa: E402

MAGIC = 0x1D10D001

A_TB = M.A_TB                    # 80
CRC_LEN = M.CRC_LEN              # 24
KB = M.KB                        # 6
ZC = M.ZC                        # 20
K_BITS = KB * ZC                 # 120
NCODED = M.NCODED                # 1040
PUNCT = M.PUNCT                  # 40
FILLER0 = F.FILLER0              # 64 (buffer coordinates)
FILLER_N = F.FILLER_N            # 16
USABLE = F.USABLE                # 984
E_BITS = M.E_BITS                # 720
RV_K0 = F.RV_K0                  # {0:0, 1:246, 2:492, 3:738}
QAM_AMP = {2: 2000, 4: 894, 6: 436}

# QPSK at both redundancy versions plus 16QAM/64QAM at rv=0: the first two
# exercise the k0 offset, the last two the interleaver width and the level map.
VARIANTS = ((2, 0, M.N_RNTI), (4, 0, M.N_RNTI),
            (6, 0, M.N_RNTI), (2, 2, M.N_RNTI))


# ------------------------------------------------------------------ the chain
def info_bits(tb):
    """TB + CRC24A + filler, i.e. the K=120 LDPC information vector."""
    return list(tb) + M.crc24a(tb) + [0] * (K_BITS - A_TB - CRC_LEN)


def encode(tb):
    return L5.encode(info_bits(tb), KB, ZC)


def rate_match(cw, rv):
    """38.212 5.4.2.1: puncture, drop the NULL filler, select E bits at k0."""
    buf = cw[PUNCT:]
    usable = [b for i, b in enumerate(buf)
              if not (FILLER0 <= i < FILLER0 + FILLER_N)]
    assert len(usable) == USABLE, (len(usable), USABLE)
    k0 = RV_K0[rv]
    return [usable[(k0 + j) % USABLE] for j in range(E_BITS)], usable, k0


def scramble(e, n_rnti):
    """38.211 7.3.1.1 with q=0 and n_f=0 -> c_init = n_RNTI << 15."""
    scr = M.scrambling_seq(n_rnti)
    return [b ^ s for b, s in zip(e, scr)], scr


def bit_interleave(e_sc, qm):
    """38.212 5.4.2.2: write rows of Qm, read columns."""
    rows = len(e_sc) // qm
    return [e_sc[j * rows + i] for i in range(rows) for j in range(qm)]


def qam_map(f, qm):
    """38.211 7.3.1.2 square QAM, integer levels (2000 / 894 / 436)."""
    return QAM.qam_map(f, qm)


def layer_map(syms):
    """38.211 7.3.3.1 single layer, 7.3.3.2 single port: x = d, y = x.

    Kept as a named stage so the one-layer assumption is visible; a second
    layer would need a precoding matrix here instead of an identity.
    """
    return [(int(c.real), int(c.imag)) for c in syms]


def dl_mod(tb, qm, rv, n_rnti=M.N_RNTI):
    cw = encode(tb)
    e, usable, k0 = rate_match(cw, rv)
    e_sc, scr = scramble(e, n_rnti)
    f = bit_interleave(e_sc, qm)
    syms = qam_map(f, qm)
    y = layer_map(syms)
    return dict(tb=list(tb), info=info_bits(tb), cw=cw, usable=usable, e=e,
                k0=k0, scr=scr, e_sc=e_sc, f=f, syms=syms, y=y,
                qm=qm, rv=rv, n_rnti=n_rnti, nsym=len(y))


# ------------------------------------------------------------------- the dump
def pack(bits):
    out = [0] * ((len(bits) + 31) // 32)
    for i, b in enumerate(bits):
        if b:
            out[i >> 5] |= 1 << (i & 31)
    return out


def dump_td(path, variants=VARIANTS, tb=None):
    """Write only the stimulus: TB bits and the variant table."""
    tb = M.payload_bits() if tb is None else list(tb)
    assert len(tb) == A_TB
    words = [MAGIC, len(variants), A_TB, (A_TB + 31) // 32]
    words += pack(tb)                    # 3 words
    words += [0]                         # pad header to 8
    for qm, rv, rnti in variants:
        words += [qm, rv, rnti, 0]
    with open(path, "wb") as fh:
        for w in words:
            fh.write(struct.pack("<I", w & 0xFFFFFFFF))
    return len(words) * 4


def read_td(path):
    data = open(path, "rb").read()
    td = list(struct.unpack("<%dI" % (len(data) // 4), data))
    assert td[0] == MAGIC, "bad TD magic 0x%08x" % td[0]
    nvar, a_tb, tb_words = td[1], td[2], td[3]
    tb = [(td[4 + i // 32] >> (i % 32)) & 1 for i in range(a_tb)]
    variants = []
    for v in range(nvar):
        p = 8 + 4 * v
        variants.append((td[p], td[p + 1], td[p + 2]))
    return tb, variants, tb_words


# ----------------------------------------------------------------- self test
def selftest():
    ok = True
    tb = M.payload_bits()
    print("Downlink modulation chain (G2/B2): TB -> CRC24A -> LDPC -> rate "
          "match -> scramble -> interleave -> QAM")

    r0 = dl_mod(tb, 2, 0)
    # stage 1: the code block must agree with the other independent encoder
    same_cw = r0["cw"] == LN.encode(2, r0["info"], KB, ZC)
    ok &= same_cw
    print("  code block vs ldpc_nr.encode(BG2, kb=6, Zc=20) : %s"
          % ("OK" if same_cw else "FAIL"))

    # stage 2: rv=0 must reproduce pdsch_plan's own rate_match output
    e0, usable0, filler0 = M.rate_match(r0["cw"])
    same_rm = (r0["e"] == e0 and r0["usable"] == usable0 and r0["k0"] == 0)
    ok &= same_rm
    print("  rate match rv=0 vs pdsch_plan.rate_match      : %s (filler %d..%d)"
          % ("OK" if same_rm else "FAIL", filler0[0], filler0[-1]))

    r2 = dl_mod(tb, 2, 2)
    e2_ref, k0_ref = F.rate_select(usable0, 2)
    k0_ok = (r2["k0"] == 492 == k0_ref and r2["e"] == e2_ref)
    ok &= k0_ok
    print("  rv=2 k0=492 vs pdsch_full_plan.rate_select    : %s"
          % ("OK" if k0_ok else "FAIL"))

    # stage 3: scrambling against the model the receiver descrambles with
    scr_ok = r0["scr"] == M.scrambling_seq()
    ok &= scr_ok
    print("  scrambling c_init = n_RNTI << 15              : %s"
          % ("OK" if scr_ok else "FAIL"))

    # stage 4/5: QPSK path against the hardware-verified QPSK mapper
    f_ok = r0["f"] == F.interleave(r0["e_sc"])
    sym_ok = r0["syms"] == M.qpsk_map(r0["f"])
    ok &= f_ok and sym_ok
    print("  QPSK: interleave + map vs pdsch_full_plan      : %s / %s"
          % ("OK" if f_ok else "FAIL", "OK" if sym_ok else "FAIL"))

    for qm in (4, 6):
        r = dl_mod(tb, qm, 0)
        a = QAM_AMP[qm]
        flat = [v for p in r["y"] for v in p]
        mags = {abs(v) // a for v in flat}
        want = {1, 3} if qm == 4 else {1, 3, 5, 7}
        m_ok = mags == want and len(r["y"]) == E_BITS // qm
        de_ok = AMC.deinterleave(r["f"], qm) == r["e_sc"]
        ok &= m_ok and de_ok
        print("  Qm=%d: %3d symbols, levels %s, deinterleave %s"
              % (qm, len(r["y"]), sorted(mags), "OK" if de_ok else "FAIL"))
        if not m_ok:
            print("        FAIL expected levels %s" % sorted(want))

    # injections: each must actually change the produced symbols
    bad_rv = dl_mod(tb, 2, 1)
    ok &= bad_rv["e"] != r0["e"]
    bad_rnti = dl_mod(tb, 2, 0, M.N_RNTI ^ 1)
    ok &= bad_rnti["e_sc"] != r0["e_sc"]
    bad_qm = dl_mod(tb, 4, 0)
    ok &= bad_qm["f"] != r0["f"]
    print("  injections wrong rv / RNTI / Qm change output  : OK")

    print("[ %s ] downlink modulation chain" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="downlink modulation golden model")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    d = sub.add_parser("dump-td")
    d.add_argument("--out", required=True)
    a = ap.parse_args()
    if a.cmd == "dump-td":
        n = dump_td(a.out)
        print("wrote %s (%d bytes, %d variants, TB %d bits)"
              % (a.out, n, len(VARIANTS), A_TB))
        return 0
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
