#!/usr/bin/env python3
"""PUSCH (SISO) golden model + binary time-domain stimulus.

Uplink data channel, modelled as the mirror image of the downlink PDSCH chain
(tools/pdsch_full_plan.py) so that a future ``run-pusch`` SoC benchmark can
reuse the *entire* receive pipeline.  The three 3GPP differences vs PDSCH are
implemented explicitly:

  * PUSCH DMRS        -- 38.211 6.4.1.1 Gold sequence (different c_init than the
                        7.4.1.1 PDSCH DMRS), one QPSK pilot every 4th RE,
                        configuration type 1 (same density as the PDSCH model so
                        the per-symbol LS estimate is unchanged).
  * PUSCH scrambling  -- 38.211 6.3.1.1 (q=0, n_ID = N_ID^cell); here simplified
                        to c_init = n_RNTI << 15, identical to the PDSCH model so
                        the DCI -> RNTI coupling stays consistent.
  * Transform precoding -- optional DFT-s-OFDM (38.211 6.1.3).  When enabled the
                        360 QAM symbols are M-point-DFT spread across the 360 data
                        REs; the receiver does an IDFT *after* per-subcarrier
                        equalisation and before QAM demod.  When disabled the
                        link is structurally identical to PDSCH.

Everything else (TB -> CRC24A -> LDPC BG2 -> rate match -> QPSK -> RE grid ->
flat gain + AWGN -> IFFT + CP -> int32 TD dump, and the reverse) reuses the
*exact same* helpers as the PDSCH model, so the two chains are bit-for-bit
aligned wherever they overlap.

Constants below MUST stay in sync with sw/soc/pusch_bm.c (future Phase B).
"""
import argparse
import cmath
import math
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdsch_plan as M
import ldpc5g as L
import nr_pbch as NR
import check_pdsch_iter as CI
from ssb_plan import _fft

# ---- layout / scalars (shared with the PDSCH model and the C benchmark) -----
NSYM = 2                      # PUSCH occupies two OFDM symbols
NRE = 240
NPILOT = 60
NDATA_RE = 180
E_BITS = 720                  # QPSK: 2 bits per data RE, 360 data REs
NFFT = 4096
CP_LEN = 288
TD_SCALE = 256
BIN0 = NFFT // 2 + 1          # RE r -> bin BIN0 + r (keeps clear of DC)
SYM_LEN = CP_LEN + NFFT
LLR_SHIFT = 8                 # real LLRs are ~1e4; keep them << SENTINEL
ITERS = 8                     # hardware iteration count (pure min-sum)
ZC = 20
MAXD = 10
SENTINEL = 32767
NCODED = 52 * ZC
PUNCT = 2 * ZC
USABLE = 984                  # NCODED - PUNCT - 16 filler bits
FILLER0 = 64                 # filler positions in buffer coordinates
FILLER_N = 16
DEFAULT_SNR = 20.0
DEFAULT_SEED = 1

# ---- HARQ / redundancy versions (38.212 5.4.2.1) ---------------------------
RV_K0 = {0: 0, 1: USABLE // 4, 2: USABLE // 2, 3: 3 * USABLE // 4}
RV_SEQ = (0, 2)               # two transmissions: rv=0 then rv=2
NTX = len(RV_SEQ)

# ---- PUSCH DMRS configuration (38.211 6.4.1.1, configuration type 1) -------
PUSCH_N_DMRS_SYMB = NSYM      # model as multi-symbol DMRS (one pilot per symbol)
PUSCH_L0 = 0                  # first DMRS symbol index inside the slot
PUSCH_NSLOT = 0
PUSCH_NSCID = 0
PUSCH_NID = M.N_RNTI          # n_ID = n_RNTI (simplified, cf. PDSCH model)
QM = 2                        # QPSK bits per symbol (38.212 5.4.2.2)


def bin_of(r):
    return BIN0 + r


# ----------------------------------------------------------- PUSCH DMRS (6.4.1.1)
def dmrs_cinit_pusch(s, nid=PUSCH_NID):
    """38.211 6.4.1.1: c_init of the PUSCH DMRS Gold sequence r(n).

    l is the OFDM symbol index *within the DMRS set* (here l = L0 + s, with both
    PUSCH symbols carrying a pilot).  N_DMRS_symb is the number of DMRS symbols
    configured for this allocation.  n_ID = n_RNTI (see PUSCH_NID).
    """
    l = PUSCH_L0 + s
    return ((1 << 17) * (PUSCH_N_DMRS_SYMB * PUSCH_NSLOT + l + 1)
            * (2 * nid + 1)
            + 2 * nid + PUSCH_NSCID) % (1 << 31)


def dmrs_seq_pusch(s, nid=PUSCH_NID):
    """NPILOT complex QPSK pilots for OFDM symbol s, from the Gold sequence."""
    c = NR.gold(dmrs_cinit_pusch(s, nid), 2 * NPILOT)
    return [complex((1 - 2 * c[2 * p]) * M.QPSK,
                    (1 - 2 * c[2 * p + 1]) * M.QPSK)
            for p in range(NPILOT)]


# ----------------------------------------------------- PUSCH scrambling (6.3.1.1)
def pusch_scrambling_seq(n_rnti=M.N_RNTI):
    """38.211 6.3.1.1 PUSCH data scrambling (q=0, n_ID = N_ID^cell).

    Simplified here to c_init = n_RNTI << 15 so the DCI -> RNTI coupling matches
    the PDSCH model and the future hardware benchmark.
    """
    return NR.gold((n_rnti << 15) % (1 << 31), E_BITS)


# ----------------------------------------------------------- bit interleaving
def interleave(e):
    """38.212 5.4.2.2 bit interleaving (QPSK, Qm=2: column write / row read)."""
    rows = len(e) // QM
    f = [0] * len(e)
    for i in range(rows):
        for j in range(QM):
            f[QM * i + j] = e[j * rows + i]
    return f


def deinterleave(f):
    """Inverse of interleave(), applied to the LLR vector."""
    rows = len(f) // QM
    e = [0] * len(f)
    for i in range(rows):
        for j in range(QM):
            e[j * rows + i] = f[QM * i + j]
    return e


def rate_select(usable, rv):
    """38.212 5.4.2.1 bit selection with the redundancy-version offset k0."""
    k0 = RV_K0[rv]
    return [usable[(k0 + j) % len(usable)] for j in range(E_BITS)], k0


# ----------------------------------------------------- flat data-RE ordering
def data_flat():
    """Enumerate (symbol, RE) pairs in the order QAM symbols are placed/read.

    Mirrors pdsch_plan.build_grid: for s in range(NSYM): for r in data_res().
    Length = NSYM * NDATA_RE = 360.  Used both for plain placement and for the
    DFT-s-OFDM spread (M = 360).
    """
    return [(s, r) for s in range(NSYM) for r in M.data_res()]


# ----------------------------------------------------- DFT-s-OFDM transform
def dft(x):
    """M-point DFT (M = len(x)).  idft(dft(x)) == x with the normalisation below."""
    n = len(x)
    return [sum(x[k] * cmath.exp(-2j * math.pi * m * k / n) for k in range(n))
            for m in range(n)]


def idft(x):
    """M-point IDFT, 1/M normalised so idft(dft(x)) == x."""
    n = len(x)
    return [sum(x[k] * cmath.exp(2j * math.pi * m * k / n) for k in range(n)) / n
            for m in range(n)]


# ----------------------------------------------------------- coding chain
RM_INFO = (None, None)        # (usable_len, filler_idx) set by build_pusch_grid


def build_pusch_grid_tb(tb, rv=0, tp=False, rnti=M.N_RNTI):
    """Coding chain + interleaving + Gold DMRS for an explicit transport block.

    Returns the NSYM x NRE frequency-domain RE grid (pilots on every 4th RE).
    When ``tp`` is True the 360 QAM symbols are DFT-spread across the 360 data
    REs; otherwise they map straight onto the data REs (== PDSCH structure).
    ``rnti`` is n_RNTI: it seeds the 38.211 6.3.1.1 data scrambling, which is
    what lets Msg3 be addressed by a TC-RNTI (tools/nr_rach.py) instead of the
    default C-RNTI.  The 6.4.1.1 DMRS keeps the module-level n_ID (PUSCH_NID):
    deriving it from n_RNTI as well would need a runtime Gold sequence per RNTI
    on the CGRA side, so run-pusch and run-rach share one pilot set.
    """
    cw = L.encode(tb + M.crc24a(tb) + [0] * (M.KB * M.ZC - M.A_TB - M.CRC_LEN),
                  M.KB, M.ZC)
    _, usable, filler = M.rate_match(cw)
    global RM_INFO
    RM_INFO = (len(usable), filler)
    e_rv, _ = rate_select(usable, rv)
    scr = pusch_scrambling_seq(rnti)
    e_sc = [b ^ s for b, s in zip(e_rv, scr)]
    f = interleave(e_sc)
    syms = M.qpsk_map(f)                       # 360 QAM symbols
    grid = [[0j] * NRE for _ in range(NSYM)]
    for s in range(NSYM):
        d = dmrs_seq_pusch(s)
        for p, r in enumerate(M.pilot_res()):
            grid[s][r] = d[p]
    flat = data_flat()
    if tp:
        F = dft(syms)                          # M-point spread
        # Normalise so the *total* transmit power equals the TP-off case: each
        # spread symbol carries 1/sqrt(M) of its energy per RE.  This keeps the
        # per-symbol post-equalisation SNR identical to TP-off (DFT-s-OFDM buys
        # PAPR reduction, not SNR); without it the M-fold RE power boost inflates
        # the channel noise and corrupts the pilot-based channel estimate.
        norm = 1.0 / math.sqrt(len(F))
        for m, (s, r) in enumerate(flat):
            grid[s][r] = F[m] * norm
    else:
        for m, (s, r) in enumerate(flat):
            grid[s][r] = syms[m]
    return grid


def build_pusch_grid(rv=0, tp=False):
    """Coding chain for the default (all-zero MAC payload) transport block."""
    return build_pusch_grid_tb(M.payload_bits(), rv, tp)


# ----------------------------------------------------------- OFDM / TD
def td_from_y(y):
    """Frequency-domain RE grid -> integer TD symbols with CP (reuses PDSCH form)."""
    td = []
    for s in range(NSYM):
        freq = [0j] * NFFT
        for r in range(NRE):
            freq[bin_of(r)] = y[s][r]
        sig = _fft(freq, inverse=True)         # normalised IFFT (1/N)
        samples = [complex(int(round(c.real)), int(round(c.imag))) for c in sig]
        td.append(NR.add_cp(samples, CP_LEN))
    return td


def build_td_pusch(rv=0, snr_db=DEFAULT_SNR, seed=DEFAULT_SEED, tp=False):
    """Single transmission -> (TD blocks, freq grid)."""
    grid = build_pusch_grid(rv, tp)
    y = M.add_channel(grid, snr_db, seed)
    return td_from_y(y), y


def quantised_y(td):
    """What an ideal receiver sees after stripping CP + FFT.  Same as PDSCH."""
    yq = []
    for s in range(NSYM):
        spec = _fft(td[s][CP_LEN:CP_LEN + NFFT], inverse=False)
        yq.append([spec[bin_of(r)] for r in range(NRE)])
    return yq


# ----------------------------------------------------------- receiver front-end
def pusch_channel_estimate(yq):
    """Per-symbol LS estimate against the 38.211 6.4.1.1 Gold DMRS sequence."""
    h = []
    den = M.NPILOT * 2 * M.QPSK * M.QPSK // M.H_SCALE
    for s in range(NSYM):
        d = dmrs_seq_pusch(s)
        acc = 0j
        for p, r in enumerate(M.pilot_res()):
            acc += yq[s][r] * complex(d[p].real, -d[p].imag)
        h.append(complex(int(acc.real // den), int(acc.imag // den)))
    return h


def pusch_extract_llr(yq, h, tp):
    """QPSK soft LLRs in interleaved (f) order, ready for deinterleave().

    tp=False : per-data-RE matched filter (identical to PDSCH extract_llr).
    tp=True  : per-subcarrier matched filter -> flat vector -> IDFT -> LLR of the
               recovered time-domain QAM symbol (DFT-s-OFDM deprecoding).
    """
    if not tp:
        return M.extract_llr(yq, h)
    A = [yq[s][r] * complex(h[s].real, -h[s].imag) for (s, r) in data_flat()]
    T = idft(A)                                # 360 QAM-symbol estimates
    llr = []
    for x in T:
        llr += [int(x.real), int(x.imag)]
    return llr


def hw_decode(dem, iters=ITERS):
    """Bit-exact model of the hardware LDPC engine (pure min-sum, no scaling)."""
    llr = [v >> LLR_SHIFT for v in dem]
    edges = CI.edges_table()
    msg = [[[0] * ZC for _ in range(MAXD)] for _ in range(42)]
    for r, row in enumerate(edges):
        for k in range(MAXD):
            if k < len(row):
                c, sh = row[k]
                msg[r][k] = [llr[c * ZC + (i + sh) % ZC] for i in range(ZC)]
            else:
                msg[r][k] = [SENTINEL] * ZC
    hard = CI.iterate(llr, msg, edges, iters)
    return [hard[b][i] for b in range(52) for i in range(ZC)], llr


# ----------------------------------------------------------- golden anchors
def pusch_golden(snr_db=DEFAULT_SNR, seed=DEFAULT_SEED, rv=0, tp=False):
    """All intermediate anchors of the receive chain, plus the expected TB."""
    td, y = build_td_pusch(rv, snr_db, seed, tp)
    yq = quantised_y(td)
    h = pusch_channel_estimate(yq)
    raw = pusch_extract_llr(yq, h, tp)        # LLRs in interleaved (f) order
    llr_e = deinterleave(raw)                 # back to rate-match (e) order
    scr = pusch_scrambling_seq()
    descr = [-v if scr[k] else v for k, v in enumerate(llr_e)]
    dem = M.rate_dematch(descr, *RM_INFO)
    hard, scaled = hw_decode(dem)
    tb = hard[:M.A_TB]
    crc = hard[M.A_TB:M.A_TB + M.CRC_LEN]
    want = M.payload_bits()
    return dict(
        yq=yq, h=h, raw=raw, descr=descr, scaled=scaled, hard=hard, tb=tb,
        crc=crc, want=want,
        crc_ok=(M.crc24a(tb) == crc),
        tb_errors=sum(a != b for a, b in zip(tb, want)),
    )


def soft_combine_pusch(snr_db=DEFAULT_SNR, seed=DEFAULT_SEED, tp=False):
    """Golden anchors for the two-transmission Chase combiner (rv=0/2)."""
    buf = [0] * USABLE
    hs, raws, descrs = [], [], []
    yq0 = None
    for tx, rv in enumerate(RV_SEQ):
        _, y = build_td_pusch(rv, snr_db, seed + tx, tp)
        yq = quantised_y(td_from_y(y))
        if tx == 0:
            yq0 = yq
        h = pusch_channel_estimate(yq)
        raw = pusch_extract_llr(yq, h, tp)
        llr_e = deinterleave(raw)
        scr = pusch_scrambling_seq()
        descr = [-v if scr[k] else v for k, v in enumerate(llr_e)]
        k0 = RV_K0[rv]
        for k, v in enumerate(descr):
            buf[(k0 + k) % USABLE] += v
        hs.append(h); raws.append(raw); descrs.append(descr)
    dem = [0] * PUNCT
    u = 0
    for i in range(NCODED - PUNCT):
        if FILLER0 <= i < FILLER0 + FILLER_N:
            dem.append(0)
        else:
            dem.append(buf[u]); u += 1
    return dem, hs, raws, descrs, yq0


def pusch_golden_harq(snr_db=DEFAULT_SNR, seed=DEFAULT_SEED, tp=False):
    """Expected hard decisions / TB after Chase combining (rv=0/2)."""
    dem, hs, raws, descrs, yq0 = soft_combine_pusch(snr_db, seed, tp)
    hard, scaled = hw_decode(dem)
    tb = hard[:M.A_TB]
    crc = hard[M.A_TB:M.A_TB + M.CRC_LEN]
    want = M.payload_bits()
    return dict(dem=dem, h=hs[0], raw=raws[0], descr=descrs[0], yq=yq0, hard=hard,
                scaled=scaled, tb=tb, crc=crc, want=want,
                crc_ok=(M.crc24a(tb) == crc),
                tb_errors=sum(a != b for a, b in zip(tb, want)))


# ----------------------------------------------------------- TD dump (for Phase B)
def dump_td_bin(path, rv=0, snr_db=DEFAULT_SNR, seed=DEFAULT_SEED, tp=False):
    """Binary int32 TD dump: NSYM x SYM_LEN complex samples, re/im interleaved."""
    td, _ = build_td_pusch(rv, snr_db, seed, tp)
    with open(path, "wb") as f:
        for s in range(NSYM):
            for c in td[s]:
                f.write(struct.pack("<I", (int(c.real) * TD_SCALE) & 0xFFFFFFFF))
                f.write(struct.pack("<I", (int(c.imag) * TD_SCALE) & 0xFFFFFFFF))
    return len(td) * SYM_LEN * 2 * 4


# ----------------------------------------------------------- self test
def selftest():
    ok = True
    print("PUSCH golden model self-test (TB=80, BG2 Zc=20, QPSK, 2x180 data REs)")
    for tp in (False, True):
        tag = "DFT-s-OFDM (TP on)" if tp else "no transform precoding"
        print("  -- %s --" % tag)
        for snr in (20.0, 0.0, -6.0):
            g = pusch_golden(snr, 1, 0, tp)
            good = g["crc_ok"] and g["tb_errors"] == 0
            ok &= good
            print("    snr=%6.1f dB seed=1 : crc_ok=%-5s tb_errors=%d%s"
                  % (snr, g["crc_ok"], g["tb_errors"],
                     "  [OK]" if good else "  [FAIL]"))
        # HARQ rv=0/2 Chase combining (proves DCI rv -> k0 coupling on the UL)
        gh = pusch_golden_harq(20.0, 1, tp)
        good = gh["crc_ok"] and gh["tb_errors"] == 0
        ok &= good
        print("    HARQ rv=0/2  seed=1 : crc_ok=%-5s tb_errors=%d%s"
              % (gh["crc_ok"], gh["tb_errors"],
                 "  [OK]" if good else "  [FAIL]"))
    print("[ %s ] PUSCH golden model" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="PUSCH SISO golden model")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("selftest")
    d = sub.add_parser("dump")
    d.add_argument("out")
    d.add_argument("--rv", type=int, default=0)
    d.add_argument("--snr", type=float, default=DEFAULT_SNR)
    d.add_argument("--seed", type=int, default=DEFAULT_SEED)
    d.add_argument("--tp", action="store_true", help="enable DFT-s-OFDM")
    a = ap.parse_args()

    if a.cmd == "dump":
        n = dump_td_bin(a.out, a.rv, a.snr, a.seed, a.tp)
        print("wrote %s (%d bytes, rv=%d, snr=%.1f dB, seed=%d, tp=%s)"
              % (a.out, n, a.rv, a.snr, a.seed, a.tp))
        return 0

    return selftest()


if __name__ == "__main__":
    sys.exit(main())
