#!/usr/bin/env python3
"""Check the slot-timebase SSB benchmark (sw/soc/ssb_slot_bm.c).

This is the P0.2 integration: the sample-level SSB chain (FFT x4 -> PSS -> SSS
-> DMRS -> QPSK -> polar SC -> MIB) is driven from the CLINT slot timer, with the
336 SSS hypotheses amortised across slot ticks (one batch per tick) instead of
run as one straight line.

We re-use the same golden models as tools/check_ssb_fft.py: what is checked is the
*decision* (N_ID2, N_ID1, the 32 MIB bits) and the channel estimate H == (16, 0),
plus the slot-scheduling bookkeeping the benchmark reports:

    word[8]    errs (+ overruns + bad_traps, folded in by the program)
    word[9]    total cycles of the whole SSB
    word[11]   0x600D epilogue
    word[942]  detected N_ID1        word[943]  true N_ID1
    word[946..953]  channel estimate H (re,im) per symbol, H_SCALE = 16
    word[954..985]  decoded MIB bits
    word[991]  sss_ticks actually serviced
    word[992]  slot deadline overruns
    word[993]  unexpected traps
    word[994]  SLOT_TICK_PERIOD (cycles)
    word[995]  SSS_BATCH (hypotheses / tick)

The 20 ms NR budget at 100 MHz is 2,000,000 cycles.  Scheduling alone does not
reduce the fixed compute, so a single SSB still lands above that; the checker
reports the gap and flags it as a CGRA-throughput follow-up rather than a failure.
"""
import sys
import ssb_plan as sp

NREP = 127
PSS_RE0 = 56
NHYP = 336
SSS_BATCH = 24
BUDGET_20MS = 2_000_000          # cycles @ 100 MHz


def sx32(v):
    return v - 0x100000000 if v & 0x80000000 else v


def sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def i32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def main(path):
    vals = []
    for line in open(path):
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "word" and parts[2].startswith("0x"):
            w64 = int(parts[2], 16)
            vals.append(w64 & 0xFFFFFFFF)
            vals.append((w64 >> 32) & 0xFFFFFFFF)
    if len(vals) < 996:
        print(f"  dump too short: {len(vals)} words (need >= 996)")
        return 1

    ok = True

    # ---- epilogue + error counters ----------------------------------------
    if vals[11] != 0x600D:
        print(f"  epilogue missing: 0x{vals[11]:08x}")
        ok = False
    if vals[8] != 0:
        print(f"  errors reported by the program: {vals[8]} "
              f"(chain errs + slot overruns + bad traps)")
        ok = False

    # ---- PSS: N_ID2 ------------------------------------------------------
    if vals[6] != 1:
        print(f"  detected N_ID2: got {vals[6]} expected 1")
        ok = False

    # ---- SSS: N_ID1 ------------------------------------------------------
    got_nid1 = vals[942]
    true_nid1 = vals[943]
    if got_nid1 != true_nid1:
        print(f"  SSS: detected N_ID1 {got_nid1} instead of {true_nid1}")
        ok = False
    # cross-check against the floating-point model on the SSS symbol
    td, grid, payload = sp.build_td()
    x3 = sp._fft(td[3])
    seq = [sp.nr_seq.sss_seq(h, 1) for h in range(NHYP)]
    gold = [abs(sum(x3[sp.SSB_K0 + PSS_RE0 + t].real * seq[h][t] for t in range(NREP))) +
            abs(sum(x3[sp.SSB_K0 + PSS_RE0 + t].imag * seq[h][t] for t in range(NREP)))
            for h in range(NHYP)]
    best_h = max(range(NHYP), key=lambda h: gold[h])
    if best_h != true_nid1:
        print(f"  SSS: model itself does not peak on {true_nid1} (argmax {best_h}) "
              f"- grid/RE layout problem")
        ok = False
    print(f"  probe: SSS N_ID1 hw={got_nid1} (metric {vals[944]}) "
          f"true={true_nid1} (metric {vals[945]}), model argmax={best_h}")

    # ---- DMRS channel estimate + decoded MIB ------------------------------
    h = [(sx32(vals[946 + 2 * s]), sx32(vals[947 + 2 * s])) for s in range(4)]
    print(f"  probe: channel estimate (H_SCALE=16) per symbol: {h}")
    if any(abs(hre - 16) > 2 or abs(him) > 2 for hre, him in h):
        print("  DMRS: channel estimate is not (16, 0) - pilot Gold/c_init wrong")
        ok = False
    mib = [vals[954 + k] for k in range(32)]
    want = sp.payload_bits()
    bad = [k for k in range(32) if mib[k] != want[k]]
    print(f"  probe: decoded MIB bits = {''.join(str(b) for b in mib)}")
    print(f"  probe: model   MIB bits = {''.join(str(b) for b in want)}")
    if bad:
        print(f"  MIB: {len(bad)}/32 bits differ (first {bad[:6]})")
        ok = False

    # ---- slot-scheduling bookkeeping -------------------------------------
    sss_ticks = vals[991]
    overruns = vals[992]
    bad_traps = vals[993]
    tick_period = vals[994]
    sss_batch = vals[995]
    cycles = vals[9]

    expect_ticks = (NHYP + sss_batch - 1) // sss_batch
    if sss_ticks != expect_ticks:
        print(f"  slot: sss_ticks={sss_ticks} expected {expect_ticks} "
              f"(NHYP={NHYP}/batch={sss_batch})")
        ok = False
    if overruns != 0:
        print(f"  slot: {overruns} slot-deadline overruns (tick_period={tick_period})")
        ok = False
    if bad_traps != 0:
        print(f"  slot: {bad_traps} unexpected traps taken")
        ok = False

    ratio = cycles / max(1, BUDGET_20MS)
    print(f"  slot: sss_ticks={sss_ticks} (expect {expect_ticks}), "
          f"overruns={overruns}, bad_traps={bad_traps}")
    print(f"  slot: tick_period={tick_period} cycles, sss_batch={sss_batch} hypotheses/tick")
    print(f"  timing: total {cycles} cycles for one SSB")
    print(f"  timing: 20 ms @100 MHz = {BUDGET_20MS} cycles -> "
          f"{ratio:.2f}x over budget")
    if cycles > BUDGET_20MS:
        print("[warn] single-SSB compute is above the 20 ms budget; closing the "
              "gap needs CGRA-throughput work (faster FFT/array kernels), not "
              "scheduling -- this is the documented P0.2 follow-up")

    if ok:
        print(f"[info] N_ID2={vals[6]}, N_ID1={got_nid1}, MIB 32/32, "
              f"{sss_ticks} slot ticks, {cycles} cycles")
        print("[ OK ] slot-timebase SSB: sample-level chain driven by the CLINT "
              "slot timer, SSS amortised across ticks, MIB 32/32, no overruns")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else
                 "sim/soc/build/ssb_slot_dump.txt"))
