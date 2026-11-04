#!/usr/bin/env python3
"""NR uplink entry points: PRACH preamble detection + PUCCH format 0 (38.211).

Model-level only (no CGRA / SoC benchmark yet).  It establishes the two uplink
physical channels that the downlink-only GPCP chain is missing:

  PRACH  : Zadoff-Chu root sequence, cyclic-shift preamble, timing-advance
           estimation by circular correlation at the receiver
  PUCCH  : format 0, 1-bit HARQ-ACK carried by one of two cyclic shifts
           (m_CS = 0 / 6), detected by comparing correlation energies

Scope: AWGN-only, single root sequence, no PRACH format / restricted set /
N_CS configuration, no PUSCH, no power control.  L_RA = 139 (short preamble)
instead of the full 839 so the pure-Python correlation stays fast.
"""
import cmath
import math
import random

L_RA = 139
ROOT_U = 1
CS_ACK = 0
CS_NACK = 6


def zc_root(u=ROOT_U, n=L_RA):
    """38.211 6.3.3.1: x_u(i) = exp(-j*pi*u*i*(i+1)/L_RA)."""
    return [cmath.exp(-1j * math.pi * u * i * (i + 1) / n) for i in range(n)]


def cyclic_shift(seq, s):
    n = len(seq)
    return [seq[(i + s) % n] for i in range(n)]


def corr(a, b):
    return sum(x * y.conjugate() for x, y in zip(a, b))


def awgn(sig, snr_db, seed):
    rnd = random.Random(seed)
    sigma = 1.0 / math.sqrt(2 * 10 ** (snr_db / 10))
    return [v + complex(rnd.gauss(0, sigma), rnd.gauss(0, sigma)) for v in sig]


# ------------------------------------------------------------------ PRACH
def prach_tx(root=None, cs=0):
    return cyclic_shift(root if root is not None else zc_root(), cs)


def prach_detect(y, root=None):
    """Circular correlation over all delays; returns (estimated TA, peak)."""
    r = root if root is not None else zc_root()
    best, peak = -1.0, 0
    for d in range(L_RA):
        m = abs(corr(y, cyclic_shift(r, d)))
        if m > best:
            best, peak = m, d
    return peak, best


# ------------------------------------------------------------- PUCCH f0
def pucch_tx(ack, root=None):
    r = root if root is not None else zc_root()
    return cyclic_shift(r, CS_ACK if ack else CS_NACK)


def pucch_detect(y, root=None):
    r = root if root is not None else zc_root()
    m0 = abs(corr(y, cyclic_shift(r, CS_ACK)))
    m6 = abs(corr(y, cyclic_shift(r, CS_NACK)))
    return 1 if m0 > m6 else 0


def main():
    root = zc_root()
    print("NR uplink: L_RA=%d, root u=%d, PUCCH m_CS=%d/%d" % (
        L_RA, ROOT_U, CS_ACK, CS_NACK))

    ok = True
    for ta in (0, 5, 12, 23):
        preamble = prach_tx(root, cs=0)
        y = awgn(cyclic_shift(preamble, ta), 6.0, seed=ta + 1)
        est, peak = prach_detect(y, root)
        good = est == ta and peak > 0.6 * L_RA
        ok &= good
        print("  PRACH TA=%2d : detected %2d, peak %.1f %s" % (
            ta, est, peak, "OK" if good else "BAD"))

    for snr in (0.0, 3.0, 6.0):
        hits = 0
        for seed in range(1, 9):
            ack = seed % 2
            y = awgn(pucch_tx(ack, root), snr, seed + 40)
            hits += 1 if pucch_detect(y, root) == ack else 0
        print("  PUCCH SNR %4.1f dB : %d/8 ACK/NACK correct" % (snr, hits))
        if snr >= 3.0:
            ok &= hits == 8

    print("[ %s ] PRACH timing + PUCCH format 0" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
