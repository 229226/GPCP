#!/usr/bin/env python3
"""Check the downlink modulation chain (sw/soc/dl_mod_bm.c), gate run-dl-mod.

The golden model is tools/nr_dl_mod.py, whose every stage is cross-checked in
its own self-test against a different implementation that the hardware already
validates (ldpc_nr.encode, pdsch_full_plan.interleave, pdsch_plan.qpsk_map,
nr_pdsch_amc.deinterleave).  Nothing here compares the C against itself.

Result-area contract (32-bit words, see dl_mod_bm.c header):
    [0] marker [1] nvar [2] errors [3] cycles [4] array runs
    [5] a_tb   [6] K    [7] 0x600D
    blocks at 8 + v*861:
      cw[33] usable[31] e[23] e_sc[23] f[23] syms[720] meta[8]
    meta = {qm, rv, k0, nsym, crc_ok, syndrome, filler_n, 0}
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_dl_mod as NR                                  # noqa: E402
from checklib import parse_dump, words32                # noqa: E402

CW_W = (NR.NCODED + 31) // 32
US_W = (NR.USABLE + 31) // 32
E_W = (NR.E_BITS + 31) // 32
SYM_W = 2 * (NR.E_BITS // 2)
BLK = CW_W + US_W + 3 * E_W + SYM_W + 8
HDR = 8


def sx(v):
    return v - 0x100000000 if v >= 0x80000000 else v


def unpack(v, off, n):
    return [(v[off + i // 32] >> (i % 32)) & 1 for i in range(n)]


def first_diff(got, want, limit=4):
    out = []
    for i, (a, b) in enumerate(zip(got, want)):
        if a != b:
            out.append((i, a, b))
            if len(out) >= limit:
                break
    return out


def main(dump_path, td_path):
    tb, variants, tb_words = NR.read_td(td_path)
    if tb_words != (NR.A_TB + 31) // 32:
        sys.exit("[FAIL] TD tb_words %d, want %d"
                 % (tb_words, (NR.A_TB + 31) // 32))

    st = parse_dump(dump_path)
    if st.get("timeout", 1):
        sys.exit("[FAIL] simulation timed out")
    if st.get("exit_code") != 0x600D:
        sys.exit("[FAIL] exit_code 0x%x" % st.get("exit_code", 0))
    v = words32(st)
    need = HDR + len(variants) * BLK
    if len(v) < need:
        sys.exit("[FAIL] short dump: %d words, need %d" % (len(v), need))

    errs = []

    def eq(name, got, want):
        if got != want:
            errs.append("%s: got %r want %r" % (name, got, want))

    eq("marker", v[0], NR.MAGIC)
    eq("nvar", v[1], len(variants))
    eq("a_tb", v[5], NR.A_TB)
    eq("K", v[6], NR.K_BITS)
    eq("epilogue", v[7], 0x600D)
    eq("array runs", v[4], 0)
    eq("internal errors", v[2], 0)

    summary = []
    for idx, (qm, rv, rnti) in enumerate(variants):
        r = NR.dl_mod(tb, qm, rv, rnti)
        base = HDR + idx * BLK
        o_cw = base
        o_us = o_cw + CW_W
        o_e = o_us + US_W
        o_esc = o_e + E_W
        o_f = o_esc + E_W
        o_sym = o_f + E_W
        o_meta = o_sym + SYM_W
        tag = "v%d(Qm=%d,rv=%d,rnti=0x%04x)" % (idx, qm, rv, rnti)

        d = first_diff(unpack(v, o_cw, NR.NCODED), r["cw"])
        if d:
            errs.append("%s cw: %d+ diffs, first %r" % (tag, len(d), d))
        d = first_diff(unpack(v, o_us, NR.USABLE), r["usable"])
        if d:
            errs.append("%s usable: first %r" % (tag, d))
        d = first_diff(unpack(v, o_e, NR.E_BITS), r["e"])
        if d:
            errs.append("%s e: first %r" % (tag, d))
        d = first_diff(unpack(v, o_esc, NR.E_BITS), r["e_sc"])
        if d:
            errs.append("%s e_sc: first %r" % (tag, d))
        d = first_diff(unpack(v, o_f, NR.E_BITS), r["f"])
        if d:
            errs.append("%s f: first %r" % (tag, d))

        got_y = []
        for k in range(r["nsym"]):
            got_y.append((sx(v[o_sym + 2 * k]), sx(v[o_sym + 2 * k + 1])))
        d = first_diff(got_y, r["y"])
        if d:
            errs.append("%s symbols: %d differ, first %r"
                        % (tag, sum(1 for a, b in zip(got_y, r["y"]) if a != b), d))

        eq("%s meta qm" % tag, v[o_meta + 0], qm)
        eq("%s meta rv" % tag, v[o_meta + 1], rv)
        eq("%s meta k0" % tag, v[o_meta + 2], r["k0"])
        eq("%s meta nsym" % tag, v[o_meta + 3], r["nsym"])
        eq("%s meta crc_ok" % tag, v[o_meta + 4], 1)
        eq("%s meta syndrome" % tag, v[o_meta + 5], 0)
        eq("%s meta filler_n" % tag, v[o_meta + 6], NR.FILLER_N)
        # unused symbol slots must stay marked, not silently zero
        if (2 * r["nsym"] < SYM_W and
                v[o_sym + 2 * r["nsym"]] != 0xDEADBEEF):
            errs.append("%s unused symbol tail is not marked" % tag)

        summary.append("%s %d syms" % (tag, r["nsym"]))

    if errs:
        print("[FAIL] downlink modulation: %d problem(s)" % len(errs))
        for e in errs[:12]:
            print("   -", e)
        return 1

    nsym = sum(NR.E_BITS // qm for qm, _rv, _r in variants)
    print("[ OK ] Downlink modulation chain: TB -> CRC24A -> LDPC -> rate "
          "match -> scramble -> interleave -> QAM, %d variants / %d symbols, "
          "0 differences, %d cycles"
          % (len(variants), nsym, v[3]))
    print("        " + "; ".join(summary))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: check_dl_mod.py <dump> <td>")
    sys.exit(main(sys.argv[1], sys.argv[2]))
