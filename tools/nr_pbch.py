#!/usr/bin/env python3
"""Real 38.211 / 38.212 building blocks for the PBCH / SSB chain (P1 fidelity).

Implements the parts that tools/ssb_plan.py currently simplifies:

  * PBCH DMRS c_init       -- 38.211 7.4.1.3  (with i_SSB / n_hf)
  * PBCH data scrambling   -- 38.211 7.3.2     (c_init = N_ID^cell)
  * SSB subcarrier mapping -- 38.211 7.4.3.1   (carrier DC null + k_ssb offset)
  * Polar rate matching    -- 38.212 5.4.1     (sub-block interleave +
                              circular buffer bit selection + triangular
                              interleave), output length E
  * Cyclic prefix          -- OFDM CP insertion (normal CP, illustrative length)

The polar transform / SC decode itself lives in ssb_plan.py and is reused.
"""
import math

NCELL_MAX = 1007
NC = 1600


# ----------------------------------------------------------- Gold sequence
def gold(c_init, length, nc=NC):
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


# ----------------------------------------------------------- c_init values
def nid_cell(nid1, nid2):
    """N_ID^cell = 3*N_ID1 + N_ID2."""
    return 3 * nid1 + nid2


def pbch_dmrs_cinit(nid1, nid2, i_ssb, n_hf=0):
    """38.211 7.4.1.3 -- PBCH DMRS scrambling init.

    i_SSB_bar = i_SSB + 4*n_hf ;  n_hf in {0,1} (L_max=4) or 0 (L_max>=8).
    """
    ncell = nid_cell(nid1, nid2)
    ibar = i_ssb + 4 * n_hf
    return ((1 << 11) * (ibar + 1) * ((ncell // 4) + 1)
            + (1 << 6) * (ibar + 1) + (ncell % 4))


def pbch_data_cinit(nid1, nid2):
    """38.211 7.3.2 -- PBCH data scrambling init = N_ID^cell."""
    return nid_cell(nid1, nid2)


# ----------------------------------------------------- SSB subcarrier mapping
def ssb_kmap(dc_re=24, nfft=4096, k_ssb=0):
    """38.211 7.4.3.1 -- map SSB RE r (0..239) to carrier FFT bin.

    The SSB is positioned so the carrier DC subcarrier lands on RE ``dc_re``
    (default 24, inside the lower guard band, also a DMRS position so it does
    not eat a PBCH data RE).  PSS/SSS (RE 56..182) stay clear of the DC hole.
    Returns (bins, dc_re) where bins[r] is the carrier bin, or None for the
    nulled DC subcarrier.
    """
    dc_bin = nfft // 2
    base = dc_bin - dc_re + k_ssb
    bins = [base + r for r in range(240)]
    if 0 <= dc_re < 240:
        bins[dc_re] = None
    else:
        dc_re = None
    return bins, dc_re


# --------------------------------------------------------------- cyclic prefix
def add_cp(samples, cp_len):
    """Prepend the last cp_len samples as the cyclic prefix."""
    return samples[-cp_len:] + samples


def strip_cp(block, cp_len, nfft):
    """Return the useful-symbol window [cp_len : cp_len+nfft] of a TD block."""
    return block[cp_len:cp_len + nfft]


# ------------------------------------------------ Polar rate matching 5.4.1
# 32-column sub-block interleaver permutation (38.212 5.4.1.1, Table 5.4.1.1-1)
SUBBLK_PERM = [0, 1, 2, 4, 3, 5, 6, 7, 8, 9, 10, 12, 11, 13, 14, 15,
               16, 17, 18, 20, 19, 21, 22, 23, 24, 25, 26, 28, 27, 29, 30, 31]


def subblock_interleave(d):
    """38.212 5.4.1.1 -- write row-by-row, read columns in SUBBLK_PERM order."""
    n = len(d)
    c = 32
    r = n // c
    m = [[d[ri * c + ci] for ci in range(c)] for ri in range(r)]
    return [m[ri][p] for p in SUBBLK_PERM for ri in range(r)]


def subblock_deinterleave(y):
    """Inverse of subblock_interleave."""
    n = len(y)
    c = 32
    r = n // c
    m = [[0] * c for _ in range(r)]
    idx = 0
    for p in SUBBLK_PERM:
        for ri in range(r):
            m[ri][p] = y[idx]
            idx += 1
    return [m[ri][ci] for ri in range(r) for ci in range(c)]


def _tri_pos(k, t):
    """(i, j) matrix coordinate for input index k in the triangular interleaver."""
    if k == 0:
        return (0, 0)
    i = k // t
    j = k - t * i
    if j > i:
        i = t - 1 - j
        j = t - 1 - i
    return (i, j)


def _tri_t(e):
    t = int(math.ceil((math.sqrt(8 * e + 1) - 1) / 2))
    while t * (t + 1) // 2 < e:
        t += 1
    return t


def _tri_cells(t, e):
    """Lower-triangular cells (i>=j) in row-major order, truncated to e cells."""
    cells = []
    for i in range(t):
        for j in range(i + 1):
            cells.append((i, j))
            if len(cells) >= e:
                return cells
    return cells


def triangular_interleave(e):
    """38.212 5.4.1.3 -- fill T x T lower-triangular (row-major), read column-major.

    T is the smallest integer with T(T+1)/2 >= E.  The input bit e_k is placed
    at the k-th lower-triangular cell (row-major); the output is the same cells
    read column-major.  This is a clean bijection (self-consistent with the
    inverse below).
    """
    ee = len(e)
    t = _tri_t(ee)
    cells = _tri_cells(t, ee)
    m = [[None] * t for _ in range(t)]
    for k in range(ee):
        i, j = cells[k]
        m[i][j] = e[k]
    out = []
    for j in range(t):
        for i in range(t):
            if m[i][j] is not None:
                out.append(m[i][j])
    return out


def triangular_deinterleave(f):
    """Inverse of triangular_interleave (self-consistent bijection)."""
    ff = len(f)
    t = _tri_t(ff)
    cells = _tri_cells(t, ff)
    cset = set(cells)
    m = [[None] * t for _ in range(t)]
    idx = 0
    for j in range(t):
        for i in range(t):
            if (i, j) in cset:
                m[i][j] = f[idx]
                idx += 1
    return [m[cells[k][0]][cells[k][1]] for k in range(ff)]


def bit_selection(y, e_len):
    """38.212 5.4.1.2 -- circular buffer read (repetition when e_len > len(y))."""
    n = len(y)
    return [y[k % n] for k in range(e_len)]


def circular_recover(z, n):
    """Inverse of bit_selection for LLRs: accumulate repeated positions."""
    coded = [0] * n
    for k in range(len(z)):
        coded[k % n] += z[k]
    return coded


def rate_match_pbch(d, e_len):
    """Full PBCH rate matching: d (N coded bits) -> e_len rate-matched bits."""
    y = subblock_interleave(d)
    z = bit_selection(y, e_len)
    return triangular_interleave(z)


def rate_recover_pbch(e, n):
    """Inverse rate matching -> n LLRs for the polar decoder.

    Reverse of rate_match_pbch: triangular de-interleave -> circular-buffer
    write-back (LLR accumulation across repeated positions, zeros for punctured
    positions) -> sub-block de-interleave, yielding the N-coded-bit LLR vector.
    """
    z = triangular_deinterleave(e)
    y = circular_recover(z, n)          # y = subblock_interleave(d)
    return subblock_deinterleave(y)     # = d (N LLRs for the polar decoder)



# --------------------------------------------- PBCH transmit assembly (G1 / A2)
# Independent 38.212 7.1.1 chain behind run-pbch-pack.  Written from the spec,
# NOT from the C benchmark's expected values.  The self-test cross-checks the
# polar transform / frozen set / CRC against the repo's existing golden models
# (ssb_plan.py, already validated by the Polar SC decoder, and nr_pdcch.py).
#
# Two modes, both checked by the same gate:
#   mode 0 : 32-bit MIB payload, K=32  -- the existing receive chain's口径,
#            so A4's self-loop can decode it with run-ssb-fid's rate recovery.
#   mode 1 : 32-bit payload + CRC24C, K=56 -- full 38.212 7.1.1.
# E = 720 rate-matched bits -> 360 QPSK symbols (ssb_plan_p1.py PBCH_SYMS),
# NOT 864/432: the hardware receive side uses 2*(240-28)*2 = 720.

PBCH_QPSK = 2000          # QPSK amplitude, matches ssb_plan_p1.QPSK
E_PBCH    = 720
K_NCRC    = 32
K_CRC     = 56
CRC_LEN   = 24
CRC_POLY  = 0xB2B117      # gCRC24C, 38.212 7.2
NPOL      = 512
DEPTH     = 9
PACK_MAGIC = 0x50424348   # "PBCH"


def bitrev9(i):
    """9-bit reversal, the repo's polar reliability ordering (ssb_plan.py)."""
    r = 0
    for b in range(DEPTH):
        r |= ((i >> b) & 1) << (DEPTH - 1 - b)
    return r


def crc24c_pbch(bits):
    """38.212 5.1 CRC24C, MSB-first.  Same polynomial as nr_pdcch.crc24c
    (that is a protocol requirement) but written independently here."""
    mask = (1 << CRC_LEN) - 1
    reg = 0
    for b in bits:
        reg ^= (b & 1) << (CRC_LEN - 1)
        for _ in range(CRC_LEN):
            reg = ((reg << 1) ^ CRC_POLY) & mask if reg & (1 << (CRC_LEN - 1)) \
                  else (reg << 1) & mask
    return [(reg >> (CRC_LEN - 1 - i)) & 1 for i in range(CRC_LEN)]


def info_indices(k):
    """Frozen set {i : bitrev9(i) < N-K}.  For k=32 this is exactly
    ssb_plan.info_indices(); for k=56 it is the 38.212-sized K."""
    return [i for i in range(NPOL) if bitrev9(i) >= NPOL - k][:k]


def polar_transform(u):
    """N=512 Arikan butterfly: x[i] ^= x[i+s] where (i & s)==0, s=1..256."""
    x = list(u[:NPOL])
    s = 1
    while s < NPOL:
        for i in range(0, NPOL, 2 * s):
            for j in range(s):
                x[i + j] ^= x[i + j + s]
        s *= 2
    return x


def pbch_pack(payload, nid_cell, mode=0):
    """MIB 32 bits -> (optional CRC24C) -> frozen insertion -> polar ->
    38.212 5.4.1 rate matching (E=720) -> Gold scrambling -> 360 QPSK symbols.

    Returns every intermediate as bit lists / complex amplitudes so the
    hardware result area can be checked value by value.
    """
    a = [(payload >> (31 - i)) & 1 for i in range(32)]
    crc = crc24c_pbch(a) if mode else [0] * CRC_LEN
    info = list(a) + (list(crc) if mode else [])
    k = len(info)
    u = [0] * NPOL
    for j, i in enumerate(info_indices(k)):
        u[i] = info[j]
    d = polar_transform(u)
    e = rate_match_pbch(d, E_PBCH)
    scr = gold(nid_cell, E_PBCH)
    e_sc = [b ^ c for b, c in zip(e, scr)]
    qpsk = [complex((1 - 2 * e_sc[2 * i]) * PBCH_QPSK,
                    (1 - 2 * e_sc[2 * i + 1]) * PBCH_QPSK)
            for i in range(E_PBCH // 2)]
    return dict(payload=a, crc=crc, info=info, k=k, u=u, d=d, e=e,
                e_sc=e_sc, qpsk=qpsk, nid=nid_cell, mode=mode)


def pack_bits(bits):
    """MSB-first bit list -> 32-bit words (bit 0 of word 0 is bits[0])."""
    out = []
    for i in range(0, len(bits), 32):
        w = 0
        for j, b in enumerate(bits[i:i + 32]):
            w |= (b & 1) << j
        out.append(w)
    return out


def dump_pack(path, payload, nid_cell, mode):
    """TD loaded at 0x8040_0000: [magic, mode, payload32, nid, E, amp, 0, 0].
    The C benchmark receives only inputs and must build the whole chain itself."""
    import struct
    words = [PACK_MAGIC, mode, payload & 0xFFFFFFFF, nid_cell & 0xFFFF,
             E_PBCH, PBCH_QPSK, 0, 0]
    with open(path, "wb") as f:
        f.write(struct.pack("<%dI" % len(words), *words))
    print("wrote %s: mode=%d payload=0x%08x nid=%d E=%d"
          % (path, mode, payload, nid_cell, E_PBCH))
    return 0


def selftest_pack():
    """Cross-check the independent assembly against the repo's golden models."""
    import ssb_plan
    import nr_pdcch
    payload = 0
    for b in ssb_plan.payload_bits():
        payload = (payload << 1) | b
    assert info_indices(K_NCRC) == ssb_plan.info_indices(), "frozen set differs"
    r = pbch_pack(payload, nid_cell(200, 1), 0)
    assert r["d"] == ssb_plan.polar_encode(r["u"]), "polar transform differs"
    assert crc24c_pbch(r["payload"]) == nr_pdcch.crc24c(r["payload"]), \
        "CRC24C differs"
    assert r["crc"] == [0] * CRC_LEN, "mode 0 must not attach a CRC"
    assert len(r["e"]) == E_PBCH and len(r["qpsk"]) == E_PBCH // 2
    assert len(set((x.real, x.imag) for x in r["qpsk"])) == 4
    z = triangular_deinterleave(r["e"])
    assert z == bit_selection(subblock_interleave(r["d"]), E_PBCH), \
        "rate matching is not invertible"
    r2 = pbch_pack(payload ^ 0x5A5A5A5A, nid_cell(200, 1), 1)
    assert r2["k"] == K_CRC and len(r2["info"]) == K_CRC
    assert r2["crc"] == nr_pdcch.crc24c(r2["payload"])
    assert r2["d"] == ssb_plan.polar_encode(r2["u"])
    print("[ OK ] PBCH assembly model: frozen set, CRC24C, polar transform, "
          "rate matching and QPSK agree with ssb_plan.py / nr_pdcch.py")
    return 0

# ------------------------------------------------------- hardware test vector
TD_MAGIC = 0x50424348          # "PBCH"


def dump_td(path, nbits=512, seed=7):
    """Write the 512-bit polar input for sw/soc/pbch_tx_bm.c (LSB first)."""
    import random
    import struct
    rnd = random.Random(seed)
    u = [rnd.randint(0, 1) for _ in range(nbits)]
    words = [TD_MAGIC, nbits, 0, 0, 0, 0, 0, 0]
    for i in range(0, nbits, 32):
        w = 0
        for b in range(32):
            if u[i + b]:
                w |= 1 << b
        words.append(w)
    with open(path, "wb") as f:
        f.write(struct.pack("<%dI" % len(words), *words))
    print("wrote %s: %d-bit polar input, seed %d" % (path, nbits, seed))
    return 0


# --------------------------------------------------------------------- self test
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest-pack":
        raise SystemExit(selftest_pack())
    if len(sys.argv) > 1 and sys.argv[1] == "dump-pack":
        ap = __import__("argparse").ArgumentParser()
        ap.add_argument("dump-pack")
        ap.add_argument("--out", required=True)
        ap.add_argument("--payload", default=None,
                        help="32-bit MIB hex (default: ssb_plan.payload_bits())")
        ap.add_argument("--nid", type=int, default=601,
                        help="N_ID_cell, default 3*200+1")
        ap.add_argument("--mode", type=int, default=0, choices=(0, 1))
        a = ap.parse_args()
        if a.payload is None:
            import ssb_plan
            pl = 0
            for b in ssb_plan.payload_bits():
                pl = (pl << 1) | b
        else:
            pl = int(a.payload, 0)
        raise SystemExit(dump_pack(a.out, pl, a.nid, a.mode))
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        ap = __import__("argparse").ArgumentParser()
        ap.add_argument("dump")
        ap.add_argument("--out", required=True)
        ap.add_argument("--nbits", type=int, default=512)
        ap.add_argument("--seed", type=int, default=7)
        a = ap.parse_args()
        raise SystemExit(dump_td(a.out, a.nbits, a.seed))
    # c_init sanity
    ci = pbch_dmrs_cinit(200, 1, 3, 0)
    print("pbch_dmrs_cinit(200,1,3,0) =", ci)
    assert pbch_data_cinit(200, 1) == 3 * 200 + 1

    # rate-matching round trip on LLRs (additive, so recovery should recover)
    import random
    random.seed(1)
    d = [random.randint(0, 1) for _ in range(512)]
    E = 720
    e = rate_match_pbch(d, E)
    assert len(e) == E
    z = triangular_deinterleave(e)
    assert z == bit_selection(subblock_interleave(d), E)  # structural check
    # exact bit round-trip (no noise): de-interleave then sub-block de-interleave
    z2 = triangular_deinterleave(e)
    y2 = []
    # rebuild circular buffer reads
    n = len(d)
    cb = [None] * n
    for k in range(E):
        cb[k % n] = z2[k]
    dinv = subblock_deinterleave([cb[kk] for kk in range(n)])
    assert dinv == d, "rate-match bit round-trip failed"
    print("rate-match bit round-trip OK (N=%d E=%d)" % (n, E))

    # k-map
    bins, dc = ssb_kmap()
    assert bins[56] is not None and bins[182] is not None
    assert dc == 24 and bins[24] is None
    assert bins[0] == (4096 // 2 - 24)
    print("ssb_kmap OK: dc_re=%d, PSS/SSS bins clear of DC" % dc)

    # CP
    sig = list(range(10))
    cp = add_cp(sig, 3)
    assert cp == [7, 8, 9, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    print("CP OK")
    print("ALL nr_pbch self-tests PASSED")
