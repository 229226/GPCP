#!/usr/bin/env python3
"""CSI-RS (38.211 7.4.1.5) + wideband CQI reporting - golden model.

Chain modelled here (and mirrored by sw/soc/csi_bm.c):

    CSI-RS QPSK sequence (Gold, c_init per 7.4.1.5)
      -> one OFDM symbol, density 3 (80 CSI-RS REs out of 240)
      -> flat channel h + AWGN
      -> 4096-point FFT -> CSI-RS RE extraction
      -> per-RE LS estimate A[i] = Y[r_i] * conj(r_i)
      -> Ps = |mean(A)|^2, Pn = sum|A[i]-A[i-1]|^2 / (2(N-1))
      -> snr_lin = Ps/Pn -> CQI (38.214 Table 5.2.2.1-2 gate, via nr_la)

The noise estimate uses the *difference* of adjacent LS estimates, so it is
blind to the front-end gain: a fixed-point FFT that is uniformly 10% low
scales Ps and Pn identically and leaves the SNR untouched.  That is why this
gate can be tight even though the absolute channel estimate is not.
"""
import argparse
import math
import os
import random
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import nr_la as LA
import pdsch_full_plan as F
import pdsch_plan as M

NFFT = 4096
CP_LEN = 288
NSYM = 1
NRE = 240
DENSITY = 3
NCSI = NRE // DENSITY                  # 80
CSI_NID = 1
CSI_NSLOT = 0
CSI_L = 0
N_SYMB_SLOT = 14
QPSK = 2000
TD_SCALE = 256
DEFAULT_SNR = 20.0
DEFAULT_SEED = 1


def csi_cinit():
    """38.211 7.4.1.5.2: c_init = (2^10 (N_symb*n_slot + l + 1)(2 n_ID + 1)
    + n_ID) mod 2^31."""
    v = ((1 << 10) * (N_SYMB_SLOT * CSI_NSLOT + CSI_L + 1)
         * (2 * CSI_NID + 1) + CSI_NID)
    return v & 0x7FFFFFFF


def gold_bits(c_init, n):
    """Length-31 Gold sequence, same LFSR convention as the C side."""
    nc = 1600
    x1 = [0] * (nc + n + 31)
    x2 = [0] * (nc + n + 31)
    x1[0] = 1
    for i in range(31):
        x2[i] = (c_init >> i) & 1
    for i in range(nc + n):
        x1[i + 31] = (x1[i + 3] + x1[i]) & 1
        x2[i + 31] = (x2[i + 3] + x2[i + 2] + x2[i + 1] + x2[i]) & 1
    return [(x1[nc + i] + x2[nc + i]) & 1 for i in range(n)]


def csi_rs_seq():
    """80 QPSK symbols at amplitude QPSK on each axis."""
    c = gold_bits(csi_cinit(), 2 * NCSI)
    return [complex((1 - 2 * c[2 * i]) * QPSK, (1 - 2 * c[2 * i + 1]) * QPSK)
            for i in range(NCSI)]


def csi_re():
    """CSI-RS subcarrier index inside the 240-RE window (density 3)."""
    return [i * DENSITY for i in range(NCSI)]


def build_grid():
    grid = [[0j] * NRE for _ in range(NSYM)]
    seq = csi_rs_seq()
    for i, r in enumerate(csi_re()):
        grid[0][r] = seq[i]
    return grid


def add_channel(grid, snr_db, seed):
    """Flat channel h = 2+j, AWGN referenced to the per-RE signal power."""
    rnd = random.Random(seed)
    h = complex(M.CH_RE, M.CH_IM)
    # SNR is defined on the *occupied* CSI-RS REs and includes the channel
    # gain: dividing by NRE would add 10*log10(density)=4.8 dB, forgetting
    # |h|^2 would add another 7 dB (both were real bugs in this model).
    p_sig = (abs(h) ** 2
             * sum(abs(v) ** 2 for row in grid for v in row) / (NSYM * NCSI))
    sigma = math.sqrt(p_sig / (10 ** (snr_db / 10.0)) / 2.0)
    y = [[grid[s][r] * h + complex(rnd.gauss(0, sigma), rnd.gauss(0, sigma))
          for r in range(NRE)] for s in range(NSYM)]
    return y, h


BIN0 = NFFT // 2 + 1


def td_from_y(y):
    """One-symbol frequency grid -> integer TD samples with CP.

    pdsch_full_plan.td_from_y hard-codes the two-symbol PDSCH layout, so the
    CSI-RS generator mirrors it locally for NSYM = 1.
    """
    freq = [0j] * NFFT
    for r in range(NRE):
        freq[BIN0 + r] = y[0][r]
    sig = F._fft(freq, inverse=True)
    samples = [complex(int(round(c.real)), int(round(c.imag))) for c in sig]
    return [F.NR.add_cp(samples, CP_LEN)]


def estimate(y):
    """LS + adjacent-difference noise estimate, then CQI."""
    seq = csi_rs_seq()
    s2 = 2.0 * QPSK * QPSK          # |s|^2, so A is an unbiased h estimate
    a = [y[0][r] * complex(seq[i].real, -seq[i].imag) / s2
         for i, r in enumerate(csi_re())]
    mr = sum(v.real for v in a) / NCSI
    mi = sum(v.imag for v in a) / NCSI
    ps = mr * mr + mi * mi
    acc = 0.0
    for i in range(1, NCSI):
        dr = a[i].real - a[i - 1].real
        di = a[i].imag - a[i - 1].imag
        acc += dr * dr + di * di
    pn = acc / (2.0 * (NCSI - 1))
    snr_db = 10.0 * math.log10(ps / pn) if pn > 0 else 99.0
    return dict(a=a, ps=ps, pn=pn, snr_db=snr_db,
                cqi=LA.cqi_from_sinr(snr_db))


def golden(snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    grid = build_grid()
    y, h = add_channel(grid, snr_db, seed)
    g = estimate(y)
    g.update(h=h, grid=grid, y=y, true_snr=snr_db, seed=seed)
    return g


def dump_td_bin(path, snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    grid = build_grid()
    y, _h = add_channel(grid, snr_db, seed)
    td = td_from_y(y)
    with open(path, "wb") as fh:
        for blk in td:
            for c in blk:
                fh.write(struct.pack("<I", (int(c.real) * TD_SCALE) & 0xFFFFFFFF))
                fh.write(struct.pack("<I", (int(c.imag) * TD_SCALE) & 0xFFFFFFFF))
    return len(td) * F.SYM_LEN * 2 * 4


def cqi_thresholds():
    """Linear (Q8) SNR threshold for each CQI, matching nr_la.cqi_from_sinr."""
    out = [0] * 16
    for c in range(1, 16):
        req = LA.required_snr_db(LA.cqi_entry(c)) + LA.SPREAD_DB * 1.2816
        out[c] = int(round((10 ** (req / 10.0)) * 256))
    return out


def selftest():
    ok = True
    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    print("  CSI-RS: %d REs, density %d, c_init=0x%08x, seq[0]=%s"
          % (NCSI, DENSITY, csi_cinit(), csi_rs_seq()[0]))
    prev_cqi = -1
    for snr in (-5.0, 0.0, 5.0, 10.0, 15.0, 20.0, 25.0):
        g = golden(snr, 1)
        err = g["snr_db"] - snr
        print("  SNR %5.1f dB -> est %6.2f dB (err %+5.2f), CQI %2d"
              % (snr, g["snr_db"], err, g["cqi"]))
        chk(abs(err) < 2.0, "SNR estimate error %.2f dB at %.1f dB" % (err, snr))
        chk(g["cqi"] == LA.cqi_from_sinr(g["snr_db"]), "CQI mismatch")
        chk(g["cqi"] >= prev_cqi, "CQI not monotone at %.1f dB" % snr)
        prev_cqi = g["cqi"]

    thr = cqi_thresholds()
    chk(all(thr[i] < thr[i + 1] for i in range(1, 15)),
        "Q8 CQI thresholds not increasing")
    print("  Q8 thresholds: %s ..." % thr[1:6])

    # a dead front end must show up as a huge SNR error
    g = golden(20.0, 1)
    z = estimate([[0j] * NRE])
    chk(abs(z["snr_db"] - 20.0) > 5.0, "all-zero input should not estimate 20 dB")

    print("[ %s ] CSI-RS + CQI model (38.211 7.4.1.5 / 38.214 5.2.2.1)"
          % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="CSI-RS / CQI golden model")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    d = sub.add_parser("dump")
    d.add_argument("out")
    d.add_argument("--snr", type=float, default=DEFAULT_SNR)
    d.add_argument("--seed", type=int, default=DEFAULT_SEED)
    a = ap.parse_args()
    if a.cmd == "dump":
        n = dump_td_bin(a.out, a.snr, a.seed)
        print("wrote %s (%d bytes, snr=%.1f dB, seed=%d)" % (a.out, n, a.snr, a.seed))
        return 0
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
