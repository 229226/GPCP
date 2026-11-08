#!/usr/bin/env python3
"""Check the Zc=64 paged layered min-sum pass (sw/soc/pdsch_zc64_bm.c).

The same script serves both base graphs; the TD says which one the binary was
built for.

Two independent claims are verified:

  1. paging -- the check-node outputs of every (check node, 16-value page) pair
     match a min-sum over that page's lifted values (the +32767 sentinel padding
     for edges beyond the row degree can never win against real messages, so it
     is left out here);
  2. the layered iteration -- every variable total the hardware leaves behind is
     bit-identical to a row-by-row in-place min-sum replay, the hard decisions
     match tools/nr_ldpc.py's layered decoder, the syndrome is zero and the
     information bits come back.

The channel LLRs come from the same TD the benchmark loads, so this script does
not re-derive the stimulus -- it only re-derives the expected numbers.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ldpc_nr as NR
import nr_ldpc as N

PES = 16
CNRES_OFF = 16


def load_td(path):
    data = open(path, "rb").read()
    w = struct.unpack("<%dI" % (len(data) // 4), data)
    if w[0] != 0x5A433634:
        sys.exit("bad TD magic 0x%08x" % w[0])
    bg, zc, rows, cols, iters, nllr, ninfo = w[1], w[2], w[3], w[4], w[5], w[6], w[7]
    s32 = lambda x: x - (1 << 32) if x & 0x80000000 else x
    llr = [s32(x) for x in w[8:8 + nllr]]
    info = [int(x) for x in w[8 + nllr:8 + nllr + ninfo]]
    return bg, zc, rows, cols, iters, llr, info


def layered_model(bg, llr, zc, iters, scale=0.75):
    """Row-by-row in-place min-sum replay.

    Returns (total, cn) where cn[(row, page)] = (min1, min2, sign) of the CN
    *inputs* used by the last iteration.  Expecting the paged check-node outputs
    to be a min-sum of the channel LLRs would only be right for the first row:
    in a layered decoder every later row already sees the previous rows' updates.
    """
    chk, _ = NR.graph(bg, zc)
    chan = list(llr)
    chan[:2 * zc] = [0] * (2 * zc)
    total = list(chan)
    mcv = {(r, blk): [0] * zc
           for r, row in enumerate(chk) for (blk, sh) in row}
    cn = {}
    for _ in range(iters):
        for r, row in enumerate(chk):
            v2c = {}
            for (blk, sh) in row:
                v2c[(blk, sh)] = [total[blk * zc + (i + sh) % zc] - mcv[(r, blk)][i]
                                  for i in range(zc)]
            for j in range(zc // PES):
                m1, m2, sg = [], [], []
                for i in range(j * PES, (j + 1) * PES):
                    col = [v2c[e][i] for e in row]
                    ab = sorted(abs(v) for v in col)
                    m1.append(ab[0])
                    m2.append(ab[1])
                    sgn = 0
                    for v in col:
                        if v < 0:
                            sgn ^= 1
                    sg.append(-1 if sgn else 0)
                cn[(r, j)] = (m1, m2, sg)
            new = {}
            for k, e in enumerate(row):
                out = [0] * zc
                for i in range(zc):
                    vals = [v2c[e2][i] for j2, e2 in enumerate(row) if j2 != k]
                    mag = min(abs(v) for v in vals) if vals else 0
                    sgn = 0
                    for v in vals:
                        if v < 0:
                            sgn ^= 1
                    val = int(mag * scale)
                    out[i] = -val if sgn else val
                new[e] = out
            for e, msg in new.items():
                blk, sh = e
                old = mcv[(r, blk)]
                for i in range(zc):
                    total[blk * zc + (i + sh) % zc] += msg[i] - old[i]
                mcv[(r, blk)] = msg
    return total, cn


def parse_dump(path):
    vals = []
    for line in open(path):
        p = line.split()
        if len(p) >= 3 and p[0] == "word" and p[2].startswith("0x"):
            w = int(p[2], 16)
            vals.append(w & 0xFFFFFFFF)
            vals.append((w >> 32) & 0xFFFFFFFF)
    return vals


def main(dump_path, td_path):
    vals = parse_dump(dump_path)
    bg, zc, rows, cols, iters, llr, info = load_td(td_path)
    pages = zc // PES

    ncn = rows * pages * 3 * PES
    ntot = cols * zc
    if len(vals) < CNRES_OFF + ncn + ntot:
        sys.exit("[FAIL] short dump (%d words, need %d)"
                 % (len(vals), CNRES_OFF + ncn + ntot))
    if vals[0] != 0xCAFEBABE:
        sys.exit("[FAIL] marker 0x%08x" % vals[0])
    if vals[7] != 0:
        sys.exit("[FAIL] benchmark reported %d bus/timeout errors" % vals[7])

    want, want_cn = layered_model(bg, llr, zc, iters)

    # ---- claim 1: paged check-node outputs -------------------------------
    bad = 0
    for r in range(rows):
        for j in range(pages):
            e = want_cn[(r, j)]
            for m in range(3):
                for i in range(PES):
                    got = vals[CNRES_OFF + ((r * pages + j) * 3 + m) * PES + i]
                    exp = e[m][i] & 0xFFFFFFFF
                    if got != exp:
                        bad += 1
                        if bad <= 5:
                            print("[diff] row %d page %d field %d lifted %d: "
                                  "got 0x%08x want 0x%08x"
                                  % (r, j, m, j * PES + i, got, exp))

    # ---- claim 2: the layered iteration ----------------------------------
    s32 = lambda x: x - (1 << 32) if x & 0x80000000 else x
    base = CNRES_OFF + ncn
    tot = [[s32(vals[base + v * zc + i]) for i in range(zc)] for v in range(cols)]
    tbad = 0
    for v in range(cols):
        for i in range(zc):
            if tot[v][i] != want[v * zc + i]:
                tbad += 1
                if tbad <= 5:
                    print("[diff] total[%d][%d]: got %d want %d"
                          % (v, i, tot[v][i], want[v * zc + i]))

    bits = [1 if tot[v][i] < 0 else 0 for v in range(cols) for i in range(zc)]
    exp_bits, _ = N.layered_decode(llr, zc, iters=iters, bg=bg)
    bbad = sum(1 for a, b in zip(bits, exp_bits) if a != b)
    syn = NR.syndrome(bg, bits, zc)
    info_bad = sum(1 for i in range(len(info)) if bits[i] != info[i])

    print("[info] BG%d zc %d, %d check nodes x %d pages, %d array runs, %d DMA, "
          "%d cycles, iters %d"
          % (vals[1], vals[2], vals[3], pages, vals[4], vals[5], vals[6], vals[10]))
    if bbad:
        print("[diff] %d/%d hard decisions differ from nr_ldpc.layered_decode"
              % (bbad, len(bits)))
    if vals[8] != 0:
        print("[diff] bench reported %d syndrome bits set" % vals[8])
    if vals[9] != 0:
        print("[diff] bench reported %d information-bit mismatches" % vals[9])
    if syn:
        print("[diff] recomputed syndrome has %d words set" % syn)
    if info_bad:
        print("[diff] %d/%d information bits wrong" % (info_bad, len(info)))

    ok = not (bad or tbad or bbad or syn or info_bad or vals[8] or vals[9])
    if not ok:
        print("[FAIL] BG%d Zc=64 paged layered decode: %d CN, %d total, %d bit diffs"
              % (bg, bad, tbad, bbad))
        return 1
    print("[ OK ] BG%d Zc=%d block, edge messages paged %d x %d: min1/min2/sign "
          "bit-exact on every page, %d layered iteration(s) matches the model "
          "value for value, zero syndrome, %d/%d information bits"
          % (bg, zc, pages, PES, iters, len(info), len(info)))
    return 0


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "pdsch_zc64_dump.txt"
    t = sys.argv[2] if len(sys.argv) > 2 else "build/ldpc/pdsch_zc64.td"
    sys.exit(main(d, t))
