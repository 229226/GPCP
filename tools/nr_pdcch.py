#!/usr/bin/env python3
"""NR PDCCH / DCI (SISO) golden model -- 38.211 7.3 + 38.212 7.3.

A compact but complete control-channel link, self-consistent end to end:

    DCI (32 bit) -> CRC24C -> RNTI-scrambled CRC -> Polar (N=512, K=56)
       -> 38.212 5.4.1 rate matching (E=432, AL=4) -> Gold scrambling
       -> QPSK -> AWGN -> soft LLR -> descramble -> inverse rate matching
       -> polar SC decode -> RNTI blind detection -> CRC24C -> DCI fields

Scope notes (deliberate simplifications, called out for the next iteration):
  * the polar reliability ordering is the natural/bit-reversed order, not the
    38.212 Table 5.3.1.2-1 sequence; the encoder/decoder pair is self-consistent
  * no PC bits, no REG/CCE interleaving, no PDCCH DMRS (AWGN-only channel)
  * RNTI blind detection re-runs the whole decode for each candidate, which is
    exactly the real search-space behaviour (just with a tiny candidate set)
"""
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_pbch as NR
from ssb_plan import bitrev, polar_encode, f_node, g_node

# ---- parameters -----------------------------------------------------------
A_DCI = 32
CRC_LEN = 24
K = A_DCI + CRC_LEN                 # 56 information bits for the polar code
NPOL = 512
DEPTH = 9
E_BITS = 432                        # aggregation level 4: 4 * 108 QPSK bits
QPSK = 2000
N_RNTI = 0x1234
N_ID = 0                            # scrambling identity (pdcch-DMRS-ScramblingID)
DEFAULT_SNR = 12.0
CRC_POLY = 0xB2B117                 # gCRC24C, 38.212 7.2


# ---- CRC24C ---------------------------------------------------------------
def crc24c(bits):
    reg = 0
    for b in bits:
        reg ^= (b & 1) << 23
        for _ in range(CRC_LEN):
            if reg & 0x800000:
                reg = ((reg << 1) ^ CRC_POLY) & 0xFFFFFF
            else:
                reg = (reg << 1) & 0xFFFFFF
    return [(reg >> (23 - i)) & 1 for i in range(CRC_LEN)]


def rnti_scramble_crc(crc, rnti):
    """38.212 7.3.2: XOR the first 16 CRC parity bits with the 16-bit RNTI."""
    c = list(crc)
    for k in range(16):
        c[k] ^= (rnti >> k) & 1
    return c


# ---- DCI payload ----------------------------------------------------------
DCI_FIELDS = [("mcs", 5), ("rv", 2), ("ndi", 1), ("harq_pid", 4),
              ("rb_start", 4), ("rb_len", 4), ("tpc", 2), ("reserved", 10)]


def pack_dci(fields):
    bits = []
    for name, width in DCI_FIELDS:
        v = int(fields.get(name, 0)) & ((1 << width) - 1)
        bits += [(v >> (width - 1 - i)) & 1 for i in range(width)]
    assert len(bits) == A_DCI
    return bits


def unpack_dci(bits):
    out, p = {}, 0
    for name, width in DCI_FIELDS:
        v = 0
        for i in range(width):
            v = (v << 1) | (bits[p + i] & 1)
        out[name] = v
        p += width
    return out


# ---- polar code (parameterised frozen set) --------------------------------
def is_frozen(i, n=NPOL, k=K, depth=DEPTH):
    return bitrev(i, depth) < (n - k)


def info_indices(n=NPOL, k=K, depth=DEPTH):
    return [i for i in range(n) if not is_frozen(i, n, k, depth)][:k]


def polar_encode_k(info, n=NPOL, k=K, depth=DEPTH):
    u = [0] * n
    for j, idx in enumerate(info_indices(n, k, depth)):
        u[idx] = info[j] & 1
    return polar_encode(u)


def sc_decode_k(llr, n=NPOL, k=K, depth=DEPTH):
    """Successive-cancellation decoder with min-sum f/g nodes."""
    lbuf = [[0] * n for _ in range(depth + 1)]
    bbuf = [[0] * n for _ in range(depth + 1)]
    lbuf[0] = list(llr[:n])

    def rec(d, o, size):
        if size == 1:
            bbuf[d][o] = 0 if is_frozen(o, n, k, depth) else (1 if lbuf[d][o] < 0 else 0)
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

    rec(0, 0, n)
    u = bbuf[depth]
    return [u[i] for i in info_indices(n, k, depth)]


# ---- transmit / receive ---------------------------------------------------
def scrambling_seq(rnti, length=E_BITS):
    """38.211 7.3.2.3: c_init = n_RNTI*2^15 + n_ID (n_ID = 0 here)."""
    return NR.gold(((rnti & 0xFFFF) << 15) + N_ID, length)


def qpsk_map(bits):
    return [complex((1 - 2 * bits[2 * i]) * QPSK,
                    (1 - 2 * bits[2 * i + 1]) * QPSK)
            for i in range(len(bits) // 2)]


def transmit(fields, rnti=N_RNTI):
    dci = pack_dci(fields)
    info = dci + rnti_scramble_crc(crc24c(dci), rnti)
    d = polar_encode_k(info)
    e = NR.rate_match_pbch(d, E_BITS)
    scr = scrambling_seq(rnti, E_BITS)
    e_sc = [b ^ s for b, s in zip(e, scr)]
    return qpsk_map(e_sc), dci


def add_awgn(syms, snr_db, seed=1):
    rnd = random.Random(seed)
    p_sig = sum(abs(v) ** 2 for v in syms) / len(syms)
    sigma = math.sqrt(p_sig / (2 * 10 ** (snr_db / 10)))
    return [complex(v.real + rnd.gauss(0, sigma), v.imag + rnd.gauss(0, sigma))
            for v in syms]


def decode_once(llr_e, rnti):
    scr = scrambling_seq(rnti, E_BITS)
    descr = [-v if scr[k] else v for k, v in enumerate(llr_e)]
    z = NR.rate_recover_pbch(descr, NPOL)
    info = sc_decode_k(z)
    dci, crc_scr = info[:A_DCI], info[A_DCI:]
    crc = rnti_scramble_crc(crc_scr, rnti)
    return (crc24c(dci) == crc), unpack_dci(dci), dci


def receive(y, candidates, rnti=N_RNTI):
    """RNTI blind detection: only the matching RNTI passes CRC24C."""
    llr = []
    for v in y:
        llr += [int(v.real), int(v.imag)]
    tried = []
    for cand in candidates:
        ok, fields, dci = decode_once(llr, cand)
        tried.append((cand, ok))
        if ok:
            return dict(rnti=cand, ok=True, fields=fields, dci=dci, tried=tried)
    return dict(rnti=None, ok=False, fields=None, dci=None, tried=tried)


CANDIDATES = [0x1111, 0x5678, N_RNTI, 0x9ABC, 0xDEF0]


def main():
    fields = dict(mcs=17, rv=2, ndi=1, harq_pid=3, rb_start=5, rb_len=8,
                  tpc=1, reserved=0)
    print("PDCCH/DCI: A=%d K=%d N=%d E=%d (AL=4), RNTI=0x%04x" % (
        A_DCI, K, NPOL, E_BITS, N_RNTI))
    print("  TX DCI: %s" % fields)
    for snr in (8.0, 10.0, 12.0, 15.0):
        hits = 0
        result = None
        for seed in range(1, 5):
            syms, _ = transmit(fields)
            y = add_awgn(syms, snr, seed)
            r = receive(y, CANDIDATES)
            hits += 1 if r["ok"] and r["rnti"] == N_RNTI else 0
            result = r
        print("  SNR %5.1f dB : blind detect %d/4, last tried %s" % (
            snr, hits, [("0x%04x" % c, ok) for c, ok in result["tried"]]))
    syms, _ = transmit(fields)
    r = receive(add_awgn(syms, DEFAULT_SNR, 1), CANDIDATES)
    print("  decoded DCI: %s" % (r["fields"] if r["ok"] else "FAILED"))
    ok = r["ok"] and r["rnti"] == N_RNTI and r["fields"] == fields
    print("[ %s ] PDCCH/DCI blind detection" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
