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
"""Combined TD for the DCI-driven link benchmark (tools/link_e2e_bm.c).

Writes one binary file:  [ PDCCH TD (1 symbol) | PDSCH TD (2 symbols) ].

The PDCCH symbol encodes a DCI whose `rv` field equals --rv; the PDSCH
symbols are generated at that same rv.  The SoC must therefore read `rv` out of
the decoded DCI and use it as the PDSCH circular-buffer offset k0 -- if it
hard-coded k0=0 the rv=2 case would fail (k0 would be wrong by 492 positions).
"""
import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nr_pdcch as PDCCH
import pdsch_full_plan as F
import pdsch_plan as M

TD_SCALE = 256
PDCCH_SNR = 12.0


def pack_pdcch_td(fields, rnti, snr, seed):
    syms, _ = PDCCH.transmit(fields, rnti)
    td, _ = PDCCH.td_from_syms(syms, snr, seed)
    out = bytearray()
    for c in td:
        out += struct.pack("<I", (int(c.real) * TD_SCALE) & 0xFFFFFFFF)
        out += struct.pack("<I", (int(c.imag) * TD_SCALE) & 0xFFFFFFFF)
    return bytes(out)


def pack_pdsch_td(rv, snr, seed):
    grid = F.build_link_fid(rv)
    y = M.add_channel(grid, snr, seed)
    tds = F.td_from_y(y)
    out = bytearray()
    for blk in tds:
        for c in blk:
            out += struct.pack("<I", (int(c.real) * TD_SCALE) & 0xFFFFFFFF)
            out += struct.pack("<I", (int(c.imag) * TD_SCALE) & 0xFFFFFFFF)
    return bytes(out)


def main():
    ap = argparse.ArgumentParser(description="combined PDCCH+PDSCH TD for link_e2e")
    ap.add_argument("out")
    ap.add_argument("--rv", type=int, default=0,
                    help="redundancy version carried by the DCI and used by PDSCH")
    ap.add_argument("--snr", type=float, default=20.0, help="PDSCH SNR (dB)")
    ap.add_argument("--pdcch-snr", type=float, default=PDCCH_SNR,
                    help="PDCCH control-channel SNR (dB)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--mcs", type=int, default=17)
    ap.add_argument("--ndi", type=int, default=1)
    ap.add_argument("--harq-pid", type=int, default=0)
    a = ap.parse_args()

    fields = dict(mcs=a.mcs, rv=a.rv, ndi=a.ndi, harq_pid=a.harq_pid,
                  rb_start=0, rb_len=8, tpc=0, reserved=0)
    pd = pack_pdcch_td(fields, PDCCH.N_RNTI, a.pdcch_snr, a.seed)
    ps = pack_pdsch_td(a.rv, a.snr, a.seed)
    with open(a.out, "wb") as f:
        f.write(pd)
        f.write(ps)
    print("wrote %s : PDCCH %d B + PDSCH %d B "
          "(rv=%d, pdsch_snr=%.1f dB, pdcch_snr=%.1f dB, seed=%d)" % (
              a.out, len(pd), len(ps), a.rv, a.snr, a.pdcch_snr, a.seed))


if __name__ == "__main__":
    main()
