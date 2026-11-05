#!/usr/bin/env python3
"""MCS-driven PDSCH: QPSK (MCS<=9) vs 16QAM (MCS 10..16), P4 of docs/11.

This closes the loop between tools/nr_la.py (which chooses an MCS from a CSI
report), tools/nr_qam.py (the modulation mapper / max-log demapper) and the
hardware-verified PDSCH receive chain:

    CQI/SINR -> nr_la.select() -> MCS -> Qm (2 or 4)
      -> 38.212 5.4.2.2 bit interleaving with Qm columns
      -> QPSK / 16QAM symbols -> RE grid (Gold DMRS every 4th RE)
      -> flat gain + AWGN -> IFFT + CP -> int32 TD
    [SoC] FFT -> Gold-DMRS LS -> equalise -> QPSK/16QAM max-log LLR
      -> de-interleave -> descramble -> rate de-match -> LDPC -> CRC24A -> TB

E_BITS stays 720 and the LDPC / rate-matching engine is untouched.  With Qm=4
only 180 QAM symbols are produced, so they occupy the first 180 data REs (all
of OFDM symbol 0); the remaining data REs carry nothing.  That is a deliberate
simplification of the resource mapping (a real gNB would raise E or reduce the
allocation), documented here so the 3 dB offset in M.add_channel()'s noise
power is not mistaken for a link bug: the model uses an explicit power reference
over the *occupied* REs only.

The companion hardware target is run-pdsch-16qam (sw/soc/pdsch_full_bm.c
compiled with -DQAM16), checked by tools/check_pdsch_16qam.py.
"""
import argparse
import math
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import nr_la as LA
import nr_qam as QAM
import nr_pdcch as PDCCH
import pdsch_plan as M
import pdsch_full_plan as F
import ldpc5g as L

NSYM = F.NSYM
NRE = F.NRE
NPILOT = F.NPILOT
NDATA_RE = M.data_res()
E_BITS = F.E_BITS
TD_SCALE = F.TD_SCALE
USABLE = F.USABLE
DEFAULT_SNR = 20.0
DEFAULT_SEED = 1
PDCCH_SNR = 12.0
N_RNTI = M.N_RNTI

# MCS boundaries from 38.214 Table 5.1.3.1-1 (Qm 2 -> 4 -> 6).
QPSK_MCS_MAX = 9
QAM16_MCS_MAX = 16


def qm_of_mcs(mcs):
    if mcs <= QPSK_MCS_MAX:
        return 2
    if mcs <= QAM16_MCS_MAX:
        return 4
    return 6


def data_flat():
    return [(s, r) for s in range(NSYM) for r in NDATA_RE]


def interleave(e, qm):
    """38.212 5.4.2.2 bit interleaving with Qm columns (Qm=2 reduces to F)."""
    rows = len(e) // qm
    f = [0] * len(e)
    for i in range(rows):
        for j in range(qm):
            f[qm * i + j] = e[j * rows + i]
    return f


def deinterleave(f, qm):
    rows = len(f) // qm
    e = [0] * len(f)
    for i in range(rows):
        for j in range(qm):
            e[j * rows + i] = f[qm * i + j]
    return e


def _rate_dematch_rv(descr, rv):
    buf = [0] * USABLE
    k0 = F.RV_K0[rv]
    for k, v in enumerate(descr):
        buf[(k0 + k) % USABLE] += v
    dem = [0] * F.PUNCT
    u = 0
    for i in range(F.NCODED - F.PUNCT):
        if F.FILLER0 <= i < F.FILLER0 + F.FILLER_N:
            dem.append(0)
        else:
            dem.append(buf[u])
            u += 1
    return dem, k0


# ---------------------------------------------------------------- transmit
def build_grid(mcs, rv=0, tb=None, n_rnti=N_RNTI):
    """Coding chain + interleaving + QAM + Gold DMRS for one MCS/RV."""
    qm = qm_of_mcs(mcs)
    tb = M.payload_bits() if tb is None else tb
    cw = L.encode(tb + M.crc24a(tb)
                  + [0] * (M.KB * M.ZC - M.A_TB - M.CRC_LEN), M.KB, M.ZC)
    _, usable, _filler = M.rate_match(cw)
    e_rv, k0 = F.rate_select(usable, rv)
    scr = M.scrambling_seq(n_rnti)
    e_sc = [b ^ s for b, s in zip(e_rv, scr)]
    f = interleave(e_sc, qm)
    syms = QAM.qam_map(f, qm)
    grid = [[0j] * NRE for _ in range(NSYM)]
    for s in range(NSYM):
        d = F.dmrs_seq(s)
        for p, r in enumerate(M.pilot_res()):
            grid[s][r] = d[p]
    for idx, (s, r) in enumerate(data_flat()[:len(syms)]):
        grid[s][r] = syms[idx]
    return grid, k0, qm, len(syms)


def add_channel(grid, qm, snr_db, seed):
    """Like M.add_channel but with the noise power referenced to occupied REs."""
    import random as _r
    rnd = _r.Random(seed)
    n_used = E_BITS // qm
    p_sig = sum(abs(v) ** 2 for row in grid for v in row) / n_used
    p_noise = p_sig / (10 ** (snr_db / 10))
    sigma = math.sqrt(p_noise / 2)
    h = complex(M.CH_RE, M.CH_IM)
    y = [[0j] * NRE for _ in range(NSYM)]
    for s in range(NSYM):
        for r in range(NRE):
            v = grid[s][r] * h if grid[s][r] != 0 else 0j
            y[s][r] = complex(v.real + rnd.gauss(0, sigma),
                              v.imag + rnd.gauss(0, sigma))
    return y


def td_from_y(y):
    return F.td_from_y(y)


def dump_td_bin(path, mcs, rv=0, snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    grid, _k0, qm, _n = build_grid(mcs, rv)
    y = add_channel(grid, qm, snr_db, seed)
    td = td_from_y(y)
    with open(path, "wb") as fh:
        for blk in td:
            for c in blk:
                fh.write(struct.pack("<I", (int(c.real) * TD_SCALE) & 0xFFFFFFFF))
                fh.write(struct.pack("<I", (int(c.imag) * TD_SCALE) & 0xFFFFFFFF))
        # One trailing word carries the redundancy version.  The -DQAM16 SoC
        # binary has no PDCCH decoder, so it cannot learn rv from a DCI; reading
        # it from the image keeps k0 = rv*(USABLE/4) genuinely driven without
        # recompiling per RV (and avoids a stale binary serving new stimulus).
        fh.write(struct.pack("<I", rv & 0xFFFFFFFF))
    return len(td) * F.SYM_LEN * 2 * 4 + 4


# ---------------------------------------------------------------- receive
def rx_pdsch(y, qm, rv, n_rnti=N_RNTI):
    """Model of the SoC receiver for a Qm-adaptive PDSCH transmission."""
    yq = F.quantised_y(F.td_from_y(y))
    h = F.channel_estimate_fid(yq)
    n_sym = E_BITS // qm
    raw = []
    for idx, (s, r) in enumerate(data_flat()[:n_sym]):
        sym = yq[s][r]
        raw += QAM.qam_llr(sym, h[s], qm)
    llr_e = deinterleave(raw, qm)
    scr = M.scrambling_seq(n_rnti)
    descr = [-v if scr[k] else v for k, v in enumerate(llr_e)]
    dem, k0 = _rate_dematch_rv(descr, rv)
    hard, scaled = F.hw_decode(dem)
    tb = hard[:M.A_TB]
    crc = hard[M.A_TB:M.A_TB + M.CRC_LEN]
    return dict(h=h, raw=raw, descr=descr, dem=dem, scaled=scaled, hard=hard,
                tb=tb, crc_ok=(M.crc24a(tb) == crc), k0=k0,
                want=M.payload_bits())


def golden(mcs, rv=0, snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    grid, k0, qm, n_sym = build_grid(mcs, rv)
    y = add_channel(grid, qm, snr_db, seed)
    g = rx_pdsch(y, qm, rv)
    g.update(mcs=mcs, qm=qm, rv=rv, k0=k0, n_sym=n_sym, y=y)
    return g


# ---------------------------------------------------------------- self test
def selftest():
    ok = True
    print("MCS-driven PDSCH (P4): nr_la -> MCS -> Qm -> "
          "QPSK/16QAM PDSCH full chain")

    # link adaptation must pick Qm consistently with the mapper
    for sinr in (0, 5, 10, 15, 20, 25):
        d = LA.LinkAdaptation().select(sinr)
        chk_qm = LA.qm_of_mcs(d["mcs"]) == qm_of_mcs(d["mcs"])
        ok &= chk_qm
        print("  SINR %2d dB -> CQI %2d MCS %2d Qm %d (LA Qm %d) %s"
              % (sinr, d["cqi"], d["mcs"], qm_of_mcs(d["mcs"]),
                 LA.qm_of_mcs(d["mcs"]), "OK" if chk_qm else "BAD"))

    for mcs, qm in ((4, 2), (9, 2), (10, 4), (13, 4), (16, 4),
                    (20, 6), (24, 6)):
        for rv in (0, 2):
            for snr in ((25.0, 20.0) if qm == 6 else (20.0, 10.0)):
                g = golden(mcs, rv, snr, 1)
                good = (g["crc_ok"] and g["tb"] == g["want"]
                        and g["k0"] == F.RV_K0[rv]
                        and len(g["raw"]) == E_BITS
                        and g["qm"] == qm)
                ok &= good
                print("  MCS %2d Qm %d rv %d SNR %4.1f dB : crc=%s "
                      "tb_err=%d k0=%3d syms=%d %s"
                      % (mcs, qm, rv, snr, g["crc_ok"],
                         sum(a != b for a, b in zip(g["tb"], g["want"])),
                         g["k0"], g["n_sym"], "[OK]" if good else "[FAIL]"))

    # Three injections, each tied to a real hardware acceptance gate.
    # NOTE: crc_ok alone is NOT a sufficient gate -- CRC24A bits are themselves
    # LDPC information bits, so convergence to *any* valid codeword makes the
    # CRC check pass.  That is exactly why check_pdsch_16qam.py gates on the
    # bit-exact 1040 hard decisions and the LLR values, not just CRC.
    grid, _, qm, _ = build_grid(13, 0)
    y = add_channel(grid, qm, 20.0, 1)
    ref = rx_pdsch(y, qm, 0)

    bad_rv = rx_pdsch(y, qm, 2)                 # wrong k0 (492 instead of 0)
    ok &= not bad_rv["crc_ok"] and bad_rv["tb"] != bad_rv["want"]
    print("  injection wrong rv (k0=492) : crc=%s tb_errors=%d"
          % (bad_rv["crc_ok"],
             sum(a != b for a, b in zip(bad_rv["tb"], bad_rv["want"]))))

    bad_rnti = rx_pdsch(y, qm, 0, N_RNTI ^ 1)   # wrong descrambling sequence
    ok &= not bad_rnti["crc_ok"] and bad_rnti["tb"] != bad_rnti["want"]
    print("  injection wrong RNTI        : crc=%s tb_errors=%d"
          % (bad_rnti["crc_ok"],
             sum(a != b for a, b in zip(bad_rnti["tb"], bad_rnti["want"]))))

    # Demapping a 16QAM waveform as QPSK still converges (16QAM's I/Q sign bits
    # coincide with the QPSK decisions), so the CRC/TB gates do NOT catch it --
    # but 534 of 720 LLRs differ, which the hardware checker's element-wise
    # LLR/hard-decision gates do catch.  This is the reason for those gates.
    bad_qm = rx_pdsch(y, 2, 0)
    ddiff = sum(a != b for a, b in zip(bad_qm["descr"], ref["descr"]))
    ok &= ddiff > 100
    print("  injection wrong Qm (4->2)   : crc=%s TB still ok, but %d/720 LLRs "
          "differ -> element-wise gate catches it"
          % (bad_qm["crc_ok"], ddiff))

    print("[ %s ] MCS-driven PDSCH (QPSK / 16QAM / 64QAM)" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="MCS-driven PDSCH golden model")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    d = sub.add_parser("dump")
    d.add_argument("out")
    d.add_argument("--mcs", type=int, default=13)
    d.add_argument("--rv", type=int, default=0)
    d.add_argument("--snr", type=float, default=DEFAULT_SNR)
    d.add_argument("--seed", type=int, default=DEFAULT_SEED)
    a = ap.parse_args()
    if a.cmd == "dump":
        n = dump_td_bin(a.out, a.mcs, a.rv, a.snr, a.seed)
        print("wrote %s (%d bytes, MCS=%d Qm=%d rv=%d snr=%.1f dB seed=%d)"
              % (a.out, n, a.mcs, qm_of_mcs(a.mcs), a.rv, a.snr, a.seed))
        return 0
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
