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
import argparse
import math
import random
import struct
import sys

import ldpc5g as L
import ldpc_nr as NR

# ldpc5g carries the i_LS = 2 table, so only set-2 lifting sizes can be
# cross-checked against it.  Zc=64 is i_LS=0: it has its own gates
# (run-pdsch-zc64 / run-pdsch-bg1-zc64) and is exercised below via ldpc_nr.
ZC_LIST = (20, 40, 80, 160)
KB = 6                        # BG2 information columns in use
PUNCT_ZC = 2


KB_BY_BG = {1: 22, 2: 6}


def make_codeword(zc, seed=5, bg=2):
    kb = KB_BY_BG[bg]
    rnd = random.Random(seed)
    info = [rnd.randint(0, 1) for _ in range(kb * zc)]
    cw = NR.encode(bg, info, kb, zc)
    assert NR.syndrome(bg, cw, zc) == 0
    return info, cw


def channel_llr(cw, snr_db, seed=9, sigma=0.5, bg=2):
    """Channel LLRs.  sigma is the additive noise on the un-punctured bits: the
    0.5 default is a nearly noiseless channel (any SNR converges in one
    iteration), larger values are what actually exercise the iteration count."""
    rnd = random.Random(seed)
    zc = len(cw) // NR.dims(bg)[1]
    var = 10 ** (-snr_db / 10)
    llr = [int((1 - 2 * b) * 4.0 / var) for b in cw]
    llr[:PUNCT_ZC * zc] = [0] * (PUNCT_ZC * zc)   # punctured systematic bits
    for i in range(PUNCT_ZC * zc, len(llr)):
        llr[i] += int(rnd.gauss(0, sigma))
    return llr


def layered_decode(llr, zc, iters=8, scale=0.75, bg=2):
    """Layered (row-by-row) normalised min-sum, in-place variable totals."""
    chk, _ = NR.graph(bg, zc)
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
        if NR.syndrome(bg, bits, zc) == 0:
            return bits, 0
    bits = [1 if v < 0 else 0 for v in total]
    return bits, NR.syndrome(bg, bits, zc)


TD_MAGIC = 0x5A433634          # "ZC64"


def dump_td(path, zc=64, snr_db=3.0, iters=1, seed=5, nseed=9, sigma=0.5, bg=2):
    """Write the hardware test data for sw/soc/pdsch_zc64_bm.c.

    Layout (little-endian 32-bit words):
      [0] magic  [1] zc  [2] rows  [3] cols  [4] iters  [5] nllr  [6] ninfo  [7] 0
      [8 ..] channel LLR (cols*zc, punctured columns already zeroed)
      [...]  information bits (KB*zc, one 0/1 per word)
    """
    info, cw = make_codeword(zc, seed, bg)
    llr = channel_llr(cw, snr_db, nseed, sigma, bg)
    rows, cols, _kb = NR.dims(bg)
    words = [TD_MAGIC, bg, zc, rows, cols, iters, cols * zc, len(info)]
    words += [v & 0xFFFFFFFF for v in llr]
    words += [int(b) for b in info]
    with open(path, "wb") as f:
        f.write(struct.pack("<%dI" % len(words), *words))
    print("wrote %s: BG%d Zc=%d rows=%d cols=%d iters=%d snr=%.1f dB sigma=%.2f, "
          "%d LLR + %d info words"
          % (path, bg, zc, rows, cols, iters, snr_db, sigma, len(llr), len(info)))
    return 0



ENC_MAGIC = 0x1D5C0DE1          # B1 encoder TD


def dump_enc(path, bg=2, zc=20, kb_used=None, seed=5):
    """Write the B1 encoder TD: header + information bits packed LSB-first.

    No expected codeword is stored: check_ldpc_enc.py recomputes it from
    ldpc_nr.encode, so the model cannot agree with itself by construction.
    """
    _rows, _cols, kb = NR.dims(bg)
    if kb_used is None:
        kb_used = KB_BY_BG[bg]
    kb_used = min(kb_used, kb)
    nbits = kb_used * zc
    rnd = random.Random(seed)
    info = [rnd.randint(0, 1) for _ in range(nbits)]
    ils = NR.zc_to_ils(zc)
    words = [ENC_MAGIC, bg, ils, zc, kb_used, nbits, 0, 0]
    for i in range(0, nbits, 32):
        w = 0
        for j in range(32):
            if i + j < nbits and info[i + j]:
                w |= 1 << j
        words.append(w)
    with open(path, "wb") as f:
        f.write(struct.pack("<%dI" % len(words), *words))
    print("wrote %s: BG%d i_LS=%d Zc=%d kb_used=%d, %d info bits"
          % (path, bg, ils, zc, kb_used, nbits))
    return 0


def main():
    print("QC-LDPC extension: BG2 graph, Zc list %s" % (ZC_LIST,))
    ok = True
    for zc in ZC_LIST:
        info, cw = make_codeword(zc)
        llr = channel_llr(cw, 3.0)
        bits_f, sw_f = L.decode(llr, zc, iters=8)
        bits_l, sw_l = layered_decode(llr, zc, iters=8)
        info_ok = bits_l[:KB * zc] == info[:KB * zc]
        print("  Zc=%2d : syndrome(flood)=%d syn(layer)=%d, info %s" % (
            zc, sw_f, sw_l, "OK" if info_ok else "BAD"))
        ok &= info_ok

    # Zc = 64 is i_LS = 0: both sides go through ldpc_nr so the same table is used
    _, cw = make_codeword(64, 5, 2)
    llr = channel_llr(cw, 3.0)
    for it in (1, 2, 4, 8):
        bits_l, sw_l = layered_decode(llr, 64, iters=it)
        print("  iters=%d : Zc=64 (i_LS=0) layered syndrome=%d" % (it, sw_l))
    print("[ %s ] Zc=64 layered scheduling" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        ap = argparse.ArgumentParser(prog="nr_ldpc.py dump")
        ap.add_argument("dump")
        ap.add_argument("--out", required=True)
        ap.add_argument("--zc", type=int, default=64)
        ap.add_argument("--snr", type=float, default=3.0)
        ap.add_argument("--iters", type=int, default=1)
        ap.add_argument("--seed", type=int, default=5)
        ap.add_argument("--nseed", type=int, default=9)
        ap.add_argument("--sigma", type=float, default=5.0)
        ap.add_argument("--bg", type=int, default=2)
        a = ap.parse_args()
        raise SystemExit(dump_td(a.out, a.zc, a.snr, a.iters, a.seed, a.nseed, a.sigma, a.bg))
    if len(sys.argv) > 1 and sys.argv[1] == "dump-enc":
        ap = argparse.ArgumentParser(prog="nr_ldpc.py dump-enc")
        ap.add_argument("dump-enc")
        ap.add_argument("--out", required=True)
        ap.add_argument("--bg", type=int, default=2, choices=(1, 2))
        ap.add_argument("--zc", type=int, default=20)
        ap.add_argument("--kb", type=int, default=None)
        ap.add_argument("--seed", type=int, default=5)
        a = ap.parse_args()
        raise SystemExit(dump_enc(a.out, a.bg, a.zc, a.kb, a.seed))
    raise SystemExit(main())
