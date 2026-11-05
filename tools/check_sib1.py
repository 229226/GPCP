#!/usr/bin/env python3
"""Acceptance check for the SIB1 benchmark (Type0-PDCCH + PDSCH).

Parses the Verilator --dump output of sw/soc/link_e2e_bm.c compiled with
-DSIB1 (sw/soc/sib1_bm.c equivalent) and verifies:

  * PDCCH: SI-RNTI (0xFFFF) blind detection succeeds, CRC24C ok, and the
    32-bit DCI equals the golden DCI (r/rb/mcs..., SI indicator set);
  * PDSCH: 1040 LDPC hard decisions match the golden model bit-for-bit,
    CRC24A passes, the 80-bit transport block equals the golden SIB1 payload;
  * the decoded SIB1 fields (cell identity, TAC, PRACH config, SI scheduling,
    CORESET#0) match the transmitted configuration;
  * k0 = rv * USABLE/4 (the DCI rv genuinely drives the PDSCH circular buffer);
  * the 38.211 7.4.1.1 DMRS c_init values match the model.

Result layout (sw/soc/link_e2e_bm.c):
  [0] 0xCAFEBABE [1] errors [2] cycles [3] runs [4] matched RNTI
  [5] PDCCH CRC ok [6] 0x600D [7] rv
  [8..39] 32-bit DCI   [40] mcs [41] ndi [42] harq_pid
  [43] rb_start [44] rb_len [45] tpc [46] PDSCH CRC ok
  [48..1087] 1040 LDPC hard decisions
  [1093]/[1094] DMRS c_init sym0/sym1   [1095] k0
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_sib1 as S

MARKER = 0xCAFEBABE
EPILOGUE = 0x600D
NHARD = 1040


def parse_dump(path):
    vals = []
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "word" and parts[2].startswith("0x"):
                w = int(parts[2], 16)
                vals.append(w & 0xFFFFFFFF)
                vals.append((w >> 32) & 0xFFFFFFFF)
    return vals


def main(path, snr, seed, rv):
    vals = parse_dump(path)
    if len(vals) < 1096:
        print("  dump too short: %d words (need >= 1096)" % len(vals))
        return 1

    g = S.build_sib1(None, snr, S.PDCCH_SNR, seed, rv)
    rx = S.receive_sib1(g["pdcch_td"], g["pdsch_td"], rv)
    want = g["cfg"]

    ok = True
    def chk(cond, msg):
        nonlocal ok
        if not cond:
            print("  " + msg)
            ok = False

    chk(vals[0] == MARKER, "epilogue missing: 0x%08x" % vals[0])
    chk(vals[6] == EPILOGUE, "program did not finish: 0x%08x" % vals[6])
    chk(vals[1] == 0, "program reported %d internal errors" % vals[1])
    chk(vals[4] == S.SI_RNTI,
        "matched RNTI 0x%04x != SI-RNTI 0x%04x" % (vals[4], S.SI_RNTI))
    chk(vals[5] == 1, "PDCCH CRC24C failed")
    chk(vals[7] == rv, "DCI rv = %d, want %d" % (vals[7], rv))

    exp_dci = S.PDCCH.pack_dci(g["dci"])
    chk(vals[8:40] == exp_dci,
        "DCI bits differ: hw=%s model=%s" % (vals[8:40], exp_dci))
    chk(vals[40] == g["dci"]["mcs"], "DCI mcs = %d, want %d" % (vals[40], g["dci"]["mcs"]))
    chk(vals[43] == g["dci"]["rb_start"],
        "DCI rb_start = %d, want %d" % (vals[43], g["dci"]["rb_start"]))
    chk(vals[44] == g["dci"]["rb_len"],
        "DCI rb_len = %d, want %d" % (vals[44], g["dci"]["rb_len"]))

    chk(vals[46] == 1, "PDSCH CRC24A failed")
    hard = vals[48:48 + NHARD]
    bad = [i for i in range(NHARD) if hard[i] != rx["hard"][i]]
    chk(not bad, "LDPC hard decisions: %d/%d differ (first %s)"
        % (len(bad), NHARD, bad[:6]))

    tb = hard[:S.M.A_TB]
    tbbad = [i for i, (a, b) in enumerate(zip(tb, g["tb"])) if a != b]
    chk(not tbbad, "transport block: %d/%d bits differ (first %s)"
        % (len(tbbad), S.M.A_TB, tbbad[:8]))

    # Decode the SIB1 fields from the hardware TB and compare to the config.
    got = S.unpack_sib1(tb) if not tbbad else None
    if got is None:
        print("  cannot decode SIB1 fields (TB differs)")
        ok = False
    else:
        badf = [k for k, _ in S.SIB1_FIELDS if got[k] != want[k]]
        chk(not badf, "SIB1 fields differ: %s" % badf)
        print("  probe: SIB1 cell_identity=0x%09x tac=%d prach_config_index=%d "
              "msg1_fdm=%d msg1_freq_start=%d zero_corr_zone=%d scs=%d"
              % (got["cell_identity"], got["tac"], got["prach_config_index"],
                 got["msg1_fdm"], got["msg1_freq_start"], got["zero_corr_zone"],
                 got["scs"]))

    chk(vals[1095] == g["k0"],
        "k0 = %d, want %d (rv=%d)" % (vals[1095], g["k0"], rv))
    chk(vals[1093] == S.F.dmrs_cinit(0) and vals[1094] == S.F.dmrs_cinit(1),
        "DMRS c_init hw=(0x%x,0x%x) model=(0x%x,0x%x)"
        % (vals[1093], vals[1094], S.F.dmrs_cinit(0), S.F.dmrs_cinit(1)))

    print("  probe: %d errors, %d cycles, %d array runs" % (vals[1], vals[2], vals[3]))
    print("  probe: PDCCH RNTI=0x%04x crc=%d, DCI rv=%d, PDSCH crc=%d, k0=%d"
          % (vals[4], vals[5], vals[7], vals[46], vals[1095]))

    if ok:
        print("[ OK ] SIB1: Type0-PDCCH (SI-RNTI 0x%04x) -> PDSCH -> "
              "1040/1040 LDPC -> CRC24A -> 80-bit SIB1 fields" % S.SI_RNTI)
        return 0
    print("[FAIL] SIB1 acceptance")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--snr", type=float, default=S.SIB1_SNR)
    ap.add_argument("--seed", type=int, default=S.DEFAULT_SEED)
    ap.add_argument("--rv", type=int, default=0)
    a = ap.parse_args()
    sys.exit(main(a.dump, a.snr, a.seed, a.rv))
