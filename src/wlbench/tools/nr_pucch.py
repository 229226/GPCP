#!/usr/bin/env python3
"""PUCCH format 0 golden model + TD image for the CGRA (P3 of docs/11).

The downlink/uplink data chains are hardware-verified; what was still missing
on the control side is PUCCH.  tools/nr_ul.py already models PUCCH format 0 as
1-bit HARQ-ACK / SR carried by one of two cyclic shifts of a Zadoff-Chu root
(m_CS = 0 / 6), detected by comparing correlation energies -- but only in
floating point, purely in Python.

This module makes the same detection *hardware-exact*:

  * the received sequence and the two reference rotations are quantised to
    int32 pairs (CHIP_SCALE), exactly like tools/nr_rach.py does for Msg1;
  * the correlation is the 127-tap integer sum of the already-verified
    ``prach_corr`` reference kernel (reference kernels/prach_corr.yaml -> prach_corr_prog.h),
    reused here for only NCAND = 2 candidate shifts;
  * the correlation is reported as a total; an implementation may split the
    sum across its own lanes, but the reference value stays the total.

So ``run-pucch`` shares the PRACH kernel and adds the RRC-era control-plane
channel to the SoC without inventing new reference kernel.

Stimulus layout (byte offsets from TD_BASE, all little-endian int32):
    word 0 .. 277            : 139 received (re, im) pairs
    word 278 + 254*d ..      : candidate d's 127 reference taps (d = 0, 1)
                               candidate 0 uses m_CS = 0, candidate 1 m_CS = 6

Deliberate simplifications (same as nr_ul.py / the P2 PRACH model): L_RA = 139
short ZC, a single root u = 1, 127 of the 139 chips correlated, AWGN only, no
OCC / frequency hopping / PUCCH resource sets, 1 bit per occasion.
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

import nr_ul as UL
import nr_rach as RACH

L_RA = UL.L_RA
ROOT_U = UL.ROOT_U
CS_ACK = UL.CS_ACK
CS_NACK = UL.CS_NACK
CANDIDATES = (CS_ACK, CS_NACK)          # 0, 6
NCAND = len(CANDIDATES)
TAPS = RACH.PRACH_TAPS                   # 127 (the prach_corr reference kernel tap count)
CHIP_SCALE = RACH.CHIP_SCALE             # 2048
PAIR_WORDS = 2 * L_RA                    # 278
TAP_WORDS = 2 * TAPS                     # 254
DEFAULT_SNR = 6.0
DEFAULT_SEED = 1


# ---------------------------------------------------------------- sequences
def zc_chips(u=ROOT_U, n=L_RA):
    """Quantised ZC root: the same integer taps the reference kernel uses."""
    return [(int(round(c.real * CHIP_SCALE)), int(round(c.imag * CHIP_SCALE)))
            for c in UL.zc_root(u, n)]


def pucch_tx_int(ack, chips=None, snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    """1-bit SR/ACK on PUCCH format 0, quantised + AWGN (integer model)."""
    chips = zc_chips() if chips is None else chips
    shift = CS_ACK if ack else CS_NACK
    y = [chips[(i + shift) % L_RA] for i in range(L_RA)]
    rnd = random.Random(seed)
    sigma = CHIP_SCALE / math.sqrt(2 * 10 ** (snr_db / 10))
    return [(int(round(v[0] + rnd.gauss(0, sigma))),
             int(round(v[1] + rnd.gauss(0, sigma)))) for v in y]


def reference_taps(chips=None):
    """The NCAND rotated reference blocks, in the order CANDIDATES."""
    chips = zc_chips() if chips is None else chips
    return [[chips[(i + cs) % L_RA] for i in range(TAPS)] for cs in CANDIDATES]


def correlations(y, taps_blocks=None):
    """Exact integer arithmetic of the prach_corr reference kernel for each candidate."""
    taps_blocks = reference_taps() if taps_blocks is None else taps_blocks
    out = []
    for blk in taps_blocks:
        re = im = 0
        for i in range(TAPS):
            yr, yi = y[i]
            cr, ci = blk[i]
            re += yr * cr + yi * ci
            im += yi * cr - yr * ci
        out.append((re, im))
    return out


def detect(y, taps_blocks=None):
    """1 = ACK / SR present, 0 = NACK / SR absent; plus peak/second diagnostics."""
    corr = correlations(y, taps_blocks)
    pw = [re * re + im * im for re, im in corr]
    hi = 0 if pw[0] >= pw[1] else 1
    lo = 1 - hi
    return dict(bit=1 if hi == 0 else 0, cand=hi, cs=CANDIDATES[hi],
                corr=corr, power=pw, peak=pw[hi], second=pw[lo],
                margin=pw[hi] / max(1, pw[lo]),
                re=corr[hi][0], im=corr[hi][1])


# ---------------------------------------------------------------- TD image
def _pack_pairs(pairs):
    out = bytearray()
    for re, im in pairs:
        out += struct.pack("<I", re & 0xFFFFFFFF)
        out += struct.pack("<I", im & 0xFFFFFFFF)
    return out


def dump_td_bin(path, ack=1, snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    """[ received chips | NCAND rotated 127-tap references ] for run-pucch."""
    chips = zc_chips()
    y = pucch_tx_int(ack, chips, snr_db, seed)
    out = _pack_pairs(y)
    for blk in reference_taps(chips):
        out += _pack_pairs(blk)
    with open(path, "wb") as f:
        f.write(out)
    return len(out), y, chips


def golden(ack=1, snr_db=DEFAULT_SNR, seed=DEFAULT_SEED):
    """All anchors the checker needs."""
    chips = zc_chips()
    y = pucch_tx_int(ack, chips, snr_db, seed)
    d = detect(y, reference_taps(chips))
    # cross-check against the independent floating-point PUCCH model
    fy = [(re / CHIP_SCALE, im / CHIP_SCALE) for re, im in y]
    fc = [complex(re, im) for re, im in fy]
    float_bit = UL.pucch_detect(fc)
    return dict(ack=ack, y=y, chips=chips, ref=reference_taps(chips),
                detect=d, float_bit=float_bit,
                consistent=(float_bit == d["bit"]))


# ---------------------------------------------------------------- self test
def selftest():
    ok = True
    print("PUCCH format 0 golden model (L_RA=%d root u=%d, m_CS=%s, "
          "%d-tap integer correlation, CHIP_SCALE=%d)"
          % (L_RA, ROOT_U, CANDIDATES, TAPS, CHIP_SCALE))

    for snr in (0.0, 3.0, 6.0, 12.0):
        hits = consistent = 0
        for seed in range(1, 9):
            for ack in (0, 1):
                g = golden(ack, snr, seed)
                hits += 1 if g["detect"]["bit"] == ack else 0
                consistent += 1 if g["consistent"] else 0
        good = hits == 16 and consistent == 16
        ok &= good
        print("  SNR %4.1f dB : %d/16 bits correct, %d/16 agree with nr_ul.py %s"
              % (snr, hits, consistent, "[OK]" if good else "[FAIL]"))

    # correlation values are the exact integer sums of the reference kernel
    g = golden(1, DEFAULT_SNR, DEFAULT_SEED)
    y, refs = g["y"], g["ref"]
    manual = []
    for blk in refs:
        re = im = 0
        for i in range(TAPS):
            yr, yi = y[i]
            cr, ci = blk[i]
            re += yr * cr + yi * ci
            im += yi * cr - yr * ci
        manual.append((re, im))
    ok &= manual == g["detect"]["corr"]
    print("  integer correlations: c0=(%d,%d) c6=(%d,%d), margin %.1f dB"
          % (manual[0][0], manual[0][1], manual[1][0], manual[1][1],
             10 * math.log10(g["detect"]["margin"])))

    # TD image layout
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "pucch_selftest.td")
    n, _, _ = dump_td_bin(path, 1, DEFAULT_SNR, DEFAULT_SEED)
    want = (PAIR_WORDS + NCAND * TAP_WORDS) * 4
    ok &= n == want
    print("  TD image: %d bytes (expect %d) %s"
          % (n, want, "[OK]" if n == want else "[FAIL]"))
    os.remove(path)

    print("[ %s ] PUCCH format 0 integer golden model" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="PUCCH format 0 golden model / TD")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    d = sub.add_parser("dump")
    d.add_argument("out")
    d.add_argument("--ack", type=int, default=1)
    d.add_argument("--snr", type=float, default=DEFAULT_SNR)
    d.add_argument("--seed", type=int, default=DEFAULT_SEED)
    a = ap.parse_args()

    if a.cmd == "dump":
        n, _, _ = dump_td_bin(a.out, a.ack, a.snr, a.seed)
        print("wrote %s (%d bytes, ack=%d, snr=%.1f dB, seed=%d, candidates=%s)"
              % (a.out, n, a.ack, a.snr, a.seed, CANDIDATES))
        return 0
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
