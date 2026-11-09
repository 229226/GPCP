#!/usr/bin/env python3
"""G2/B4 gate: PDCCH self-loopback on one SoC (sw/soc/pdcch_loop_bm.c).

The transmit chain (B3) builds the waveform in RAM and the receive chain (A4)
decodes it in the same program, so this is the first half of docs/19 G2's
acceptance ("the DCI we transmit is blind-detected by our own receiver").

Anchors, in order of strength:
  1. e_sc bits (432) bit-exact against tools/nr_pdcch_tx.pdcch_tx  -- the
     waveform the transmitter really built;
  2. decoded DCI bits (32) == transmitted DCI bits;
  3. blind detection picked the transmitted RNTI out of five candidates, with a
     valid CRC24C;
  4. every one of the 432 raw LLR signs matches the transmitted bit, and the LLR
     magnitudes sit near the QPSK amplitude (a sign-only check would also pass a
     loopback that lost 90% of its energy).
"""
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import nr_pdcch as PDCCH                                # noqa: E402
import nr_pdcch_tx as TX                                # noqa: E402

A_DCI = TX.A_DCI
E_BITS = TX.E_BITS
QAM = TX.QAM
MARKER = 0xCAFEB4B1


def read_dump(path):
    words = []
    for line in open(path):
        p = line.split()
        if len(p) >= 3 and p[0] == "word" and p[2].startswith("0x"):
            w = int(p[2], 16)
            words.append(w & 0xFFFFFFFF)
            words.append((w >> 32) & 0xFFFFFFFF)
    return words


def sx(v):
    return v - 0x100000000 if v >= 0x80000000 else v


def main(dump, td_path):
    w = read_dump(dump)
    if len(w) < 544:
        print(f"  dump too short: {len(w)} words")
        return 1
    ok = True

    if w[0] != MARKER:
        print(f"  bad marker 0x{w[0]:08x} (want 0x{MARKER:08x})")
        return 1
    if w[1] != 0:
        print(f"  errors reported by the program: {w[1]}")
        ok = False

    par = TX.read_td(td_path)
    rnti = par["variants"][0]
    ref = TX.pdcch_tx(TX.FIELDS, rnti)
    dci_ref = 0
    for b in ref["dci"]:
        dci_ref = (dci_ref << 1) | (b & 1)

    # ---- 1. the waveform the transmitter built ------------------------------
    got_esc = w[112:112 + E_BITS]
    if got_esc != ref["e_sc"]:
        bad = sum(1 for a, b in zip(got_esc, ref["e_sc"]) if a != b)
        print(f"  scrambled codeword: {bad}/{E_BITS} bits differ from the model")
        ok = False
    else:
        print(f"  scrambled codeword E={E_BITS} vs nr_pdcch_tx     : OK")

    # ---- 2. DCI round trip --------------------------------------------------
    tx_bits = w[16:16 + A_DCI]
    rx_bits = w[48:48 + A_DCI]
    if w[4] != dci_ref:
        print(f"  transmitted DCI 0x{w[4]:08x} != model 0x{dci_ref:08x}")
        ok = False
    if w[5] != w[4]:
        print(f"  decoded DCI 0x{w[5]:08x} != transmitted 0x{w[4]:08x}")
        ok = False
    if rx_bits != tx_bits:
        n = sum(1 for a, b in zip(rx_bits, tx_bits) if a != b)
        print(f"  DCI bits: {n}/{A_DCI} differ")
        ok = False
    else:
        print(f"  DCI 0x{w[4]:08x}: {A_DCI}/{A_DCI} bits decoded       : OK")

    # ---- 3. blind detection -------------------------------------------------
    if w[6] != rnti:
        print(f"  transmitted RNTI 0x{rnti:04x} (from the stimulus)")
        ok = False
    if w[7] != rnti:
        print(f"  blind detection matched 0x{w[7]:04x}, want 0x{rnti:04x}")
        ok = False
    if w[8] != 1:
        print("  CRC24C did not validate")
        ok = False
    if w[7] == rnti and w[8] == 1:
        print(f"  blind RNTI 0x{rnti:04x} out of 5 candidates, CRC24C : OK")

    # ---- 4. LLR signs and levels -------------------------------------------
    if w[9] != 0:
        print(f"  {w[9]}/{E_BITS} raw LLR signs disagree with the bits sent")
        ok = False
    amin, amax = w[10], w[11]
    lo, hi = QAM // 2, QAM * 2
    if not (lo <= amin <= QAM * 3 // 2 and QAM <= amax <= hi):
        print(f"  LLR |abs| range [{amin}, {amax}] is outside the expected "
              f"[{lo}, {hi}] window (QPSK amplitude {QAM})")
        ok = False
    if w[9] == 0:
        print(f"  raw LLR: 432/432 signs match, |abs| in [{amin}, {amax}]   : OK")

    print(f"[info] {w[3]} array runs, {w[2]} cycles, n_id {w[12]}, "
          f"max|sym|={w[108]}")
    if not ok:
        print("[FAIL] PDCCH self-loopback")
        return 1
    print("[ OK ] PDCCH self-loopback: TX -> IFFT+CP -> RX on one SoC, "
          "DCI 32/32, RNTI blind-detected, LLRs clean")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: check_pdcch_loop.py <dump> <td>")
    sys.exit(main(sys.argv[1], sys.argv[2]))
