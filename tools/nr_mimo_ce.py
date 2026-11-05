#!/usr/bin/env python3
"""2x2 MIMO channel estimation with FDM-orthogonal DMRS (T4b-1).

Two transmit layers share one OFDM symbol.  The 60 pilot REs are split by
layer (layer 0 takes the even pilot indices, layer 1 the odd ones), so each
layer's 30 pilots arrive at both receive antennas without interference.  The
receiver does one FFT per antenna and a per-layer LS average:

    h[rx][l] = sum_p Y_rx[p] * conj(d_l[p]) / (NP * |d|^2)

This is the contract the SoC implementation (sw/soc/mimo_ce_bm.c) must match;
the MMSE separation that consumes these four coefficients is the next step.
"""
import argparse
import cmath
import math
import os
import random
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import nr_csi as CSI
import pdsch_full_plan as F
import pdsch_plan as M

NFFT = 4096
CP_LEN = 288
NRE = 240
NPILOT = 60                  # 60 pilot REs (every 4th), 30 per layer
NP_LAYER = NPILOT // 2
NDATA = NRE - NPILOT         # 180 data REs
QPSK = 2000
H_SCALE = 16
TD_SCALE = 256
DEFAULT_SNR = 25.0
DEFAULT_SEED = 1

# Layer-specific Gold seeds (n_ID 1 / 2) so the two pilot sequences differ.
PILOT_CINIT = [0xC01, 0xC02]


def pilot_positions():
    """Every 4th RE is a pilot; even indices -> layer 0, odd -> layer 1."""
    return [r for r in range(NRE) if r % 4 == 0]


def data_positions():
    return [r for r in range(NRE) if r % 4 != 0]


def pilot_seq(layer):
    c = CSI.gold_bits(PILOT_CINIT[layer], 2 * NP_LAYER)
    return [complex((1 - 2 * c[2 * i]) * QPSK, (1 - 2 * c[2 * i + 1]) * QPSK)
            for i in range(NP_LAYER)]


def channel_matrix(seed=DEFAULT_SEED):
    rnd = random.Random(seed)
    base = [[complex(0.92, 0.10), complex(0.30, -0.40)],
            [complex(0.35, 0.55), complex(0.80, 0.15)]]
    return [[c * cmath.exp(1j * rnd.uniform(-0.1, 0.1)) for c in row]
            for row in base]


def layer_data(seed=DEFAULT_SEED):
    """180 QPSK symbols per layer, taken from the real PDSCH stream."""
    import nr_mimo_link as L
    syms, _k0, _qm = L.data_symbols()
    import nr_mimo as MM
    return MM.layer_map(syms, 2)


def build_grids(h, seed=DEFAULT_SEED):
    """Two frequency-domain grids (one per Rx antenna)."""
    lay = layer_data(seed)
    p0, p1 = pilot_seq(0), pilot_seq(1)
    pp = pilot_positions()
    dp = data_positions()
    grids = [[0j] * NRE for _ in range(2)]
    for rx in range(2):
        for i, r in enumerate(pp):
            l = i % 2
            d = p0[i // 2] if l == 0 else p1[i // 2]
            grids[rx][r] = h[rx][0] * (d if l == 0 else 0j) \
                         + h[rx][1] * (d if l == 1 else 0j)
        for i, r in enumerate(dp):
            grids[rx][r] = h[rx][0] * lay[0][i] + h[rx][1] * lay[1][i]
    return grids, lay


def add_noise(grids, snr_db, seed):
    """AWGN on the occupied REs; SNR is per occupied RE and includes |h|^2."""
    rnd = random.Random(seed + 31)
    occ = pilot_positions() + data_positions()
    p_sig = sum(abs(grids[rx][r]) ** 2 for rx in range(2) for r in occ)
    p_sig /= (2 * len(occ))
    sigma = math.sqrt(p_sig / (10 ** (snr_db / 10.0)) / 2.0)
    out = []
    for rx in range(2):
        row = list(grids[rx])
        for r in occ:
            row[r] += complex(rnd.gauss(0, sigma), rnd.gauss(0, sigma))
        out.append(row)
    return out


def td_from_grids(grids):
    """One OFDM symbol per antenna -> list of two TD blocks."""
    tds = []
    for rx in range(2):
        freq = [0j] * NFFT
        for r in range(NRE):
            freq[CSI.BIN0 + r] = grids[rx][r]
        sig = F._fft(freq, inverse=True)
        samples = [complex(int(round(c.real)), int(round(c.imag))) for c in sig]
        tds.append(F.NR.add_cp(samples, CP_LEN))
    return tds


def estimate(grids):
    """Per-layer per-antenna LS from the 30 pilots of that layer."""
    pp = pilot_positions()
    p0, p1 = pilot_seq(0), pilot_seq(1)
    den = NP_LAYER * 2 * QPSK * QPSK / H_SCALE
    h = [[0j, 0j], [0j, 0j]]
    for rx in range(2):
        acc = [[0j, 0j], [0j, 0j]]
        for i, r in enumerate(pp):
            l = i % 2
            d = p0[i // 2] if l == 0 else p1[i // 2]
            acc[rx][l] += grids[rx][r] * complex(d.real, -d.imag)
        for l in range(2):
            h[rx][l] = complex(int(acc[rx][l].real // den),
                               int(acc[rx][l].imag // den))
    return h


def golden(snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    h = channel_matrix(seed)
    grids, lay = build_grids(h, seed)
    y = add_noise(grids, snr_db, seed)
    est = estimate(y)
    return dict(h=h, est=est, grids=grids, y=y, lay=lay, snr=snr_db, seed=seed)


def dump_td_bin(path, snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    h = channel_matrix(seed)
    grids, _lay = build_grids(h, seed)
    y = add_noise(grids, snr_db, seed)
    tds = td_from_grids(y)
    n = 0
    with open(path, "wb") as fh:
        for blk in tds:
            for c in blk:
                fh.write(struct.pack("<I", (int(c.real) * TD_SCALE) & 0xFFFFFFFF))
                fh.write(struct.pack("<I", (int(c.imag) * TD_SCALE) & 0xFFFFFFFF))
                n += 8
    return n


def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    print("  2 layers x 2 Rx, %d pilots (%d/layer), %d data REs"
          % (NPILOT, NP_LAYER, NDATA))
    h = channel_matrix(1)
    print("  true h: %s" % [[complex(round(c.real, 3), round(c.imag, 3))
                             for c in row] for row in h])
    for snr in (25.0, 15.0, 5.0):
        g = golden(snr, 1)
        err = 0.0
        for rx in range(2):
            for l in range(2):
                t = h[rx][l] * H_SCALE       # the LS output is H_SCALE-scaled
                e = g["est"][rx][l]
                err = max(err, abs(e - t) / max(abs(t), 1e-9))
        print("  SNR %4.1f dB : est %s, max rel err %.1f%%"
              % (snr, [[complex(round(c.real, 2), round(c.imag, 2))
                        for c in row] for row in g["est"]], err * 100))
        # The LS output is integer with 1 LSB, i.e. a 1/(|h|*H_SCALE) ~ 7%
        # quantisation floor; 25% still catches a wrong layer split or a
        # missing antenna by an order of magnitude.
        chk(err < 0.25, "channel estimate off by %.1f%% at %.0f dB" % (err * 100, snr))

    # a dead second antenna must show up
    g = golden(25.0, 1)
    g["y"][1] = [0j] * NRE
    e2 = estimate(g["y"])
    chk(abs(e2[1][0]) < 1 and abs(e2[1][1]) < 1,
        "zeroed antenna 1 should estimate ~0")

    print("[ %s ] 2x2 FDM-DMRS channel estimation"
          % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="2x2 MIMO channel estimation")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    d = sub.add_parser("dump")
    d.add_argument("out")
    d.add_argument("--snr", type=float, default=DEFAULT_SNR)
    d.add_argument("--seed", type=int, default=DEFAULT_SEED)
    a = ap.parse_args()
    if a.cmd == "dump":
        n = dump_td_bin(a.out, a.snr, a.seed)
        print("wrote %s (%d bytes, snr=%.1f, seed=%d)" % (a.out, n, a.snr, a.seed))
        return 0
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
