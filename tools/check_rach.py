#!/usr/bin/env python3
"""Acceptance check for run-rach (the whole 4-step random-access process on the SoC).

The benchmark is a *process*, so the gate is a process gate: every value the
hardware hands to the next step is re-derived from tools/nr_rach.py and compared
element by element.

  Msg1  all L_RA=139 cyclic-shift correlations bit-exact, peak -> preamble + TA
  Msg2  RA-RNTI built from the occasion -> DCI 32/32 bits + CRC24C, rv -> k0,
        1040/1040 LDPC hard decisions, CRC24A, the 10 RAR PDU bytes, and the
        RAPID / TA command / TC-RNTI / 27-bit UL grant inside them
  Msg3  FFT window = the Msg1-measured TA, descrambled with the TC-RNTI out of
        the decoded RAR, 1040/1040 hard decisions, CRC24A, the CCCH SDU bytes
  Msg4  DCI blind-detected with that same TC-RNTI, 1040/1040 hard decisions,
        CRC24A, the contention-resolution identity compared against the SDU the
        program itself put into Msg3, contention resolved, C-RNTI = TC-RNTI

Nothing here is a "decision is right" check: a wrong RNTI, a wrong rv, a wrong
TA or a wrong k0 shows up as mismatched values, not as a lucky pass.  The golden
model's own gates are asserted first, so a failing hardware run cannot be
confused with a broken stimulus.
"""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cgra
import nr_link as LINK
import nr_mac as MAC
import nr_pdcch as PDCCH
import nr_pusch as PUSCH
import nr_rach as R
import nr_sib1 as S1
import pdsch_full_plan as F

MARKER = 0xCAFEBABE
EPILOGUE = 0x600D
NHARD = 1040

# sw/soc/rach_bm.c result area (its header comment is the same table)
R_MSG1, R_C0 = 8, 20
R_MS2, R_MS2_H = 300, 370
R_MS3, R_MS3_H = 1420, 1460
R_MS4, R_MS4_H = 2510, 2580
NRES = R_MS4_H + NHARD


def parse_dump(path):
    vals = []
    with open(path) as f:
        for line in f:
            p = line.split()
            if len(p) >= 3 and p[0] == "word" and p[2].startswith("0x"):
                w = int(p[2], 16)
                vals.append(w & 0xFFFFFFFF)
                vals.append((w >> 32) & 0xFFFFFFFF)
    return vals


def s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


class Report:
    def __init__(self):
        self.ok = True
        self.nfail = 0

    def eq(self, name, got, want):
        if got != want:
            print("  %s: hw=%s expected %s" % (name, got, want))
            self.ok = False
            self.nfail += 1
        return got == want

    def bits(self, name, base, want):
        """Compare one value per published word (0/1 hard decisions)."""
        got = vals[base:base + len(want)]
        bad = [i for i in range(len(want)) if got[i] != want[i]]
        if bad:
            print("  %s: %d/%d differ (first %s: hw=%s model=%s)" % (
                name, len(bad), len(want), bad[:6],
                [got[i] for i in bad[:6]], [want[i] for i in bad[:6]]))
            self.ok = False
            self.nfail += 1
        return len(want) - len(bad)

    def bytes_(self, name, base, want):
        got = vals[base:base + len(want)]
        bad = [i for i in range(len(want)) if got[i] != want[i]]
        if bad:
            print("  %s: %d/%d bytes differ at %s (hw=%s model=%s)" % (
                name, len(bad), len(want), bad, got, list(want)))
            self.ok = False
            self.nfail += 1
        return not bad


def golden_legs(rec):
    """Re-run the receive chains the hardware is supposed to reproduce."""
    out = {}
    d2, _h2 = LINK.rx_pdsch(rec["y2"], rec["ra_rnti"])
    buf = [0] * LINK.USABLE
    LINK.combine(buf, d2, rec["dci2"]["rv"])
    out["ms2"] = LINK.decode(buf)
    d3, _h3, _yq3 = R.pusch_rx_timed(rec["td3"], rec["tc_rnti"], rec["ta_window"])
    buf = [0] * LINK.USABLE
    LINK.combine(buf, d3, 0)
    out["ms3"] = LINK.decode(buf)
    d4, _h4 = LINK.rx_pdsch(rec["y4"], rec["tc_rnti"])
    buf = [0] * LINK.USABLE
    LINK.combine(buf, d4, rec["msg4"][0]["rv"])
    out["ms4"] = LINK.decode(buf)
    return out


def main(path, preamble, ta, occ, zczc, seed, msg4_rv):
    # Report.eq/bits/bytes_ index this module global; main installs it.
    global vals
    vals = parse_dump(path)
    if len(vals) < NRES:
        print("  dump too short: %d words (need >= %d)" % (len(vals), NRES))
        return 1

    sib1 = dict(S1.default_sib1())
    sib1["zero_corr_zone"] = zczc
    rec = R.rach_process(sib1, preamble, ta, occ, seed=seed,
                         msg4_rv=msg4_rv, verbose=False)
    g = R.gates(rec)
    bad_gates = [k for k, v in g.items() if not v]
    if bad_gates:
        print("  golden model itself does not close here: %s" % ",".join(bad_gates))
        return 1
    if len(rec["msg4"]) != 1:
        print("  the model needed %d Msg4 transmissions but the hardware runs one "
              "-- pick a point where the first resolves" % len(rec["msg4"]))
        return 1
    exp = golden_legs(rec)
    m1 = rec["msg1"]
    corr = R.msg1_correlations(rec["y1"], rec["chips"])
    n_cs = rec["n_cs"]
    rp = Report()
    rp.eq("marker", vals[0], MARKER)
    rp.eq("epilogue", vals[5], EPILOGUE)
    rp.eq("internal errors", vals[1], 0)

    # ------------------------------------------------------ Msg1: PRACH
    # The C writes the prologue at absolute indices 13..19, not R_MSG1 + 13..19.
    rp.eq("Msg1 array runs", vals[R_MSG1 + 11], R.L_RA)
    rp.eq("Msg1 N_CS", vals[R_MSG1 + 6], n_cs)
    rp.eq("Msg1 L_RA", vals[R_MSG1 + 7], R.L_RA)
    rp.eq("Msg1 taps", vals[R_MSG1 + 8], R.PRACH_TAPS)
    rp.eq("Msg1 partial/butterfly checks", vals[R_MSG1 + 9], R.L_RA * 32)
    rp.eq("Msg1 program cycles", vals[R_MSG1 + 10], 129)
    cbad = [d for d in range(R.L_RA)
            if s32(vals[R_C0 + 2 * d]) != corr[d][0]
            or s32(vals[R_C0 + 2 * d + 1]) != corr[d][1]]
    if cbad:
        d = cbad[0]
        print("  Msg1 correlations: %d/%d shifts differ (first d=%d: hw=(%d,%d) "
              "model=(%d,%d))" % (len(cbad), R.L_RA, d,
                                  s32(vals[R_C0 + 2 * d]),
                                  s32(vals[R_C0 + 2 * d + 1]),
                                  corr[d][0], corr[d][1]))
        rp.ok = False
        rp.nfail += 1
    rp.eq("Msg1 delay", vals[R_MSG1], m1["delay"])
    rp.eq("Msg1 preamble", vals[R_MSG1 + 1], preamble)
    rp.eq("Msg1 TA", vals[R_MSG1 + 2], ta)
    rp.eq("Msg1 2nd delay", vals[R_MSG1 + 5], m1["second_delay"])
    rp.eq("Msg1 peak re", s32(vals[R_MSG1 + 3]), m1["re"])
    rp.eq("Msg1 peak im", s32(vals[R_MSG1 + 4]), m1["im"])

    # ------------------------------------------------- Msg2: RAR delivery
    rp.eq("RA-RNTI", vals[R_MS2], rec["ra_rnti"])
    rp.eq("Msg2 DCI detected", vals[R_MS2 + 1], 1)
    rp.eq("Msg2 matched RNTI", vals[R_MS2 + 2], rec["ra_rnti"])
    rp.eq("Msg2 rv", vals[R_MS2 + 3], rec["dci2"]["rv"])
    rp.eq("Msg2 k0", vals[R_MS2 + 4], rec["dci2"]["rv"] * (LINK.USABLE // 4))
    rp.eq("Msg2 CRC24A", vals[R_MS2 + 5], 1)
    rp.bits("Msg2 DCI bits", R_MS2 + 6, PDCCH.pack_dci(rec["dci2"]))
    for name, off in (("mcs", 38), ("ndi", 39), ("harq_pid", 40),
                      ("rb_start", 41), ("rb_len", 42), ("tpc", 43),
                      ("reserved", 44)):
        rp.eq("Msg2 DCI " + name, vals[R_MS2 + off], rec["dci2"][name])
    rp.eq("Msg2 RAR indicator in reserved", vals[R_MS2 + 44] & 1, 1)
    rp.eq("Msg2 DMRS c_init s0", vals[R_MS2 + 60], F.dmrs_cinit(0))
    rp.eq("Msg2 DMRS c_init s1", vals[R_MS2 + 61], F.dmrs_cinit(1))
    rp.eq("Msg2 scrambling c_init", vals[R_MS2 + 62], rec["ra_rnti"] << 15)
    rp.bytes_("Msg2 RAR PDU", R_MS2 + 50, rec["rar_pdu"])
    rp.eq("Msg2 BI", vals[R_MS2 + 45], R.RAR_BI)
    rp.eq("Msg2 RAPID", vals[R_MS2 + 46], preamble)
    rp.eq("Msg2 TA command", vals[R_MS2 + 47], m1["ta"])
    rp.eq("Msg2 TC-RNTI", vals[R_MS2 + 48], rec["tc_rnti"])
    rp.eq("Msg2 UL grant", vals[R_MS2 + 49], MAC.pack_ul_grant(R.RAR_GRANT))
    rp.eq("Msg2 TC-RNTI is the RAR's", vals[R_MS2 + 48],
          R.tc_rnti_of(vals[R_MSG1 + 1]))
    n2 = rp.bits("Msg2 hard decisions", R_MS2_H, exp["ms2"]["hard"])
    rp.bits("Msg2 TB = the transmitted RAR PDU", R_MS2_H,
            R.pdu_to_tb(rec["rar_pdu"]))

    # --------------------------------------------------- Msg3: PUSCH
    rp.eq("Msg3 window TA", vals[R_MS3], m1["ta"])
    rp.eq("Msg3 CRC24A", vals[R_MS3 + 1], 1)
    rp.eq("Msg3 k0 (38.321 5.1.3: rv=0)", vals[R_MS3 + 2], 0)
    rp.eq("Msg3 scrambling c_init", vals[R_MS3 + 3], rec["tc_rnti"] << 15)
    rp.eq("Msg3 DMRS c_init s0", vals[R_MS3 + 4], PUSCH.dmrs_cinit_pusch(0))
    rp.eq("Msg3 DMRS c_init s1", vals[R_MS3 + 5], PUSCH.dmrs_cinit_pusch(1))
    rp.eq("Msg3 LCID", vals[R_MS3 + 26], MAC.LCID_CCCH)
    rp.eq("Msg3 SDU length", vals[R_MS3 + 27], len(rec["sdu3"]))
    rp.bytes_("Msg3 CCCH SDU", R_MS3 + 28, rec["sdu3"])
    for name, off in (("rb_start", 36), ("rb_len", 37), ("tda", 38), ("mcs", 39)):
        rp.eq("Msg3 grant " + name, vals[R_MS3 + off], R.RAR_GRANT[name])
    n3 = rp.bits("Msg3 hard decisions", R_MS3_H, exp["ms3"]["hard"])
    rp.bits("Msg3 TB = the transmitted CCCH PDU", R_MS3_H, R.pdu_to_tb(
        MAC.pack_pdu([(MAC.LCID_CCCH, rec["sdu3"])])))

    # ------------------------------------------------- Msg4: contention
    ms4 = rec["msg4"][0]
    rp.eq("Msg4 DCI detected", vals[R_MS4], 1)
    rp.eq("Msg4 matched RNTI", vals[R_MS4 + 1], rec["tc_rnti"])
    rp.eq("Msg4 rv", vals[R_MS4 + 2], ms4["rv"])
    rp.eq("Msg4 k0", vals[R_MS4 + 3], ms4["rv"] * (LINK.USABLE // 4))
    rp.eq("Msg4 CRC24A", vals[R_MS4 + 4], 1)
    rp.bits("Msg4 DCI bits", R_MS4 + 5, PDCCH.pack_dci(rec["dci4"]))
    for name, off in (("mcs", 37), ("ndi", 38), ("harq_pid", 39),
                      ("rb_start", 40), ("rb_len", 41), ("tpc", 42),
                      ("reserved", 43)):
        rp.eq("Msg4 DCI " + name, vals[R_MS4 + off], rec["dci4"][name])
    rp.eq("Msg4 CE matches own SDU", vals[R_MS4 + 44], 1)
    rp.eq("Msg4 contention resolved", vals[R_MS4 + 45], 1)
    rp.eq("Msg4 C-RNTI", vals[R_MS4 + 46], rec["tc_rnti"])
    rp.bytes_("Msg4 CE identity", R_MS4 + 47, rec["sdu3"][:MAC.CR_ID_BITS // 8])
    rp.eq("Msg4 scrambling c_init", vals[R_MS4 + 55], rec["tc_rnti"] << 15)
    n4 = rp.bits("Msg4 hard decisions", R_MS4_H, exp["ms4"]["hard"])
    rp.bits("Msg4 TB = the transmitted CE PDU", R_MS4_H,
            R.pdu_to_tb(MAC.pack_cr_pdu(rec["sdu3"])))

    print("  probe: %d errors, %d cycles, %d array runs, %d DMA ops" % (
        vals[1], vals[2], vals[3], vals[4]))
    print("  probe: Msg1 p=%d TA=%d delay=%d, margin %.1f dB -> RA-RNTI 0x%04x"
          % (vals[R_MSG1 + 1], vals[R_MSG1 + 2], vals[R_MSG1],
             10 * math.log10(max(1e-9, m1["margin"])), vals[R_MS2]))
    print("  probe: Msg2 rv=%d k0=%d, RAR TA=%d RAPID=%d TC-RNTI=0x%06x, "
          "Msg3 window TA=%d, Msg4 rv=%d k0=%d" % (
              vals[R_MS2 + 3], vals[R_MS2 + 4], vals[R_MS2 + 47],
              vals[R_MS2 + 46], vals[R_MS2 + 48], vals[R_MS3],
              vals[R_MS4 + 2], vals[R_MS4 + 3]))
    print("  probe: correlations %d/%d bit-exact, hard decisions %d/%d %d/%d "
          "%d/%d (Msg2/3/4)" % (R.L_RA - len(cbad), R.L_RA,
                                n2, NHARD, n3, NHARD, n4, NHARD))

    if rp.ok:
        print("[ OK ] 4-step random access on the CGRA SoC: Msg1 preamble %d + "
              "TA %d -> Msg2 RAR (TC-RNTI 0x%06x) -> Msg3 PUSCH -> Msg4 "
              "contention resolved, C-RNTI 0x%06x" % (
                  preamble, ta, rec["tc_rnti"], rec["c_rnti"]))
        return 0
    print("[FAIL] 4-step random access (%d gate(s) failed)" % rp.nfail)
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--preamble", type=int, default=2)
    ap.add_argument("--ta", type=int, default=9)
    ap.add_argument("--occ", type=int, default=1)
    ap.add_argument("--zczc", type=int, default=0)
    ap.add_argument("--seed", type=int, default=R.DEFAULT_SEED)
    ap.add_argument("--msg4-rv", type=int, default=None)
    a = ap.parse_args()
    sys.exit(main(a.dump, a.preamble, a.ta, a.occ, a.zczc, a.seed, a.msg4_rv))
