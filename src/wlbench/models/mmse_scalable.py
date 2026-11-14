"""Parameterised N-layer x R-receiver MMSE reference workload.

Pure Python, deterministic, no third-party dependency.  The channel matrix,
transmit symbols, received samples and hard decisions all scale with
layers / rx / seed / snr_db, so a sweep produces genuinely different stimulus
and golden data - not just a different IR.

This model is intentionally independent of the frozen 2x2 nr_mimo model.
"""
import math
import struct

from wlbench import params

NRE = 180                      # data resource elements, same scale as nr_mimo
MAGIC = b"WBMS"
_CACHE = {}


def _cfg():
    p = params.current()
    return (int(p.get("layers", 2)), int(p.get("rx", 2)),
            int(p.get("seed", 1)), float(p.get("snr_db", 25.0)))


def _lcg(seed):
    x = (seed * 2654435761 + 1013904223) & 0xFFFFFFFF
    while True:
        x = (x * 1664525 + 1013904223) & 0xFFFFFFFF
        yield x


def _inv(a):
    """Complex Gauss-Jordan inverse (small square matrices)."""
    n = len(a)
    m = [list(row) + [complex(i == j) for j in range(n)]
         for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            raise ValueError("singular MMSE matrix")
        m[col], m[piv] = m[piv], m[col]
        pv = m[col][col]
        m[col] = [v / pv for v in m[col]]
        for r in range(n):
            if r == col:
                continue
            f = m[r][col]
            if f:
                m[r] = [x - f * y for x, y in zip(m[r], m[col])]
    return [row[n:] for row in m]


def _build(layers, rx, seed, snr_db):
    key = (layers, rx, seed, snr_db)
    if key in _CACHE:
        return _CACHE[key]

    g = _lcg(seed * 7919 + 13)
    H = [[complex((next(g) % 2000 - 1000) / 1000.0,
                  (next(g) % 2000 - 1000) / 1000.0)
          for _ in range(layers)] for _ in range(rx)]

    sigma2 = 10 ** (-snr_db / 10.0) / 2.0
    sigma = math.sqrt(sigma2)
    G = [[sum(H[r][i].conjugate() * H[r][j] for r in range(rx))
          + (sigma2 if i == j else 0)
          for j in range(layers)] for i in range(layers)]
    gi = _inv(G)
    W = [[sum(gi[i][k] * H[r][k].conjugate() for k in range(layers))
          for r in range(rx)] for i in range(layers)]

    g2 = _lcg(seed * 104729 + 7)
    ys, tx_bits, hat_bits = [], [], []
    for _ in range(NRE):
        x, b = [], []
        for _l in range(layers):
            b1, b2 = next(g2) & 1, next(g2) & 1
            b.extend((b1, b2))
            x.append(complex(1 - 2 * b1, 1 - 2 * b2) / math.sqrt(2))
        y = []
        for r in range(rx):
            v = sum(H[r][l] * x[l] for l in range(layers))
            nr = ((next(g2) / 2 ** 32) * 2 - 1) * sigma
            ni = ((next(g2) / 2 ** 32) * 2 - 1) * sigma
            y.append(v + complex(nr, ni))
        xh = [sum(W[i][r] * y[r] for r in range(rx))
              for i in range(layers)]
        hb = []
        for i in range(layers):
            hb.extend((1 if xh[i].real < 0 else 0,
                       1 if xh[i].imag < 0 else 0))
        ys.append(y)
        tx_bits.append(b)
        hat_bits.append(hb)

    out = {"H": H, "W": W, "ys": ys, "tx_bits": tx_bits,
           "hat_bits": hat_bits, "sigma2": sigma2}
    _CACHE[key] = out
    return out


def stimulus_bytes():
    layers, rx, seed, snr_db = _cfg()
    d = _build(layers, rx, seed, snr_db)
    buf = bytearray()
    buf += struct.pack("<4sIII", MAGIC, layers, rx, NRE)
    for row in d["H"]:
        for c in row:
            buf += struct.pack("<hh", int(c.real * 1000), int(c.imag * 1000))
    for y in d["ys"]:
        for c in y:
            buf += struct.pack("<hh", int(c.real * 1000), int(c.imag * 1000))
    return bytes(buf)


def golden():
    layers, rx, seed, snr_db = _cfg()
    d = _build(layers, rx, seed, snr_db)
    packed = []
    for bits in d["hat_bits"]:
        w = 0
        for k, b in enumerate(bits):
            w |= b << k
        packed.append(w)
    return {
        "scenario": "mmse_scalable",
        "anchors": {
            "layers": layers,
            "rx": rx,
            "n_re": NRE,
            "nbits": NRE * layers * 2,
            "sigma2_milli": int(d["sigma2"] * 1000),
            "correct_symbols": sum(
                1 for a, b in zip(d["tx_bits"], d["hat_bits"]) if a == b),
        },
        "bits": {"packed": packed},
    }
