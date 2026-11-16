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
"""2x2 MIMO layer mapping / precoding / MMSE equalisation (38.211 7.3.1).

A self-contained model that takes the existing SISO PDSCH symbol stream into
two spatial layers and recovers it with a linear MMSE receiver:

    bits -> QPSK -> layer map (2 layers) -> precoder W -> 2x2 channel H + AWGN
         -> MMSE equaliser -> layer demap -> QPSK LLR -> hard bits

Scope: single-user 2 layers / 2 Rx antennas, no codebook feedback, no CDD,
H assumed perfectly known at the receiver.  The MMSE inverse is written out
analytically for the 2x2 case so the model has no numpy dependency.
"""
import math
import random

QPSK = 2000
LAYERS = 2


def qpsk_map(bits):
    return [complex((1 - 2 * bits[2 * i]) * QPSK,
                    (1 - 2 * bits[2 * i + 1]) * QPSK)
            for i in range(len(bits) // 2)]


def qpsk_hard(sym):
    return [0 if sym.real >= 0 else 1, 0 if sym.imag >= 0 else 1]


def layer_map(syms, layers=LAYERS):
    """Round-robin mapping of one codeword onto `layers` spatial layers."""
    n = len(syms) // layers
    return [[syms[l + i * layers] for i in range(n)] for l in range(layers)]


def layer_demapper(layers_syms, layers=LAYERS):
    n = len(layers_syms[0])
    return [layers_syms[i % layers][i // layers] for i in range(n * layers)]


def precoder():
    """Identity precoder (rank-2, transparent); swap in a DFT/codebook W here."""
    return ((1 + 0j, 0j), (0j, 1 + 0j))


def hermitian(h):
    return [[h[j][i].conjugate() for j in range(2)] for i in range(2)]


def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(2)) for j in range(2)]
            for i in range(2)]


def matvec(a, v):
    return [sum(a[i][k] * v[k] for k in range(2)) for i in range(2)]


def inv2(a):
    det = a[0][0] * a[1][1] - a[0][1] * a[1][0]
    assert abs(det) > 1e-12
    return ((a[1][1] / det, -a[0][1] / det),
            (-a[1][0] / det, a[0][0] / det))


def mmse_weights(h, sigma2):
    """G = (H^H H + sigma2 I)^-1 H^H."""
    hh = hermitian(h)
    a = matmul(hh, h)
    a[0][0] += sigma2
    a[1][1] += sigma2
    return matmul(inv2(a), hh)


def channel(snr_db, seed=7, h=None):
    """Fixed 2x2 channel + complex AWGN on both Rx antennas."""
    if h is None:
        h = ((complex(0.92, 0.10), complex(0.18, -0.09)),
             (complex(-0.14, 0.06), complex(0.88, 0.16)))
    rnd = random.Random(seed)
    p_sig = 2 * QPSK * QPSK                 # ~ |H*x|^2 per antenna
    sigma = math.sqrt(p_sig / (2 * 10 ** (snr_db / 10)))
    return h, sigma


def transmit(bits):
    syms = qpsk_map(bits)
    layers = layer_map(syms)
    w = precoder()
    tx = [[sum(w[a][l] * layers[l][i] for l in range(2))
           for i in range(len(layers[0]))] for a in range(2)]
    return syms, layers, tx


def receive(tx, h, sigma, seed=11):
    rnd = random.Random(seed)
    y = []
    for i in range(len(tx[0])):
        y.append([sum(h[r][a] * tx[a][i] for a in range(2))
                  + complex(rnd.gauss(0, sigma), rnd.gauss(0, sigma))
                  for r in range(2)])
    g = mmse_weights(h, sigma * sigma)
    eq = [matvec(g, v) for v in y]           # eq[i] = [layer0, layer1] at time i
    layers_rx = [[eq[i][l] for i in range(len(eq))] for l in range(LAYERS)]
    return layer_demapper(layers_rx)


def main():
    rnd = random.Random(3)
    bits = [rnd.randint(0, 1) for _ in range(720)]
    syms, _, tx = transmit(bits)
    print("2x2 MIMO: %d bits -> %d QPSK symbols -> %d layers x %d symbols" % (
        len(bits), len(syms), LAYERS, len(tx[0])))
    for snr in (6.0, 10.0, 14.0, 20.0):
        h, sigma = channel(snr)
        eq = receive(tx, h, sigma)
        got = []
        for v in eq:
            got += qpsk_hard(v)
        errs = sum(a != b for a, b in zip(got, bits))
        print("  SNR %5.1f dB : %3d / %d bit errors (BER %.4f)" % (
            snr, errs, len(bits), errs / len(bits)))
    h, sigma = channel(20.0)
    eq = receive(tx, h, sigma)
    got = [b for v in eq for b in qpsk_hard(v)]
    ok = got == bits
    print("[ %s ] 2x2 MMSE layer recovery" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
