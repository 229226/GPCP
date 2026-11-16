#!/usr/bin/env python3
# --- tools path bootstrap (subdirs import root modules) ---
import os as _os, sys as _sys
_T = _os.path.dirname(_os.path.abspath(__file__))
_TOOLS = _T if _os.path.basename(_T) == "tools" else _os.path.dirname(_T)
for _p in (_TOOLS, _os.path.join(_TOOLS, "models"), _os.path.join(_TOOLS, "checks"),
           _os.path.join(_os.path.dirname(_TOOLS), "cgra", "tools")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
_ROOT = _os.path.dirname(_TOOLS)
# --- end bootstrap ---
"""Check the PBCH polar transform (sw/soc/bench/pbch_tx_bm.c).

The stimulus comes from the same TD the benchmark loads (16 words of 32 bits =
512 codeword input bits, LSB first), so this script only re-derives the expected
butterfly result with tools/models/ssb_plan.py's polar_encode -- an implementation that
was already validated against the Polar SC decoder.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ssb_plan


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
    data = open(td_path, "rb").read()
    td = struct.unpack("<%dI" % (len(data) // 4), data)
    if td[0] != 0x50424348:
        sys.exit("bad TD magic 0x%08x" % td[0])
    nbits = td[1]
    nw = nbits // 32
    if nbits != ssb_plan.NPOL:
        sys.exit("TD has %d bits, the polar model works on %d"
                 % (nbits, ssb_plan.NPOL))
    u = [(td[8 + i] >> b) & 1 for i in range(nw) for b in range(32)]
    exp = ssb_plan.polar_encode(u)

    vals = parse_dump(dump_path)
    if len(vals) < 16 + nw:
        sys.exit("[FAIL] short dump (%d words)" % len(vals))
    if vals[0] != 0xCAFEBABE:
        sys.exit("[FAIL] marker 0x%08x" % vals[0])
    if vals[3] != 0:
        sys.exit("[FAIL] benchmark reported %d bus/timeout errors" % vals[3])
    got = [(vals[16 + i] >> b) & 1 for i in range(nw) for b in range(32)]

    bad = [i for i in range(nbits) if got[i] != exp[i]]
    print("[info] polar transform N=%d, %d stages, %d array runs, %d cycles"
          % (nbits, vals[1], vals[2], vals[5]))
    if bad:
        for i in bad[:6]:
            print("[diff] bit %d: got %d want %d" % (i, got[i], exp[i]))
        print("[FAIL] PBCH polar encode: %d/%d bits differ" % (len(bad), nbits))
        return 1
    print("[ OK ] PBCH polar encode on the CGRA: %d/%d bits match the model, "
          "9 stages / %d array runs" % (nbits, nbits, vals[2]))
    return 0


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "pbch_tx_dump.txt"
    t = sys.argv[2] if len(sys.argv) > 2 else "build/pbch/pbch_tx.td"
    sys.exit(main(d, t))
