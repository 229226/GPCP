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


# --------------------------------------------------------------------- self test
if __name__ == "__main__":
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
