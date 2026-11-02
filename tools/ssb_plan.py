#!/usr/bin/env python3
"""SS/PBCH block (SSB) chain golden model.

Structure follows 38.211/38.212, parameters simplified and documented:

  * 240 RE x 4 OFDM symbols in the frequency domain (the 4096-point FFT is
    verified separately by `make -C sim/soc run-fft`)
  * PSS in symbol 1, SSS in symbol 3 (middle 127 subcarriers), PBCH DMRS on
    every 4th RE of every symbol (60 pilots), PBCH data on the other REs
  * MIB payload = 24 bits (+8 zero pad) = K = 32 information bits, polar code
    N = 512, frozen set {i : bitrev9(i) < N-K}; no rate matching / interleaving
    (the real PBCH transmits E = 864 bits, see docs/07 4.13)
  * PBCH-style Gold scrambling (c_init = 3*N_ID1 + N_ID2), QPSK

Receiver: PSS/SSS correlation on the CGRA, DMRS-based channel estimate per
symbol, equalisation by conj(H_hat) (scale free), soft value
Re(y * conj(H_hat)) -> polar SC decode -> MIB bits.
"""
import nr_seq

NSYM = 4
NRE = 240
PSS_RE0 = 56
NPSS = 127
DMRS_STEP = 4
NPILOT = NRE // DMRS_STEP        # 60
KINFO = 32
NPOL = 512
DEPTH = 9
QPSK = 2000                      # QPSK amplitude (keeps the int32 MAC safe)
CH_RE = 2                        # flat channel gain used by the transmitter
CH_IM = 1
LLR_SHIFT = 2                    # Re(y*conj(H)) scaling
H_SCALE = 16                     # channel estimate keeps a fractional part
NOISE = 512

TRUE_NID1 = 200
TRUE_NID2 = 1


# --------------------------------------------------------------- Gold sequence
def gold(c_init, length, nc=1600):
    """38.211 5.2.1 pseudo-random sequence c(n) = x1(n+Nc) ^ x2(n+Nc)."""
    n = length + nc
    x1 = [0] * (n + 31)
    x2 = [0] * (n + 31)
    x1[0] = 1
    for i in range(31):
        x2[i] = (c_init >> i) & 1
    for i in range(n):
        x1[i + 31] = (x1[i + 3] + x1[i]) & 1
        x2[i + 31] = (x2[i + 3] + x2[i + 2] + x2[i + 1] + x2[i]) & 1
    return [x1[nc + i] ^ x2[nc + i] for i in range(length)]


# ------------------------------------------------------------------- polar code
def bitrev(i, bits):
    r = 0
    for b in range(bits):
        r |= ((i >> b) & 1) << (bits - 1 - b)
    return r


def is_frozen(i):
    return bitrev(i, DEPTH) < (NPOL - KINFO)


def info_indices():
    return [i for i in range(NPOL) if not is_frozen(i)][:KINFO]


def polar_encode(ufull):
    x = list(ufull[:NPOL])
    step = 1
    while step < NPOL:
        for i in range(0, NPOL, 2 * step):
            for k in range(step):
                x[i + k] ^= x[i + k + step]
        step *= 2
    return x


def wrap32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def f_node(a, b):
    m = min(abs(a), abs(b))
    return wrap32(-m if ((a < 0) != (b < 0)) else m)


def g_node(a, b, beta):
    return wrap32(b + (1 - 2 * (beta & 1)) * a)


def sc_decode(llr):
    lbuf = [[0] * NPOL for _ in range(DEPTH + 1)]
    bbuf = [[0] * NPOL for _ in range(DEPTH + 1)]
    lbuf[0] = list(llr)

    def rec(d, o, size):
        if size == 1:
            bbuf[d][o] = 0 if is_frozen(o) else (1 if lbuf[d][o] < 0 else 0)
            return
        half = size // 2
        for i in range(half):
            lbuf[d + 1][o + i] = f_node(lbuf[d][o + i], lbuf[d][o + half + i])
        rec(d + 1, o, half)
        for i in range(half):
            lbuf[d + 1][o + half + i] = g_node(lbuf[d][o + i],
                                               lbuf[d][o + half + i],
                                               bbuf[d + 1][o + i])
        rec(d + 1, o + half, half)
        for i in range(half):
            bbuf[d][o + i] = bbuf[d + 1][o + i] ^ bbuf[d + 1][o + half + i]
            bbuf[d][o + half + i] = bbuf[d + 1][o + half + i]

    rec(0, 0, NPOL)
    return bbuf[DEPTH]


# ---------------------------------------------------------------------- LCG
class Lcg:
    def __init__(self, seed=0x50B0A5):
        self.s = seed

    def step(self):
        self.s = (self.s * 1103515245 + 12345) & 0xFFFFFFFF
        return self.s

    def noise(self):
        return ((self.step() >> 16) & 0x3FF) - 512


# ------------------------------------------------------------------ SSB layout
def mid_res():
    return list(range(PSS_RE0, PSS_RE0 + NPSS))


def pilot_res():
    return list(range(0, NRE, DMRS_STEP))


def data_res():
    """PBCH data REs per symbol in the scan order used by the program."""
    out = []
    for s in range(NSYM):
        skip = set(pilot_res())
        if s == 1 or s == 3:
            skip |= set(mid_res())
        out.append([r for r in range(NRE) if r not in skip])
    return out


def dmrs_values(c_init):
    c = gold(c_init, 2 * NSYM * NPILOT)
    vals = []
    for n in range(NSYM * NPILOT):
        vals.append(complex(1 - 2 * c[2 * n], 1 - 2 * c[2 * n + 1]) * QPSK)
    return vals


def payload_bits(seed=0x1234):
    g = Lcg(seed)
    return [(g.step() >> 31) & 1 for _ in range(24)] + [0] * 8


def nid_hash(nid1, nid2):
    """N_ID_cell = 3*N_ID1 + N_ID2 (used as the PBCH *data* scrambling init)."""
    return 3 * nid1 + nid2



def build_grid(nid1=TRUE_NID1, nid2=TRUE_NID2, payload=None):
    """Transmitted grid: returns (grid, payload, code_bits)."""
    if payload is None:
        payload = payload_bits()
    ufull = [0] * NPOL
    for k, i in enumerate(info_indices()):
        ufull[i] = payload[k]
    x = polar_encode(ufull)
    scr = gold(nid_hash(nid1, nid2), NPOL)
    bits = [x[i] ^ scr[i] for i in range(NPOL)]

    grid = [[0j] * NRE for _ in range(NSYM)]
    pss = nr_seq.pss_seq(nid2)
    sss = nr_seq.sss_seq(nid1, nid2)
    dr = dmrs_values(nid_hash(nid1, nid2))

    for s in range(NSYM):
        if s == 1:
            for k, r in enumerate(mid_res()):
                grid[s][r] = complex(pss[k], 0) * QPSK
        if s == 3:
            for k, r in enumerate(mid_res()):
                grid[s][r] = complex(sss[k], 0) * QPSK
        for p, r in enumerate(pilot_res()):
            grid[s][r] = dr[s * NPILOT + p]

    n = 0
    for s in range(NSYM):
        for r in data_res()[s]:
            if n + 1 >= NPOL:
                break
            grid[s][r] = complex(1 - 2 * bits[n], 1 - 2 * bits[n + 1]) * QPSK
            n += 2
    return grid, payload, bits


def transmit(grid, lcg):
    """Apply the flat channel + noise; returns the int32 y grid."""
    y = [[0j] * NRE for _ in range(NSYM)]
    h = complex(CH_RE, CH_IM)
    for s in range(NSYM):
        for r in range(NRE):
            v = grid[s][r] * h if grid[s][r] != 0 else 0j
            y[s][r] = complex(v.real + lcg.noise(), v.imag + lcg.noise())
    return y


def pss_corr(y, nid2):
    """127-point correlation over the PSS REs (real reference, so the
    imaginary part is carried by the channel)."""
    ref = nr_seq.pss_seq(nid2)
    acc = 0j
    for k, r in enumerate(mid_res()):
        acc += y[1][r] * ref[k]
    return acc


def sss_corr(y, nid1, nid2):
    ref = nr_seq.sss_seq(nid1, nid2)
    acc = 0j
    for k, r in enumerate(mid_res()):
        acc += y[3][r] * ref[k]
    return acc


def receiver(y, nid1, nid2):
    """DMRS channel estimate per symbol, equalise, descramble, soft LLRs."""
    dr = dmrs_values(nid_hash(nid1, nid2))
    h = []
    for s in range(NSYM):
        acc = 0j
        for p, r in enumerate(pilot_res()):
            d = dr[s * NPILOT + p]
            acc += y[s][r] * complex(d.real, -d.imag)
        den = NPILOT * 2 * QPSK * QPSK // H_SCALE
        h.append(complex(int(acc.real // den), int(acc.imag // den)))

    scr = gold(nid_hash(nid1, nid2), NPOL)
    llr = [0] * NPOL
    n = 0
    for s in range(NSYM):
        for r in data_res()[s]:
            if n + 1 >= NPOL:
                break
            eq = y[s][r] * complex(h[s].real, -h[s].imag)
            for soft in (eq.real, eq.imag):
                v = int(soft) >> LLR_SHIFT
                v = max(-2000000, min(2000000, v))
                llr[n] = -v if scr[n] else v
                n += 1
    return h, llr


# ------------------------------------------------------- time-domain samples
NFFT = 4096


def _fft(x, inverse=False):
    """Iterative radix-2 complex FFT (pure Python, N a power of two)."""
    n = len(x)
    x = list(x)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            x[i], x[j] = x[j], x[i]
    length = 2
    sgn = 1.0 if inverse else -1.0
    while length <= n:
        ang = sgn * 2.0 * 3.141592653589793 / length
        wl = complex(3.141592653589793 * 0 + __import__("math").cos(ang),
                     __import__("math").sin(ang))
        half = length // 2
        for i in range(0, n, length):
            w = 1 + 0j
            for k in range(half):
                u = x[i + k]
                v = x[i + k + half] * w
                x[i + k] = u + v
                x[i + k + half] = u - v
                w *= wl
        length <<= 1
    if inverse:
        x = [c / n for c in x]
    return x


# 38.211 places the 240 SSB resource elements at an offset inside the carrier;
# the demo used k = r (REs sitting on DC), which is not physical.  Keep the SSB
# clear of DC and of the FFT edges: RE r -> FFT bin SSB_K0 + r.
SSB_K0 = 480


def subcarriers():
    """SSB RE index -> FFT subcarrier (k = SSB_K0 + r)."""
    return {r: SSB_K0 + r for r in range(NRE)}


TD_SCALE = 256                   # pre-scale the samples so the fixed-point FFT keeps precision


def _dump_bin(path, snr_db=None, seed=1):
    import struct
    td, grid, payload = build_td(snr_db=snr_db, seed=seed)
    with open(path, "wb") as f:
        for s in range(NSYM):
            for c in td[s]:
                for v in (int(round(c.real * TD_SCALE)), int(round(c.imag * TD_SCALE))):
                    f.write(struct.pack("<I", v & 0xFFFFFFFF))


def dump_td(path, nid1=TRUE_NID1, nid2=TRUE_NID2):
    td, grid, payload = build_td(nid1, nid2)
    with open(path, "w") as f:
        for s in range(NSYM):
            for c in td[s]:
                f.write(f"{int(c.real) * TD_SCALE} {int(c.imag) * TD_SCALE}\n")
    return payload


def add_awgn(td, snr_db, seed=1):
    """Add complex AWGN to the four time-domain symbols.

    The SNR is defined over the *whole* 4-symbol block (signal power divided by
    noise power), which is what a receiver sees before it knows where the SSB
    starts.  Samples stay float here; _dump_bin quantises once, after scaling.
    """
    import math
    import random
    rnd = random.Random(seed)
    n = sum(len(s) for s in td)
    p_sig = sum(c.real * c.real + c.imag * c.imag for s in td for c in s) / n
    p_noise = p_sig / (10.0 ** (snr_db / 10.0))
    sigma = math.sqrt(p_noise / 2.0)
    return [[complex(c.real + rnd.gauss(0.0, sigma), c.imag + rnd.gauss(0.0, sigma))
             for c in s] for s in td]


def build_td(nid1=TRUE_NID1, nid2=TRUE_NID2, payload=None, amp=2000, snr_db=None,
             seed=1):
    """OFDM time-domain samples of the 4 SSB symbols (IFFT of the grid).

    snr_db adds AWGN (see add_awgn) so the same program can be swept over SNR;
    with snr_db = None the samples stay integer-valued exactly as before.
    """
    grid, payload, bits = build_grid(nid1, nid2, payload)
    sc = subcarriers()
    td = []
    for s in range(NSYM):
        freq = [0j] * NFFT
        for r in range(NRE):
            if grid[s][r] != 0:
                freq[sc[r]] = grid[s][r]
        td.append([int(round(c.real)) + 1j * int(round(c.imag))
                   for c in _fft(freq, inverse=True)])
    if snr_db is not None:
        td = add_awgn(td, snr_db, seed)
    return td, grid, payload


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="NR SSB golden model / sample dumper")
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("dump", help="write the 4-symbol time-domain samples")
    d.add_argument("file", nargs="?", default="td.bin")
    d.add_argument("--snr", type=float, default=None,
                   help="add AWGN at this SNR (dB over the whole 4-symbol block)")
    d.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    if a.cmd == "dump":
        _dump_bin(a.file, snr_db=a.snr, seed=a.seed)
        print(f"[info] wrote {a.file}"
              + (f" (SNR {a.snr} dB, seed {a.seed})" if a.snr is not None else ""))
    else:
        ap.print_help()
