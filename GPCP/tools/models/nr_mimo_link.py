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
"""2x2 MIMO PDSCH link: layer mapping -> precoding -> 2x2 channel -> MMSE
-> layer demap -> QPSK LLR -> descramble -> rate de-match -> LDPC -> CRC24A.

This connects the standalone nr_mimo.py algebra to the *real* coding chain in
nr_pdsch_amc.py, so the MIMO gain can be measured as a BLER curve on the same
transport block the SISO gates use (80-bit TB, BG2 Zc=20, E=720).

Scope matches nr_mimo.py: single user, 2 layers, 2 Rx, perfect H at the
receiver, no codebook / CDD.  Precoding is the identity (W = I), which is the
"no PMI" baseline; the point of the gate is the MMSE receiver, not precoder
selection.
"""
import argparse
import cmath
import math
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import nr_mimo as MIMO
import nr_pdsch_amc as A
import pdsch_full_plan as F
import pdsch_plan as M

LAYERS = 2
MCS = 9                      # Qm=2, E=720 -> 360 QPSK symbols
N_RNTI = M.N_RNTI
DEFAULT_SEED = 1


def data_symbols(mcs=MCS, rv=0):
    """The 360 QPSK symbols the SISO chain would put on the data REs."""
    grid, k0, qm, n_sym = A.build_grid(mcs, rv)
    assert qm == 2, "MIMO link uses the QPSK baseline"
    return [grid[s][r] for (s, r) in A.data_flat()[:n_sym]], k0, qm


def channel_matrix(seed=DEFAULT_SEED):
    """Fixed, well-conditioned 2x2 channel with a mild phase spread."""
    rnd = random.Random(seed)
    h = [[complex(1.0, 0.0), complex(0.25, -0.40)],
         [complex(0.35, 0.55), complex(0.80, 0.15)]]
    out = []
    for row in h:
        out.append([c * cmath.exp(1j * rnd.uniform(-0.15, 0.15)) for c in row])
    return out


def precode(layers):
    """Identity precoder: x = W s, W = I."""
    return [[layers[0][i], layers[1][i]] for i in range(len(layers[0]))]


def apply_channel(x, h, snr_db, seed):
    """y = H x + n, with the noise referenced to the per-layer symbol power."""
    rnd = random.Random(seed + 977)
    n0 = sum(abs(v) ** 2 for i, (c0, c1) in enumerate(x) for v in (c0, c1))
    n0 /= (len(x) * LAYERS)
    sigma2 = n0 / (10 ** (snr_db / 10.0))
    sigma = math.sqrt(sigma2 / 2.0)
    y = []
    for r in range(2):
        yr = []
        for i, (c0, c1) in enumerate(x):
            v = h[r][0] * c0 + h[r][1] * c1
            v += complex(rnd.gauss(0, sigma), rnd.gauss(0, sigma))
            yr.append(v)
        y.append(yr)
    return y, sigma2, n0


def mmse(y, h, sigma2, ps):
    """W = (H^H H + (sigma2/ps) I)^-1 H^H applied per symbol index.

    sigma2 must be expressed in the same units as the signal power that H
    acts on: QPSK symbols carry 8e6 here while |h|^2 is ~1, so feeding the raw
    sigma2 would shrink the equaliser even at 60 dB (this was a real bug:
    94 of 180 layer-1 symbols were wrong with no noise at all).
    """
    w = MIMO.mmse_weights(h, sigma2 / ps)
    out = [[], []]
    for i in range(len(y[0])):
        v = [y[0][i], y[1][i]]
        for l in range(2):
            out[l].append(w[l][0] * v[0] + w[l][1] * v[1])
    return out


def decode(layer_syms, rv=0):
    syms = MIMO.layer_demapper(layer_syms, LAYERS)
    raw = []
    for s in syms:
        raw += [int(s.real), int(s.imag)]
    llr_e = A.deinterleave(raw, 2)
    scr = M.scrambling_seq(N_RNTI)
    descr = [-v if scr[k] else v for k, v in enumerate(llr_e)]
    dem, k0 = A._rate_dematch_rv(descr, rv)
    hard, scaled = F.hw_decode(dem)
    tb = hard[:M.A_TB]
    crc = hard[M.A_TB:M.A_TB + M.CRC_LEN]
    want = M.payload_bits()
    return dict(tb=tb, want=want, crc_ok=(M.crc24a(tb) == crc),
                tb_err=sum(a != b for a, b in zip(tb, want)), k0=k0,
                raw=raw, descr=descr, dem=dem)


def link(snr_db, seed=DEFAULT_SEED, h=None, h_est=None):
    """h is the true channel, h_est is what the receiver assumes."""
    syms, _k0, _qm = data_symbols()
    layers = MIMO.layer_map(syms, LAYERS)
    x = precode(layers)
    h = channel_matrix(seed) if h is None else h
    h_est = h if h_est is None else h_est
    y, sigma2, ps = apply_channel(x, h, snr_db, seed)
    est = mmse(y, h_est, sigma2, ps)
    r = decode(est)
    r.update(snr=snr_db, seed=seed, h=h, sigma2=sigma2)
    return r


def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    print("  2x2 MIMO, MCS %d (Qm=2), %d symbols -> 2 layers of %d"
          % (MCS, 360, 180))
    prev_err = 10 ** 9
    for snr in (0.0, 5.0, 10.0, 15.0, 20.0):
        r = link(snr, 1)
        print("  SNR %4.1f dB : crc=%s tb_err=%2d" %
              (snr, r["crc_ok"], r["tb_err"]))
        chk(r["tb_err"] <= prev_err + 8, "TB errors not decreasing at %.0f dB" % snr)
        prev_err = min(prev_err, r["tb_err"])

    r = link(20.0, 1)
    chk(r["crc_ok"] and r["tb_err"] == 0, "20 dB must be error-free")

    # injection 1: receiver flips layer 1's phase -> that layer is all wrong.
    # (A mere identity estimate is NOT a gate: h00 dominates and LDPC still
    # corrects it, so it would pass and give false confidence.)
    bad = link(20.0, 1, h_est=[[1, 0], [0, -1]])
    chk(bad["tb_err"] > 0, "layer-1 phase flip should corrupt the TB")

    # injection 2: both layers carry the same data (rank-1) -> MMSE cannot
    # separate them, so the second layer is pure interference
    syms, _k0, _qm = data_symbols()
    lay = MIMO.layer_map(syms, LAYERS)          # 2 x 180
    x1 = [[lay[0][i], lay[0][i]] for i in range(len(lay[0]))]
    h = channel_matrix(1)
    y, sigma2, ps = apply_channel(x1, h, 20.0, 1)
    est = mmse(y, h, sigma2, ps)
    r1 = decode(est)
    chk(r1["tb_err"] > 0, "rank-1 injection should corrupt the TB")

    print("[ %s ] 2x2 MMSE + LDPC link" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="2x2 MIMO PDSCH link model")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "selftest" or a.cmd is None:
        return selftest()
    return 2


if __name__ == "__main__":
    sys.exit(main())
