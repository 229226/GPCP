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
"""Check the B1 LDPC encoder (sw/soc/bench/ldpc_enc_bm.c).

Golden model: tools/ldpc_nr.py::encode (38.212 5.3.2 base graphs; already
cross-checked bit-exactly against ldpc5g.py for BG2/Zc=20).  The checker
compares every codeword bit, recomputes the syndrome independently, and
validates the result-area contract.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ldpc_nr as NR                                   # noqa: E402
from checklib import parse_dump, words32                # noqa: E402

MAGIC = 0x1D5C0DE1
META = 12


def read_td(path):
    with open(path, "rb") as f:
        hdr = struct.unpack("<8I", f.read(32))
        assert hdr[0] == MAGIC, f"bad encoder TD magic 0x{hdr[0]:08x}"
        bg, ils, zc, kb_used, nbits = hdr[1], hdr[2], hdr[3], hdr[4], hdr[5]
        raw = f.read()
    words = struct.unpack("<%dI" % (len(raw) // 4), raw)
    info = [(words[i // 32] >> (i % 32)) & 1 for i in range(nbits)]
    return bg, ils, zc, kb_used, nbits, info


def main():
    if len(sys.argv) != 3:
        print("usage: check_ldpc_enc.py <dump> <enc.td>")
        return 2
    st = parse_dump(sys.argv[1])
    v = words32(st)
    errs = []
    if st.get("timeout", 1):
        print("[FAIL] simulation timed out")
        return 1

    bg, ils, zc, kb_used, nbits, info = read_td(sys.argv[2])
    rows, cols, kb = NR.dims(bg)
    exp = NR.encode(bg, info, kb_used, zc)

    def check(cond, msg):
        if not cond:
            errs.append(msg)

    check(len(v) >= 16 + (cols * zc + 31) // 32,
          f"dump too short: {len(v)} words")
    check(v[0] == MAGIC, f"marker: got 0x{v[0]:08x} want 0x{MAGIC:08x}")
    check(v[1] == bg, f"bg: got {v[1]} want {bg}")
    check(v[2] == ils, f"ils: got {v[2]} want {ils}")
    check(v[3] == zc, f"zc: got {v[3]} want {zc}")
    check(v[4] == kb_used, f"kb_used: got {v[4]} want {kb_used}")
    check(v[5] == rows, f"rows: got {v[5]} want {rows}")
    check(v[6] == cols, f"cols: got {v[6]} want {cols}")
    check(v[7] == 0, f"hardware syndrome weight: {v[7]}")
    check(v[9] == 0, f"array runs: {v[9]}")
    check(v[10] == 0, f"encoder errors: {v[10]}")
    check(v[11] == 0x600D, f"epilogue: got 0x{v[11]:08x} want 0x600d")
    check(st.get("exit_code") == 0x600D,
          f"exit_code: 0x{st.get('exit_code', 0):x} want 0x600d")

    got = [(v[16 + i // 32] >> (i % 32)) & 1 for i in range(cols * zc)]
    bad = [i for i in range(cols * zc) if got[i] != exp[i]]
    check(not bad, f"{len(bad)} codeword bits differ (first {bad[:6]})")
    syn = NR.syndrome(bg, got, zc)
    check(syn == 0, f"golden syndrome weight: {syn}")

    print(f"[info] BG{bg}/i_LS={ils}/Zc={zc} kb_used={kb_used}: "
          f"{cols * zc} coded bits, {cols} columns, {rows} rows, "
          f"{v[8]} cycles, hardware syndrome {v[7]}")
    if errs:
        print(f"[FAIL] {len(errs)} problem(s)")
        for e in errs[:10]:
            print("       " + e)
        return 1
    print(f"[ OK ] LDPC encode BG{bg}/Zc={zc}: codeword matches "
          f"ldpc_nr.encode bit-exactly, syndrome 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
