#!/usr/bin/env python3
"""Check the PBCH payload assembly (sw/soc/pbch_pack_bm.c), gate run-pbch-pack.

The golden model is tools/nr_pbch.py::pbch_pack(), written independently from
the C benchmark.  The polar stage is additionally cross-checked against
ssb_plan.py (already validated by the Polar SC decoder), so a formula error in
the new model cannot pass just by agreeing with itself.

Result-area contract (32-bit words, see pbch_pack_bm.c header):
    [0] marker [1] mode [2] K [3] E [4] errors [5] cycles [6] 0x600D
    [7] payload [8] CRC24C [9] nid [10] amplitude [11] array runs
    [16..17] info bits  [18..33] u[512]  [34..49] d[512]
    [50..72]  e[720]    [73..95] e_sc[720]
    [96..815] 360 QPSK symbols, re then im, int32 (+-2000)
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_pbch as NR                                    # noqa: E402
import ssb_plan                                         # noqa: E402
from checklib import parse_dump, words32                # noqa: E402


def sx(v):
    return v - 0x100000000 if v >= 0x80000000 else v


def unpack(v, off, n):
    return [(v[off + i // 32] >> (i % 32)) & 1 for i in range(n)]


def main(dump_path, td_path):
    data = open(td_path, "rb").read()
    td = struct.unpack("<%dI" % (len(data) // 4), data)
    if td[0] != NR.PACK_MAGIC:
        sys.exit("[FAIL] bad TD magic 0x%08x" % td[0])
    mode, payload, nid, E, amp = td[1], td[2], td[3], td[4], td[5]
    r = NR.pbch_pack(payload, nid, mode)
    if r["d"] != ssb_plan.polar_encode(r["u"]):
        sys.exit("[FAIL] golden model disagrees with ssb_plan.polar_encode")

    st = parse_dump(dump_path)
    if st.get("timeout", 1):
        sys.exit("[FAIL] simulation timed out")
    if st.get("exit_code") != 0x600D:
        sys.exit("[FAIL] exit_code 0x%x" % st.get("exit_code", 0))
    v = words32(st)
    if len(v) < 816:
        sys.exit("[FAIL] short dump: %d words" % len(v))

    errs = []

    def check(name, got, want):
        if got != want:
            errs.append("%s: got %r want %r" % (name, got, want))

    def checkbits(name, got, want):
        bad = [i for i, (a, b) in enumerate(zip(got, want)) if a != b]
        if bad or len(got) != len(want):
            errs.append("%s: %d/%d bits differ, first %s"
                        % (name, len(bad), len(want), bad[:5]))

    check("marker", v[0], 0xCAFEBABE)
    check("mode", v[1], mode)
    check("K", v[2], r["k"])
    check("E", v[3], NR.E_PBCH)
    check("errors", v[4], 0)
    check("epilogue", v[6], 0x600D)
    check("payload echo", v[7], payload)
    crc_word = sum(r["crc"][i] << (NR.CRC_LEN - 1 - i)
                   for i in range(NR.CRC_LEN)) if mode else 0
    check("CRC24C", v[8], crc_word)
    check("nid_cell", v[9], nid)
    check("amplitude", v[10], NR.PBCH_QPSK)
    check("array runs", v[11], 0)

    checkbits("info bits", unpack(v, 16, r["k"]), r["info"])
    checkbits("u[512]", unpack(v, 18, NR.NPOL), r["u"])
    checkbits("d[512]", unpack(v, 34, NR.NPOL), r["d"])
    checkbits("e[720]", unpack(v, 50, NR.E_PBCH), r["e"])
    checkbits("e_sc[720]", unpack(v, 73, NR.E_PBCH), r["e_sc"])

    badq = 0
    for i, z in enumerate(r["qpsk"]):
        if sx(v[96 + 2 * i]) != int(z.real) or \
           sx(v[96 + 2 * i + 1]) != int(z.imag):
            badq += 1
    if badq:
        errs.append("QPSK: %d/%d symbols differ" % (badq, len(r["qpsk"])))

    print("[info] PBCH assembly mode=%d K=%d E=%d, %d QPSK symbols at +-%d, "
          "0 array runs, %d cycles"
          % (mode, r["k"], E, NR.E_PBCH // 2, amp, v[5]))
    if errs:
        for e in errs[:12]:
            print("[FAIL] %s" % e)
        print("[FAIL] PBCH payload assembly: %d sections differ" % len(errs))
        return 1
    print("[ OK ] PBCH payload assembly: MIB -> %s -> frozen -> polar -> "
          "rate matching -> scrambling -> QPSK, all stages match the model "
          "(0 differences)" % ("CRC24C" if mode else "no CRC"))
    return 0


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "pbch_pack_dump.txt"
    t = sys.argv[2] if len(sys.argv) > 2 else "build/pbch/pbch_pack_m0.td"
    sys.exit(main(d, t))
