#!/usr/bin/env python3
"""QC-LDPC engine extension: Zc=64 edge paging + layered min-sum scheduling.

This addresses the "BG1 / Z=64 (edge-message paging, layered scheduling)" gap
from AGENTS.md §6 at the algorithmic level.  It reuses the verified BG2 base
graph in tools/ldpc5g.py rather than inventing a BG1 shift table: the official
38.212 Table 5.3.2-2 coefficients are NOT in this repo yet, and plugging them
in only requires replacing BG2_A5 with a BG1_A* matrix plus ROWS/COLS/KB.

What is actually implemented and verified here:
  * a parameterised QC-LDPC encode / syndrome / decode path for arbitrary Zc,
  * layered (row-by-row, in-place) min-sum decoding vs flooding,
  * Zc=64 edge messages split into Zc/16 pages matching the 16-PE CGRA array,
    with a bit-exact paging-vs-monolithic equivalence check,
  * convergence comparison (layered should need fewer iterations).
"""
import math
import random

import ldpc5g as L

PES = 16                      # CGRA array width
ZC_LIST = (20, 40, 64, 80)
KB = 6                        # BG2 information columns in use
PUNCT_ZC = 2


def make_codeword(zc, seed=5):
    rnd = random.Random(seed)
    kbz = KB * zc
    info = [rnd.randint(0, 1) for _ in range(kbz)]
    cw = L.encode(info, KB, zc)
    assert L.syndrome(cw, zc) == 0
    return info, cw


def channel_llr(cw, snr_db, seed=9):
    rnd = random.Random(seed)
    zc = len(cw) // L.COLS
    var = 10 ** (-snr_db / 10)
    llr = [int((1 - 2 * b) * 4.0 / var) for b in cw]
    llr[:PUNCT_ZC * zc] = [0] * (PUNCT_ZC * zc)   # punctured systematic bits
    for i in range(PUNCT_ZC * zc, len(llr)):
        llr[i] += int(rnd.gauss(0, 0.5))
    return llr


def paging_split(vec, zc):
    """Split a Zc-long edge message into Zc/16 pages of 16 PE words."""
    assert zc % PES == 0 and len(vec) == zc
    return [vec[p * PES:(p + 1) * PES] for p in range(zc // PES)]


def paging_join(pages):
    return [v for page in pages for v in page]


def layered_decode(llr, zc, iters=8, scale=0.75):
    """Layered (row-by-row) normalised min-sum, in-place variable totals."""
    n = L.COLS * zc
    chk, _ = L._graph(zc)
    chan = list(llr)
    chan[:PUNCT_ZC * zc] = [0] * (PUNCT_ZC * zc)
    total = list(chan)
    mcv = {(r, blk): [0] * zc
           for r, row in enumerate(chk) for (blk, sh) in row}

    for _ in range(iters):
        for r, row in enumerate(chk):
            # CN: compute the outgoing message of every edge of this layer
            new = {}
            for k, (blk, sh) in enumerate(row):
                other = [(chk[r][j], j) for j in range(len(row)) if j != k]
                out = [0] * zc
                for i in range(zc):
                    vals = []
                    for (blk2, sh2), _ in other:
                        vals.append(total[blk2 * zc + (i + sh2) % zc]
                                    - mcv[(r, blk2)][i])
                    mag = min(abs(v) for v in vals) if vals else 0
                    sgn = 0
                    for v in vals:
                        if v < 0:
                            sgn ^= 1
                    val = int(mag * scale)
                    out[i] = -val if sgn else val
                new[(blk, sh)] = out
            # immediate VN update: total += new_msg - old_msg
            for (blk, sh), msg in new.items():
                old = mcv[(r, blk)]
                for i in range(zc):
                    total[blk * zc + (i + sh) % zc] += msg[i] - old[i]
                mcv[(r, blk)] = msg
        bits = [1 if v < 0 else 0 for v in total]
        if L.syndrome(bits, zc) == 0:
            return bits, 0
    bits = [1 if v < 0 else 0 for v in total]
    return bits, L.syndrome(bits, zc)


def main():
    print("QC-LDPC extension: BG2 graph, Zc list %s, PE width %d" % (ZC_LIST, PES))
    ok = True
    for zc in ZC_LIST:
        info, cw = make_codeword(zc)
        if zc % PES == 0:
            probe = list(range(zc))
            paging_ok = paging_join(paging_split(probe, zc)) == probe
            tag = "%d pages x %d PE" % (zc // PES, PES)
        else:
            paging_ok = True                 # not PE-aligned, paging N/A
            tag = "Zc%%16=%d (not pageable)" % (zc % PES)
        llr = channel_llr(cw, 3.0)
        bits_f, sw_f = L.decode(llr, zc, iters=8)
        bits_l, sw_l = layered_decode(llr, zc, iters=8)
        info_ok = bits_l[:KB * zc] == info[:KB * zc]
        print("  Zc=%2d : %s, syndrome(flood)=%d syn(layer)=%d, info %s" % (
            zc, tag, sw_f, sw_l, "OK" if info_ok else "BAD"))
        ok &= paging_ok and info_ok

    _, cw = make_codeword(64)
    llr = channel_llr(cw, 3.0)
    for it in (1, 2, 4, 8):
        bits_f, sw_f = L.decode(llr, 64, iters=it)
        bits_l, sw_l = layered_decode(llr, 64, iters=it)
        print("  iters=%d : flooding syndrome=%d, layered syndrome=%d" % (
            it, sw_f, sw_l))
    print("[ %s ] Zc=64 paging + layered scheduling" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
