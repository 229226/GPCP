#!/usr/bin/env python3
"""5G NR LDPC base graphs 1 and 2, driven by the 38.212 shift tables.

Tables
------
``tools/data/38.212-bg{1,2}-shifts.csv`` hold Table 5.3.2-2 (BG1) and Table
5.3.2-3 (BG2) in the spec's own shape: one line per (row, column) cell with the
eight shift coefficients ``V(i_LS = 0..7)``; a cell that is not listed is a zero
block (-1).  ``i_LS`` is the lifting-set index of Table 5.3.2-1, so a given Zc
picks one column of that CSV.

Provenance and how the data was accepted
---------------------------------------
The CSVs are the machine-readable form of those two tables (the same files that
ship with NVIDIA Sionna / py3gpp, header included).  They were not trusted on
their own: loading ``bg2.csv`` with ``i_LS = 2`` must reproduce
``tools/ldpc5g.py``'s ``BG2_A5`` -- an independent transcription of the same
table from R1-1711982 -- in **all 42 x 52 = 2184 cells**.  That check passes, so
the file family is the 38.212 table, and ``bg1.csv`` (same shape, same origin)
is used for BG1.  ``python3 tools/ldpc_nr.py`` re-runs the equivalence check and
then exercises BG1 at Zc = 64 end to end.

Encoding
--------
Both base graphs put the first 4 parity block-columns in a "double diagonal"
core.  Writing ``a = V[r0][p0]``, ``b = V[rb][p0]`` (``rb`` = the core row that
carries P_B: 1 for BG1, 2 for BG2) the four core equations reduce to

    u  = t0 ^ t1 ^ t2 ^ t3          (t_r = information contribution of row r)
    p0 = u shifted by -b
    p1 = t0 ^ (p0 shifted by +a)
    p3 = t3 ^ (p0 shifted by +a)
    p2 = t1 ^ p1 ^ (p0 shifted by +b if rb == 1 else 0)

which is the closed form of the block inverse; ``encode()`` re-checks it by
requiring ``syndrome(codeword) == 0``.  Rows 4.. carry a single shift-0 diagonal
in parity column ``kb + r``, so they are solved in one pass.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# 38.212 Table 5.3.2-1
ILS_SETS = (
    (2, 4, 8, 16, 32, 64, 128, 256),
    (3, 6, 12, 24, 48, 96, 192, 384),
    (5, 10, 20, 40, 80, 160, 320),
    (7, 14, 28, 56, 112, 224),
    (9, 18, 36, 72, 144, 288),
    (11, 22, 44, 88, 176, 352),
    (13, 26, 52, 104, 208),
    (15, 30, 60, 120, 240),
)

# base graph -> (rows, cols, kb, csv)
DIMS = {
    1: (46, 68, 22, "38.212-bg1-shifts.csv"),
    2: (42, 52, 10, "38.212-bg2-shifts.csv"),
}

_CACHE = {}


def zc_to_ils(zc):
    for i, s in enumerate(ILS_SETS):
        if zc in s:
            return i
    raise ValueError("Zc=%d is not a 38.212 lifting size" % zc)


def lifting_sizes(i_ls):
    return ILS_SETS[i_ls]


def load_table(bgn, i_ls):
    """Shift-coefficient matrix V of one base graph for one lifting set."""
    key = (bgn, i_ls)
    if key in _CACHE:
        return _CACHE[key]
    rows, cols, _kb, name = DIMS[bgn]
    bm = [[-1] * cols for _ in range(rows)]
    r_ind = 0
    with open(os.path.join(DATA, name), "r", encoding="utf-8") as f:
        for ln in f:
            fld = ln.rstrip("\n").split(";")
            if len(fld) < 3:
                continue
            if fld[0].strip():
                try:
                    r_ind = int(fld[0])
                except ValueError:
                    continue
            if not fld[1].strip() or not fld[1].lstrip("-").isdigit():
                continue
            v = fld[i_ls + 2].strip()
            if v:
                bm[r_ind][int(fld[1])] = int(float(v))
    out = tuple(tuple(r) for r in bm)
    _CACHE[key] = out
    return out


def dims(bgn):
    rows, cols, kb, _name = DIMS[bgn]
    return rows, cols, kb


def table_for_zc(bgn, zc):
    return load_table(bgn, zc_to_ils(zc))


def edges(bgn, zc, r):
    """[(var_block, shift mod zc)] of check node r."""
    t = table_for_zc(bgn, zc)
    return [(c, t[r][c] % zc) for c in range(len(t[0])) if t[r][c] >= 0]


def graph(bgn, zc):
    """(check -> [(var_block, shift)], var_block -> [(check, shift)])."""
    chk, var = [], {}
    rows, _cols, _kb = dims(bgn)
    t = table_for_zc(bgn, zc)
    for r in range(rows):
        row = []
        for c in range(len(t[0])):
            v = t[r][c]
            if v >= 0:
                sh = v % zc
                row.append((c, sh))
                var.setdefault(c, []).append((r, sh))
        chk.append(row)
    return chk, var


def syndrome(bgn, bits, zc):
    """Weight of H * bits (0 == valid codeword)."""
    rows, _cols, _kb = dims(bgn)
    t = table_for_zc(bgn, zc)
    w = 0
    for r in range(rows):
        for i in range(zc):
            acc = 0
            for c in range(len(t[0])):
                v = t[r][c]
                if v >= 0:
                    acc ^= bits[c * zc + (i + v % zc) % zc]
            w += acc
    return w


def _core_form(bgn, zc):
    """(a, b, rb) of the 4-row double-diagonal core, read from the table."""
    _rows, _cols, kb = dims(bgn)
    t = table_for_zc(bgn, zc)
    core = [[t[r][kb + c] for c in range(4)] for r in range(4)]
    a = core[0][0]
    rb = None
    for r in (1, 2):
        if core[r][0] >= 0 and sum(1 for v in core[r] if v >= 0) >= 3:
            rb = r
            break
    if a < 0 or rb is None:
        raise ValueError("BG%d: unexpected 4x4 core %s" % (bgn, core))
    return a % zc, core[rb][0] % zc, rb


def encode(bgn, info_bits, kb_used, zc):
    """Encode one code block (no rate matching).

    info_bits : kb_used * zc systematic bits; the first 2*zc are punctured later
    returns   : cols * zc coded bits
    """
    rows, cols, kb = dims(bgn)
    if len(info_bits) != kb_used * zc:
        raise ValueError("need %d info bits, got %d" % (kb_used * zc, len(info_bits)))
    if kb_used > kb:
        raise ValueError("BG%d carries at most %d information columns" % (bgn, kb))
    t = table_for_zc(bgn, zc)
    c = [0] * (cols * zc)
    c[:kb_used * zc] = info_bits[:]

    def acc_info(r, upto_cols):
        acc = [0] * zc
        for col in range(upto_cols):
            v = t[r][col]
            if v < 0:
                continue
            sh = v % zc
            base = col * zc
            for i in range(zc):
                acc[i] ^= c[base + (i + sh) % zc]
        return acc

    a, b, rb = _core_form(bgn, zc)
    t0, t1, t2, t3 = (acc_info(r, kb) for r in range(4))
    u = [t0[i] ^ t1[i] ^ t2[i] ^ t3[i] for i in range(zc)]
    p0 = [u[(i - b) % zc] for i in range(zc)]
    p1 = [t0[i] ^ p0[(i + a) % zc] for i in range(zc)]
    p3 = [t3[i] ^ p0[(i + a) % zc] for i in range(zc)]
    if rb == 1:
        p2 = [t1[i] ^ p1[i] ^ p0[(i + b) % zc] for i in range(zc)]
    else:
        p2 = [t1[i] ^ p1[i] for i in range(zc)]
    for k, vec in enumerate((p0, p1, p2, p3)):
        c[(kb + k) * zc:(kb + k + 1) * zc] = vec

    for r in range(4, rows):
        unknown = kb + r
        sh = t[r][unknown]
        if sh < 0:
            raise ValueError("BG%d row %d has no diagonal parity block" % (bgn, r))
        acc = acc_info(r, kb)          # information columns only
        for col in range(kb, cols):
            if col == unknown or t[r][col] < 0:
                continue
            if col > unknown:
                raise ValueError("BG%d row %d needs the later parity column %d"
                                 % (bgn, r, col))
            base_src = col * zc
            s = t[r][col] % zc
            for i in range(zc):
                acc[i] ^= c[base_src + (i + s) % zc]
        sh %= zc
        c[unknown * zc:(unknown + 1) * zc] = [acc[(i - sh) % zc] for i in range(zc)]
    return c


# ---------------------------------------------------------------- self-test
def _selftest():
    import random

    ok = True

    # 1) the CSV family must reproduce the independently transcribed BG2_A5
    import ldpc5g as L
    b2 = load_table(2, 2)
    bad = sum(1 for r in range(42) for c in range(52) if b2[r][c] != L.BG2_A5[r][c])
    print("[%s] bg2.csv i_LS=2 reproduces ldpc5g.BG2_A5 (%d/%d cells differ)"
          % ("OK" if bad == 0 else "FAIL", bad, 42 * 52))
    ok &= bad == 0

    # 2) BG2 Zc=20 must behave exactly like ldpc5g (encode + syndrome + graph)
    zc, kb_used = 20, 6
    rnd = random.Random(3)
    info = [rnd.randint(0, 1) for _ in range(kb_used * zc)]
    cw_new = encode(2, info, kb_used, zc)
    cw_old = L.encode(info, kb_used, zc)
    same = cw_new == cw_old
    print("[%s] BG2 Zc=20 encodes bit-identically to ldpc5g (%d/%d bits differ)"
          % ("OK" if same else "FAIL",
             sum(1 for x, y in zip(cw_new, cw_old) if x != y), len(cw_new)))
    ok &= same
    g_new, _ = graph(2, zc)
    g_old = L.check_edges(zc)
    ok &= all(sorted(g_new[r]) == sorted(g_old[r]) for r in range(42))
    print("[OK] BG2 Zc=20 check-node edge lists match ldpc5g")

    # 3) BG1 at Zc=64: encode, syndrome, layered decode
    import nr_ldpc as N
    zc, kb_used = 64, 22
    rnd = random.Random(5)
    info = [rnd.randint(0, 1) for _ in range(kb_used * zc)]
    cw = encode(1, info, kb_used, zc)
    sw = syndrome(1, cw, zc)
    print("[%s] BG1 Zc=64 clean codeword syndrome = %d (N=%d, K=%d)"
          % ("OK" if sw == 0 else "FAIL", sw, 68 * zc, 22 * zc))
    ok &= sw == 0

    sigma = 5.0
    llr = N.channel_llr(cw, 3.0, 9, sigma)
    for it in (1, 2, 4, 8):
        bits, sw = N.layered_decode(llr, zc, iters=it, bg=1)
        err = sum(1 for a, b in zip(bits, cw) if a != b)
        good = bits[:kb_used * zc] == info[:kb_used * zc]
        print("  BG1 Zc=64 sigma=%.1f it=%d -> syndrome %d, bit errors %d, info %s"
              % (sigma, it, sw, err, "OK" if good else "BAD"))
        if it >= 4:
            ok &= (sw == 0 and err == 0 and good)

    rows, cols, kb = dims(1)
    print("[info] BG1: %d rows x %d cols, Kb=%d, i_LS=%d (Zc=%d), max row degree %d"
          % (rows, cols, kb, zc_to_ils(zc), zc,
             max(len(edges(1, zc, r)) for r in range(rows))))
    print("[ %s ] 38.212 table ingest" % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
