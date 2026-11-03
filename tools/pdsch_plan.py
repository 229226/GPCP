#!/usr/bin/env python3
"""PDSCH (SISO) golden model - step 1 of the "P2" milestone.

Chain (38.211/38.212, simplified where stated):

  TB (A bits)  -> CRC24A  -> 38.212 LDPC (BG2, Kb=6, Zc=20)
               -> rate matching (bit selection over the circular buffer, filler
                  bits skipped) -> scrambling (Gold, c_init = n_RNTI)
               -> QPSK -> RE mapping (2 symbols x 240 RE, DMRS every 4th RE)
               -> channel (flat gain + AWGN;  multipath = same EPA profile as
                  the SSB model) -> per-symbol LS channel estimate
               -> equalise -> QPSK soft LLR -> descramble -> rate de-matching
               -> min-sum LDPC decode -> CRC24A check -> TB compare

Not implemented yet (documented, next steps): bit interleaving (5.4.2.2),
layer mapping / precoding (SISO only), HARQ soft combining, PDCCH/DCI,
DCI-driven MCS, and the CGRA microcode for the new kernels.

Numbers: A = 80, K = 104, Kb = 6, Zc = 20 -> N = 1040, E = 720 (QPSK on
2 x 180 data REs).
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ldpc5g as L

A_TB = 80                 # transport block length (bits)
CRC_LEN = 24
KB, ZC = 6, 20
NCODED = 52 * ZC
PUNCT = 2 * ZC
NSYM = 2
NRE = 240
DMRS_STEP = 4
NPILOT = NRE // DMRS_STEP              # 60 pilots per symbol
NDATA_RE = NRE - NPILOT               # 180 data REs per symbol
E_BITS = NSYM * NDATA_RE * 2           # QPSK: 2 bits per data RE  (= 720)
QPSK = 2000                # amplitude, keeps int32 MAC safe
H_SCALE = 16
CH_RE, CH_IM = 2, 1
N_RNTI = 0x1234


# --------------------------------------------------------------- CRC24A
def crc24a(bits):
    """38.212 7.1: gCRC24A(D) = D^24 + D^23 + D^18 + D^17 + D^14 + D^11
    + D^10 + D^7 + D^6 + D^5 + D^4 + D^3 + D + 1  (0x864CFB)."""
    poly = 0x864CFB
    reg = 0
    for b in bits:
        reg ^= (b & 1) << 23
        for _ in range(24):
            reg = ((reg << 1) ^ poly) & 0xFFFFFF if (reg & 0x800000) else (reg << 1) & 0xFFFFFF
    return [(reg >> (23 - i)) & 1 for i in range(24)]


def payload_bits(seed=0xB00B5):
    g = Lcg(seed)
    return [(g.step() >> 31) & 1 for _ in range(A_TB)]


# --------------------------------------------------------------- scrambling
def gold(c_init, length, nc=1600):
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


def scrambling_seq(n_rnti=N_RNTI):
    """38.211 7.3.3.1 with q=0, n_f=0 (c_init = n_RNTI * 2^15)."""
    return gold(n_rnti << 15, E_BITS)


class Lcg:
    def __init__(self, seed):
        self.s = seed

    def step(self):
        self.s = (self.s * 1103515245 + 12345) & 0xFFFFFFFF
        return self.s

    def noise(self):
        return ((self.step() >> 16) & 0x3FF) - 512


# --------------------------------------------------------------- rate match
def rate_match(cw):
    """38.212 5.4.2.1 bit selection, rv=0, then QPSK bit collection.

    The circular buffer is the coded bits without the 2*Zc punctured ones; the
    filler bits of the code block are marked NULL and skipped.  Repetition is
    allowed when E > Ncb.
    """
    buf = cw[PUNCT:]
    # filler = the zero pad of the code block, in *buffer* coordinates
    # (the buffer starts after the 2*Zc punctured bits)
    filler = set(range(A_TB + CRC_LEN - PUNCT, KB * ZC - PUNCT))
    usable = [b for i, b in enumerate(buf) if i not in filler]
    out = []
    j = 0
    while len(out) < E_BITS:
        out.append(usable[j % len(usable)])
        j += 1
    return out, usable, sorted(filler)


def rate_dematch(llr, usable_len, filler_in_buf):
    """Inverse of rate_match: sum the repeated LLRs into the circular buffer."""
    buf = [0] * usable_len
    for k, v in enumerate(llr[:E_BITS]):
        buf[k % usable_len] += v
    full = []
    u = 0
    for i in range(NCODED - PUNCT):
        if i in filler_in_buf:
            full.append(0)          # filler: known zero, but it is not coded
        else:
            full.append(buf[u])
            u += 1
    return [0] * PUNCT + full


# --------------------------------------------------------------- modulation
def qpsk_map(bits):
    return [complex((1 - 2 * bits[2 * k]) * QPSK, (1 - 2 * bits[2 * k + 1]) * QPSK)
            for k in range(len(bits) // 2)]


def qpsk_llr(sym, h):
    """max-log QPSK: LLR(b0) = Re(y*conj(h)), LLR(b1) = Im(y*conj(h))."""
    eq = sym * complex(h.real, -h.imag)
    return [int(eq.real), int(eq.imag)]


def pilot_res():
    return list(range(0, NRE, DMRS_STEP))


def data_res():
    pilots = set(pilot_res())
    return [r for r in range(NRE) if r not in pilots]


def build_grid(bits):
    """Map E_BITS coded+scrambled bits onto NSYM x NRE (pilots stay zero)."""
    syms = qpsk_map(bits)
    grid = [[0j] * NRE for _ in range(NSYM)]
    # step-1 simplification: DMRS = constant +QPSK on every 4th RE (no Gold
    # sequence yet); the receiver assumes exactly these values, so the link
    # isolates the data path rather than the pilot pattern
    for s in range(NSYM):
        for r in pilot_res():
            grid[s][r] = complex(QPSK, QPSK)
    n = 0
    for s in range(NSYM):
        for r in data_res():
            grid[s][r] = syms[n]
            n += 1
    return grid


def add_channel(grid, snr_db, seed=1, multipath=False):
    """Flat gain (+ AWGN); multipath = frequency-selective phase per RE."""
    import random as _r
    rnd = _r.Random(seed)
    p_sig = sum(abs(v) ** 2 for row in grid for v in row) / (NSYM * len(data_res()))
    p_noise = p_sig / (10 ** (snr_db / 10))
    sigma = math.sqrt(p_noise / 2)
    h = complex(CH_RE, CH_IM)
    y = [[0j] * NRE for _ in range(NSYM)]
    for s in range(NSYM):
        for r in range(NRE):
            g = h
            if multipath:
                # two-tap frequency-selective gain (phase ramp across 240 RE)
                g = h * (1 + 0.5 * complex(math.cos(0.05 * r), math.sin(0.05 * r)))
            v = grid[s][r] * g if grid[s][r] != 0 else 0j
            y[s][r] = complex(v.real + rnd.gauss(0, sigma), v.imag + rnd.gauss(0, sigma))
    return y


# --------------------------------------------------------------- receiver
def channel_estimate(y):
    """Per-symbol LS gain from the pilots (they are known +QPSK here, i.e.
    pilots carry the all-zero bit pair and are not scrambled)."""
    h = []
    den = NPILOT * 2 * QPSK * QPSK // H_SCALE
    for s in range(NSYM):
        acc = 0j
        for r in pilot_res():
            acc += y[s][r] * complex(QPSK, -QPSK)   # y * conj(pilot)
        h.append(complex(int(acc.real // den), int(acc.imag // den)))
    return h


def extract_llr(y, h):
    llr = []
    for s in range(NSYM):
        for r in data_res():
            llr += qpsk_llr(y[s][r], h[s])
    return llr


def receiver(y, seed=0xB00B5):
    h = channel_estimate(y)
    llr = extract_llr(y, h)
    scr = scrambling_seq()
    llr = [-v if scr[k] else v for k, v in enumerate(llr)]
    dem = rate_dematch(llr, *RM_INFO)
    bits, sw = L.decode(dem, ZC, iters=20)
    tb = bits[:A_TB]                  # systematic bits live in cw[0:A_TB]
    crc = bits[A_TB:A_TB + CRC_LEN]
    ok = crc24a(tb) == crc
    errs = sum(1 for a, b in zip(tb, payload_bits(seed)) if a != b)
    return dict(h=h, syndrome=sw, crc_ok=ok, tb_errors=errs, tb=tb)


# --------------------------------------------------------------- link
RM_INFO = (None, None, None)   # filled by build_link: (usable_len, filler_idx)


def build_link(seed=0xB00B5):
    """Encode + scramble + modulate the whole chain (transmitter side)."""
    tb = payload_bits(seed)
    cw = L.encode(tb + crc24a(tb) + [0] * (KB * ZC - A_TB - CRC_LEN), KB, ZC)
    e, usable, filler = rate_match(cw)
    global RM_INFO
    RM_INFO = (len(usable), filler)
    scr = scrambling_seq()
    e = [b ^ s for b, s in zip(e, scr)]
    return build_grid(e)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="PDSCH SISO golden model")
    ap.add_argument("--snr", type=float, nargs="+", default=[0, 2, 4, 6])
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--multipath", action="store_true")
    a = ap.parse_args()

    grid = build_link()
    print(f"TB {A_TB} bits, K {A_TB + CRC_LEN}, Kb {KB}, Zc {ZC}, "
          f"N {NCODED}, E {E_BITS} ({E_BITS // 2} QPSK symbols)")
    print(f"{'SNR dB':>7} | {'CRC ok':>7} | {'TB err':>6} | sync")
    for snr in a.snr:
        ok = errs = 0
        for sd in range(1, a.seeds + 1):
            y = add_channel(grid, snr, sd, a.multipath)
            r = receiver(y)
            ok += r["crc_ok"]
            errs += r["tb_errors"]
        print(f"{snr:7.1f} | {ok:3d}/{a.seeds:<3d} | {errs:6d} | "
              f"h={r['h'][0]}")


if __name__ == "__main__":
    main()
