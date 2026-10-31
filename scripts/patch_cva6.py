#!/usr/bin/env python3
"""Apply the GPCP patches to the fetched CVA6 tree (idempotent).

CVA6 does not implement the read-only `time` CSR (0xC01), so every `rdtime`
traps into M-mode firmware.  Linux reads the counter frequently during boot,
which turns into a trap storm (OpenSBI `sbi_emulate_csr_read` / `hpm_allowed`).

This patch threads the CLINT `mtime` value into `csr_regfile` and returns it
for `CSR_TIME`, so S-mode reads of `time` no longer trap.

Changes:
  1. core/csr_regfile.sv : new `mtime_i` port + CSR_TIME read case
  2. core/cva6.sv        : new `mtime_i` port + connect to csr_regfile
  3. corev_apu/src/ariane.sv : new `mtime_i` port + connect to cva6
  4. (the SoC connects CLINT mtime; see rtl/soc/gpcp_soc.sv)

Usage: python3 scripts/patch_cva6.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CVA6 = ROOT / "third_party" / "cva6"


def patch(path: Path, old: str, new: str, marker: str) -> bool:
    s = path.read_text()
    if marker in s:
        print(f"[skip] {path.relative_to(ROOT)} already patched")
        return False
    if old not in s:
        print(f"[fail] {path.relative_to(ROOT)}: anchor not found")
        sys.exit(1)
    path.write_text(s.replace(old, new, 1))
    print(f"[done] {path.relative_to(ROOT)}")
    return True


def main() -> int:
    if not (CVA6 / "core" / "cva6.sv").exists():
        print("CVA6 tree not found; run scripts/fetch_cva6.py first")
        return 1

    patch(CVA6 / "core" / "csr_regfile.sv",
          "    input logic time_irq_i,",
          "    input logic time_irq_i,\n"
          "    input logic [63:0] mtime_i,  // GPCP: CLINT mtime for the time CSR",
          "GPCP: CLINT mtime")

    patch(CVA6 / "core" / "csr_regfile.sv",
          "        // Counters and Timers\n        riscv::CSR_MCYCLE:",
          "        // Counters and Timers\n"
          "        riscv::CSR_TIME: csr_rdata = mtime_i;  // GPCP: time CSR backed by CLINT\n"
          "        riscv::CSR_MCYCLE:",
          "CSR_TIME: csr_rdata")

    patch(CVA6 / "core" / "cva6.sv",
          "    input logic time_irq_i,",
          "    input logic time_irq_i,\n    input logic [63:0] mtime_i,",
          "input logic [63:0] mtime_i")

    patch(CVA6 / "core" / "cva6.sv",
          "      .time_irq_i,\n",
          "      .time_irq_i,\n      .mtime_i,\n",
          "      .mtime_i,")

    patch(CVA6 / "corev_apu" / "src" / "ariane.sv",
          "  input  logic                         time_irq_i,   // timer interrupt in (async)",
          "  input  logic                         time_irq_i,   // timer interrupt in (async)\n"
          "  input  logic [63:0]                  mtime_i,      // GPCP: CLINT mtime",
          "GPCP: CLINT mtime")

    patch(CVA6 / "corev_apu" / "src" / "ariane.sv",
          "    .time_irq_i           ( time_irq_i                ),",
          "    .time_irq_i           ( time_irq_i                ),\n"
          "    .mtime_i              ( mtime_i                   ),",
          "    .mtime_i              ( mtime_i")

    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
