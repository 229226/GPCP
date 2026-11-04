#!/usr/bin/env python3
"""PDSCH (SISO) full receive chain: golden model + binary time-domain stimulus.

This closes the gap left by ``tools/pdsch_plan.py`` (which only models the link)
and ``sw/soc/pdsch_iter_bm.c`` (which feeds LCG-random LLRs into the LDPC
engine).  Here the stimulus is an actual OFDM waveform that the SoC has to
demodulate itself:

    TB -> CRC24A -> LDPC BG2 -> rate match -> scramble
       -> 38.212 5.4.2.2 bit interleaving -> QPSK -> RE grid
          (38.211 7.4.1.1 Gold DMRS on every 4th RE)
       -> flat gain + AWGN -> IFFT + CP -> int32 TD dump
    [SoC] FFT -> RE extract -> Gold-DMRS LS estimate -> equalise
       -> QPSK LLR -> de-interleave -> descramble -> rate de-match
       -> LLR scaling -> 8 pure-min-sum LDPC iterations -> CRC24A -> TB

Constants below MUST stay in sync with sw/soc/pdsch_full_bm.c.
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdsch_plan as M
import ldpc5g as L
import nr_pbch as NR
import check_pdsch_iter as CI
from ssb_plan import _fft

# ---- layout / scalars (shared with the C benchmark) -----------------------
NSYM = 2                      # PDSCH occupies two OFDM symbols
NRE = 240
NPILOT = 60
NDATA = 180
E_BITS = 720
NFFT = 4096
CP_LEN = 288
TD_SCALE = 256
BIN0 = NFFT // 2 + 1          # RE r -> bin BIN0 + r (keeps clear of DC)
SYM_LEN = CP_LEN + NFFT
LLR_SHIFT = 8                 # real LLRs are ~2e5; keep them << SENTINEL
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

# ---- 38.211 7.4.1.1 PDSCH DMRS (configuration type 1, one symbol) --------
DMRS_NID = 1                  # N_ID^SCID (DMRS scrambling identity)
DMRS_NSCID = 0
DMRS_NSLOT = 0
DMRS_L0 = 2                   # first DMRS symbol index inside the slot
DMRS_NSYMB_SLOT = 14
DMRS_NCP = 0                  # normal CP
QM = 2                        # QPSK bits per symbol (38.212 5.4.2.2)


def bin_of(r):
    return BIN0 + r


def dmrs_cinit(s):
    """38.211 7.4.1.1: c_init of the PDSCH DMRS Gold sequence r(n)."""
    l = DMRS_L0 + s
    return ((1 << 17) * (DMRS_NSYMB_SLOT * DMRS_NSLOT + l + 1)
            * (2 * DMRS_NID + DMRS_NSCID + 1)
            + 2 * DMRS_NID + DMRS_NCP) % (1 << 31)


def dmrs_seq(s):
    """NPILOT complex QPSK pilots for OFDM symbol s, from the Gold sequence."""
    c = NR.gold(dmrs_cinit(s), 2 * NPILOT)
    return [complex((1 - 2 * c[2 * p]) * M.QPSK,
                    (1 - 2 * c[2 * p + 1]) * M.QPSK)
            for p in range(NPILOT)]


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


def build_link_fid():
    """Coding chain of pdsch_plan.build_link + interleaving + Gold DMRS."""
    tb = M.payload_bits()
    cw = L.encode(tb + M.crc24a(tb) + [0] * (M.KB * M.ZC - M.A_TB - M.CRC_LEN),
                  M.KB, M.ZC)
    e, usable, filler = M.rate_match(cw)
    M.RM_INFO = (len(usable), filler)
    scr = M.scrambling_seq()
    e_sc = [b ^ s for b, s in zip(e, scr)]
    f = interleave(e_sc)
    syms = M.qpsk_map(f)
    grid = [[0j] * NRE for _ in range(NSYM)]
    for s in range(NSYM):
        d = dmrs_seq(s)
        for p, r in enumerate(M.pilot_res()):
            grid[s][r] = d[p]
    n = 0
    for s in range(NSYM):
        for r in M.data_res():
            grid[s][r] = syms[n]
            n += 1
    return grid


def channel_estimate_fid(yq):
    """Per-symbol LS estimate against the 38.211 Gold DMRS sequence."""
    h = []
    den = M.NPILOT * 2 * M.QPSK * M.QPSK // M.H_SCALE
    for s in range(NSYM):
        d = dmrs_seq(s)
        acc = 0j
        for p, r in enumerate(M.pilot_res()):
            acc += yq[s][r] * complex(d[p].real, -d[p].imag)
        h.append(complex(int(acc.real // den), int(acc.imag // den)))
    return h


def build_td(snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    """Transmit grid -> channel -> IFFT+CP -> integer time-domain samples."""
    grid = build_link_fid()                # Gold DMRS + 38.212 bit interleaving
    y = M.add_channel(grid, snr_db, seed)
    td = []
    for s in range(NSYM):
        freq = [0j] * NFFT
        for r in range(NRE):
            freq[bin_of(r)] = y[s][r]
        sig = _fft(freq, inverse=True)     # normalised IFFT (1/N)
        samples = [complex(int(round(c.real)), int(round(c.imag))) for c in sig]
        td.append(NR.add_cp(samples, CP_LEN))
    return td, y


def quantised_y(td):
    """What an ideal (floating-point) receiver sees after stripping CP + FFT.

    The C benchmark applies the same operation to TD_SCALE-scaled samples and
    shifts the result left by 4, so up to fixed-point FFT error its RE grid
    equals this one.  Golden anchors are computed from this grid, not from the
    pre-quantisation ``y``.
    """
    yq = []
    for s in range(NSYM):
        spec = _fft(td[s][CP_LEN:CP_LEN + NFFT], inverse=False)
        yq.append([spec[bin_of(r)] for r in range(NRE)])
    return yq


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


def golden(snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    """All intermediate anchors of the receive chain, plus the expected TB."""
    td, y = build_td(snr_db, seed)
    yq = quantised_y(td)
    h = channel_estimate_fid(yq)
    raw = M.extract_llr(yq, h)             # LLRs in interleaved (f) order
    llr_e = deinterleave(raw)              # back to rate-match (e) order
    scr = M.scrambling_seq()
    descr = [-v if scr[k] else v for k, v in enumerate(llr_e)]
    dem = M.rate_dematch(descr, *M.RM_INFO)
    hard, scaled = hw_decode(dem)
    tb = hard[:M.A_TB]
    crc = hard[M.A_TB:M.A_TB + M.CRC_LEN]
    want = M.payload_bits()
    return dict(
        td=td, y=y, yq=yq, h=h, raw=raw, descr=descr, dem=dem,
        scaled=scaled, hard=hard, tb=tb, crc=crc, want=want,
        crc_ok=(M.crc24a(tb) == crc),
        tb_errors=sum(a != b for a, b in zip(tb, want)),
    )


def dump_td_bin(path, snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    """Binary int32 TD dump: NSYM x SYM_LEN complex samples, re/im interleaved."""
    td, _ = build_td(snr_db, seed)
    with open(path, "wb") as f:
        for s in range(NSYM):
            for c in td[s]:
                f.write(struct.pack("<I", (int(c.real) * TD_SCALE) & 0xFFFFFFFF))
                f.write(struct.pack("<I", (int(c.imag) * TD_SCALE) & 0xFFFFFFFF))
    return len(td) * SYM_LEN * 2 * 4


def main():
    ap = argparse.ArgumentParser(description="PDSCH SISO full-chain stimulus")
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("dump")
    d.add_argument("out")
    d.add_argument("--snr", type=float, default=DEFAULT_SNR)
    d.add_argument("--seed", type=int, default=DEFAULT_SEED)
    s = sub.add_parser("selftest")
    s.add_argument("--snr", type=float, default=DEFAULT_SNR)
    s.add_argument("--seed", type=int, default=DEFAULT_SEED)
    a = ap.parse_args()

    if a.cmd == "dump":
        n = dump_td_bin(a.out, a.snr, a.seed)
        print("wrote %s (%d bytes, snr=%.1f dB, seed=%d)" % (a.out, n, a.snr, a.seed))
        return 0

    g = golden(a.snr, a.seed)
    print("PDSCH full chain @ %.1f dB / seed %d" % (a.snr, a.seed))
    print("  DMRS c_init      : sym0=0x%x sym1=0x%x (N_ID=%d, l0=%d)" % (
        dmrs_cinit(0), dmrs_cinit(1), DMRS_NID, DMRS_L0))
    print("  channel estimate : %s" % g["h"])
    print("  raw LLR min/max  : %d / %d" % (min(g["raw"]), max(g["raw"])))
    print("  scaled LLR range : %d / %d (sentinel %d)" % (
        min(g["scaled"]), max(g["scaled"]), SENTINEL))
    print("  de-match len     : %d (usable %d, filler %d..%d, punct %d)" % (
        len(g["dem"]), M.RM_INFO[0], FILLER0, FILLER0 + FILLER_N - 1, PUNCT))
    print("  CRC ok           : %s" % g["crc_ok"])
    print("  TB errors        : %d / %d" % (g["tb_errors"], M.A_TB))
    print("  TB               : %s" % "".join(str(b) for b in g["tb"]))
    return 0 if g["crc_ok"] and g["tb_errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
