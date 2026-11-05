#!/usr/bin/env python3
"""Acceptance check for run-link-e2e (DCI-driven PDSCH on the SoC).

The benchmark loads one combined TD (PDCCH symbol + PDSCH symbols), decodes the
DCI, reads `rv` out of it, and uses that rv as the PDSCH circular-buffer offset.
This checker re-derives the golden DCI (from the same fields used by the dump)
and the golden PDSCH transport block (from pdsch_full_plan at the same rv), then
verifies:

  * PDCCH: epilogue, zero internal errors, RNTI blind-detected, 32/32 DCI bits
  * the decoded DCI `rv` equals the one the PDSCH was generated with
  * PDSCH: bit-exact 1040 LDPC hard decisions vs the golden model, CRC24A, and
    the recovered 80-bit transport block
  * the reported k0 equals rv * (USABLE/4)

Usage mirrors the other checkers:
  python3 tools/check_link_e2e.py <dump.txt> --rv <rv> --snr <dB> --seed <s>
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_pdcch as PDCCH
import pdsch_full_plan as F
import pdsch_plan as M

MARKER = 0xCAFEBABE
EPILOGUE = 0x600D
NHARD = 1040


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


def golden_pdsch(snr, seed, rv):
    """Golden PDSCH hard decisions / TB for a single transmission at `rv`."""
    grid = F.build_link_fid(rv)
    y = M.add_channel(grid, snr, seed)
    td = F.td_from_y(y)
    yq = F.quantised_y(td)
    h = F.channel_estimate_fid(yq)
    raw = M.extract_llr(yq, h)
    llr_e = F.deinterleave(raw)
    scr = M.scrambling_seq()
    descr = [(-v if scr[k] else v) for k, v in enumerate(llr_e)]
    buf = [0] * F.USABLE
    k0 = F.RV_K0[rv]
    for k, v in enumerate(descr):
        buf[(k0 + k) % F.USABLE] += v
    dem = [0] * F.PUNCT
    u = 0
    for i in range(F.NCODED - F.PUNCT):
        if F.FILLER0 <= i < F.FILLER0 + F.FILLER_N:
            dem.append(0)
        else:
            dem.append(buf[u])
            u += 1
    hard, _ = F.hw_decode(dem)
    tb = hard[:M.A_TB]
    crc = hard[M.A_TB:M.A_TB + M.CRC_LEN]
    return dict(hard=hard, tb=tb, crc_ok=(M.crc24a(tb) == crc),
                want=M.payload_bits(), h=h, raw=raw, descr=descr,
                scaled=[v >> 8 for v in dem])


def main(path, rv, snr, seed):
    vals = parse_dump(path)
    if len(vals) < 1096:
        print("  dump too short: %d words (need >= 1096)" % len(vals))
        return 1

    fields = dict(mcs=17, rv=rv, ndi=1, harq_pid=0,
                  rb_start=0, rb_len=8, tpc=0, reserved=0)
    exp_dci = PDCCH.pack_dci(fields)
    g = golden_pdsch(snr, seed, rv)
    ok = True

    if vals[0] != MARKER:
        print("  epilogue missing: 0x%08x" % vals[0]); ok = False
    if vals[6] != EPILOGUE:
        print("  program did not finish: 0x%08x" % vals[6]); ok = False
    if vals[1] != 0:
        print("  program reported %d internal errors" % vals[1]); ok = False
    if vals[4] != PDCCH.N_RNTI:
        print("  PDCCH RNTI: hw=0x%04x expected 0x%04x" % (vals[4], PDCCH.N_RNTI))
        ok = False
    if vals[5] != 1:
        print("  PDCCH CRC24C did not pass on any candidate"); ok = False

    dci = vals[8:40]
    if dci != exp_dci:
        bad = [i for i, (a, b) in enumerate(zip(dci, exp_dci)) if a != b]
        print("  DCI bits: %d/32 differ (first %s)" % (len(bad), bad[:8])); ok = False

    if vals[7] != rv:
        print("  decoded rv=%d expected %d" % (vals[7], rv)); ok = False
    parsed = dict(mcs=vals[40], ndi=vals[41], harq_pid=vals[42],
                  rb_start=vals[43], rb_len=vals[44], tpc=vals[45])
    for name, want in (("mcs", fields["mcs"]), ("ndi", fields["ndi"]),
                       ("harq_pid", fields["harq_pid"]), ("rb_start", 0),
                       ("rb_len", 8), ("tpc", 0)):
        if parsed[name] != want:
            print("  DCI field %s: hw=%d expected %d" % (name, parsed[name], want))
            ok = False

    if vals[46] != 1:
        print("  PDSCH CRC24A failed"); ok = False

    hard = vals[48:48 + NHARD]
    bad = [i for i in range(NHARD) if hard[i] != g["hard"][i]]
    if bad:
        print("  LDPC hard decisions: %d/%d differ (first %s)" % (
            len(bad), NHARD, bad[:6])); ok = False

    tb = hard[:M.A_TB]
    tbbad = [i for i, (a, b) in enumerate(zip(tb, g["want"])) if a != b]
    if tbbad:
        print("  transport block: %d/%d bits differ (first %s)" % (
            len(tbbad), M.A_TB, tbbad[:8])); ok = False

    k0 = vals[1095]
    if k0 != rv * (F.USABLE // 4):
        print("  k0: hw=%d expected %d" % (k0, rv * (F.USABLE // 4))); ok = False

    if vals[1093] != F.dmrs_cinit(0) or vals[1094] != F.dmrs_cinit(1):
        print("  DMRS c_init mismatch: hw=(0x%x,0x%x) model=(0x%x,0x%x)" % (
            vals[1093], vals[1094], F.dmrs_cinit(0), F.dmrs_cinit(1))); ok = False

    # NOTE on the front-end LLR probes: the C model and the golden model lay out
    # the per-RE equalised LLRs in *different orders* (different pilot skipping /
    # symbol-major indexing), so an element-wise "first-8 sign" compare between
    # the C's stored probe LLRs and the golden raw/de-interleaved/de-matched
    # arrays is meaningless and was removed.  The definitive proof that the entire
    # front-end -- FFT, DMRS least-squares, equalisation, QPSK LLR, de-interleave,
    # scramble and rate de-match -- is correct is the bit-exact 1040/1040 LDPC
    # hard-decision match above, which in turn drives the CRC24A and 80-bit TB
    # checks.  A channel-estimate probe slot is intentionally not asserted: it was
    # never written by the firmware (see the result-area layout in link_e2e_bm.c).

    print("  probe: %d errors, %d cycles, %d array runs" % (vals[1], vals[2], vals[3]))
    print("  probe: PDCCH RNTI=0x%04x crc_ok=%d, DCI rv=%d fields=%s" % (
        vals[4], vals[5], vals[7], parsed))
    print("  probe: PDSCH CRC24A ok=%d, TB %d/%d bits, hard %d/%d" % (
        vals[46], M.A_TB - len(tbbad), M.A_TB, NHARD - len(bad), NHARD))
    print("  probe: k0=%d (= rv %d * USABLE/4)" % (k0, rv))

    if ok:
        print("[ OK ] DCI-driven link: PDCCH->DCI(rv=%d)->PDSCH circular buffer "
              "-> 80/80 TB" % rv)
        return 0
    print("[FAIL] DCI-driven link acceptance")
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--rv", type=int, default=0)
    ap.add_argument("--snr", type=float, default=20.0)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    sys.exit(main(a.dump, a.rv, a.snr, a.seed))
