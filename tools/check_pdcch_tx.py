#!/usr/bin/env python3
"""Check the PDCCH transmit chain (sw/soc/pdcch_tx_bm.c), gate run-pdcch-tx.

The golden model is tools/nr_pdcch_tx.py, whose every stage is cross-checked in
its own self-test against the receiver-side model (nr_pdcch.transmit /
polar_encode_k / rate_match_pbch / nr_pdcch_reg).  Nothing here compares the C
against itself.

Result-area contract (32-bit words, see pdcch_tx_bm.c header):
    [0] marker [1] nvar [2] errors [3] cycles [4] array runs
    [5] dci [6] n_id [7] n_reg [8] L [9] n_shift [10] 0x600D [11] crc24c
    blocks at 16 + v*934:
      info[2] u[16] x[16] e[14] e_sc[14] sym[432] reg[432] meta[8]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_pdcch_tx as TX                                # noqa: E402
from checklib import parse_dump, words32                # noqa: E402

INFO_W = (TX.K_INFO + 31) // 32         # 2
NW = TX.NPOL // 32                      # 16
E_W = (TX.E_BITS + 31) // 32            # 14
SYM_W = 2 * (TX.E_BITS // 2)            # 432
BLK = INFO_W + NW + NW + E_W + E_W + SYM_W + SYM_W + 8
HDR = 16


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
    td = TX.read_td(td_path)
    if td["n_reg"] != TX.N_REG or td["L"] != TX.L_BUNDLE:
        sys.exit("[FAIL] TD geometry N_REG=%d L=%d, model expects %d/%d"
                 % (td["n_reg"], td["L"], TX.N_REG, TX.L_BUNDLE))
    dci_td = TX.dci_from_word(td["dci"])
    if dci_td != TX.PDCCH.pack_dci(TX.FIELDS):
        sys.exit("[FAIL] TD DCI does not match the model's DEFAULT_FIELDS")

    st = parse_dump(dump_path)
    if st.get("timeout", 1):
        sys.exit("[FAIL] simulation timed out")
    if st.get("exit_code") != 0x600D:
        sys.exit("[FAIL] exit_code 0x%x" % st.get("exit_code", 0))
    v = words32(st)
    need = HDR + len(td["variants"]) * BLK
    if len(v) < need:
        sys.exit("[FAIL] short dump: %d words, need %d" % (len(v), need))

    errs = []

    def eq(name, got, want):
        if got != want:
            errs.append("%s: got %r want %r" % (name, got, want))

    eq("marker", v[0], TX.MAGIC)
    eq("nvar", v[1], len(td["variants"]))
    eq("dci", v[5], td["dci"])
    eq("n_id", v[6], td["n_id"])
    eq("n_reg", v[7], td["n_reg"])
    eq("L", v[8], td["L"])
    eq("n_shift", v[9], td["n_shift"])
    eq("epilogue", v[10], 0x600D)
    eq("crc24c", v[11], TX.crc_word())
    eq("internal errors", v[2], 0)
    eq("array runs", v[4], len(td["variants"]) * 9)

    for idx, rnti in enumerate(td["variants"]):
        r = TX.pdcch_tx(TX.FIELDS, rnti)
        base = HDR + idx * BLK
        o_info = base
        o_u = o_info + INFO_W
        o_x = o_u + NW
        o_e = o_x + NW
        o_esc = o_e + E_W
        o_sym = o_esc + E_W
        o_reg = o_sym + SYM_W
        o_meta = o_reg + SYM_W
        tag = "rnti=0x%04x" % rnti

        for name, off, want, n in (
                ("info", o_info, r["info"], TX.K_INFO),
                ("u", o_u, r["u"], TX.NPOL),
                ("x", o_x, r["x"], TX.NPOL),
                ("e", o_e, r["e"], TX.E_BITS),
                ("e_sc", o_esc, r["e_sc"], TX.E_BITS)):
            d = first_diff(unpack(v, off, n), want)
            if d:
                errs.append("%s %s: first %r" % (tag, name, d))

        got_sym = [(sx(v[o_sym + 2 * k]), sx(v[o_sym + 2 * k + 1]))
                   for k in range(TX.N_REG)]
        want_sym = [(int(c.real), int(c.imag)) for c in r["syms"]]
        d = first_diff(got_sym, want_sym)
        if d:
            errs.append("%s symbols: %d differ, first %r"
                        % (tag, sum(1 for a, b in zip(got_sym, want_sym)
                                    if a != b), d))

        got_reg = [(sx(v[o_reg + 2 * k]), sx(v[o_reg + 2 * k + 1]))
                   for k in range(TX.N_REG)]
        want_reg = [(int(c.real), int(c.imag)) for c in r["reg"]]
        d = first_diff(got_reg, want_reg)
        if d:
            errs.append("%s REG mapping: %d differ, first %r"
                        % (tag, sum(1 for a, b in zip(got_reg, want_reg)
                                    if a != b), d))
        if sorted(got_reg) != sorted(got_sym):
            errs.append("%s REG output is not a permutation of the symbols" % tag)

        eq("%s meta rnti" % tag, v[o_meta + 0], rnti)
        eq("%s meta info_used" % tag, v[o_meta + 1], TX.K_INFO)
        eq("%s meta nbundle" % tag, v[o_meta + 2], TX.N_REG // TX.L_BUNDLE)
        eq("%s meta n_shift" % tag, v[o_meta + 3], TX.N_SHIFT)

    if errs:
        print("[FAIL] PDCCH transmit: %d problem(s)" % len(errs))
        for e in errs[:12]:
            print("   -", e)
        return 1

    print("[ OK ] PDCCH transmit chain: DCI -> CRC24C -> RNTI -> polar (CGRA, "
          "9 stages) -> rate match E=%d -> scramble -> QPSK -> REG/CCE, "
          "%d RNTI variants, 0 differences, %d array runs, %d cycles"
          % (TX.E_BITS, len(td["variants"]), v[4], v[3]))
    return 0



if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: check_pdcch_tx.py <dump> <td>")
    sys.exit(main(sys.argv[1], sys.argv[2]))
