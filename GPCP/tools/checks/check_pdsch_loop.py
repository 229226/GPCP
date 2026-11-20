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
"""G2/B5 gate: PDSCH self-loopback on one SoC (sw/soc/bench/pdsch_loop_bm.c).

The B2 transmit chain builds the two-symbol waveform in RAM (LDPC encode ->
rate match -> scramble -> interleave -> QPSK -> Gold DMRS + RE mapping ->
CGRA IFFT + CP) and the pdsch_full receive chain decodes it in the same
program, so together with run-pdcch-loop this closes docs/project/README.md G2's acceptance.

Anchors, strongest first:
  1. the 1040 hard decisions equal the codeword the transmitter encoded
     (recomputed here with tools/models/nr_dl_mod.encode, which is itself cross-checked
     against ldpc_nr in that file's self-test);
  2. the 80 decoded TB bits equal the transmitted TB bits (CRC24A valid);
  3. 720/720 LLR signs after descrambling match the transmitted E sequence,
     and the equalised |LLR| sits near 16*QPSK (flat unit channel);
  4. the per-symbol LS estimate is (16, 0) = (H_SCALE, 0), i.e. the DMRS the
     receiver regenerated matched what it actually received;
  5. DMRS c_init = 0x120002 / 0x180002 (38.211 7.4.1.1 with n_ID=1, l=2/3).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import nr_dl_mod as TX                                  # noqa: E402

A_TB = TX.A_TB
NCODED = TX.NCODED
E_BITS = TX.E_BITS
QPSK = 2000
H_SCALE = 16
MARKER = 0xCAFEBD51


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
    if len(w) < 1180:
        print(f"  dump too short: {len(w)} words")
        return 1
    ok = True

    if w[0] != MARKER:
        print(f"  bad marker 0x{w[0]:08x} (want 0x{MARKER:08x})")
        return 1
    if w[1] != 0:
        print(f"  errors reported by the program: {w[1]}")
        ok = False

    tb, variants, _ = TX.read_td(td_path)
    qm, rv, rnti = variants[0]

    # ---- 1. hard decisions vs the encoded codeword -------------------------
    cw = TX.encode(tb)
    have = [w[8 + b * 20 + i] & 1 for b in range(52) for i in range(20)]
    bad = sum(1 for a, b in zip(have, cw) if a != b)
    if bad:
        print(f"  hard decisions: {bad}/{NCODED} differ from the transmitted codeword")
        ok = False
    else:
        print(f"  LDPC hard decisions {NCODED}/{NCODED} == transmitted codeword : OK")

    # ---- 2. transport block ------------------------------------------------
    tx_bits = w[1090:1090 + A_TB]
    rx_bits = w[1180:1180 + A_TB]
    if tx_bits != tb:
        print("  transmitted TB dump does not match the stimulus")
        ok = False
    if rx_bits != tx_bits:
        n = sum(1 for a, b in zip(rx_bits, tx_bits) if a != b)
        print(f"  decoded TB: {n}/{A_TB} bits differ")
        ok = False
    if w[5] != 1:
        print("  CRC24A did not validate")
        ok = False
    if rx_bits == tx_bits and w[5] == 1:
        print(f"  transport block {A_TB}/{A_TB} bits, CRC24A ok          : OK")

    # ---- 3. LLR signs and level -------------------------------------------
    if w[1173] != 0:
        print(f"  {w[1173]}/{E_BITS} descrambled LLR signs disagree with the TX bits "
              f"(first at {w[1174]}, value {sx(w[1175])})")
        ok = False
    lvl = QPSK * H_SCALE
    raw = [sx(w[1052 + i]) for i in range(8)]
    amin = min(abs(v) for v in raw)
    amax = max(abs(v) for v in raw)
    if not (lvl // 2 <= amin and amax <= lvl * 2):
        print(f"  equalised |LLR| in [{amin}, {amax}], expected around {lvl}")
        ok = False
    if w[1173] == 0:
        print(f"  raw LLR: 720/720 signs, |LLR| in [{amin}, {amax}] (~16 x QPSK) : OK")

    # ---- 4. channel estimate ----------------------------------------------
    h = [sx(w[1048 + i]) for i in range(4)]
    if not (abs(h[0] - H_SCALE) <= 1 and abs(h[2] - H_SCALE) <= 1
            and h[1] == 0 and h[3] == 0):
        print(f"  LS channel estimate {h}, expected ~[{H_SCALE}, 0, {H_SCALE}, 0]")
        ok = False
    else:
        print(f"  per-symbol LS estimate {h} (H_SCALE={H_SCALE})              : OK")

    # ---- 5. DMRS seeds ----------------------------------------------------
    if w[1081] != 0x120002 or w[1082] != 0x180002:
        print(f"  DMRS c_init 0x{w[1081]:06x}/0x{w[1082]:06x}, "
              f"expected 0x120002/0x180002")
        ok = False
    if w[1172] != 0:
        print(f"  LDPC encoder syndrome = 0x{w[1172]:x}")
        ok = False
    if w[1171] != 720 or w[1170] != QPSK:
        print(f"  constellation: {w[1171]} RE values, max|sym| {w[1170]}")
        ok = False

    print(f"[info] {w[3]} array runs, {w[2]} cycles, {w[4]} DMA, "
          f"TB {w[7]}/{A_TB}, rv {w[1083]}, Qm {w[1084]}")
    if not ok:
        print("[FAIL] PDSCH self-loopback")
        return 1
    print("[ OK ] PDSCH self-loopback: TB -> LDPC -> waveform -> LDPC -> TB on "
          "one SoC, 1040/1040 hard, CRC ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: check_pdsch_loop.py <dump> <td>")
    sys.exit(main(sys.argv[1], sys.argv[2]))
