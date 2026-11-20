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
"""Grade a GPCP Linux boot log (B3 acceptance).

The SoC model prints everything the kernel/OpenSBI write to the 16550 (which
the RTL forwards with $write) to stdout, so `make -C sim/soc linux` tees it to
build/linux_boot.log.  This checker turns that log into a single verdict:

    stage 0  nothing / no boot           (firmware or image problem)
    stage 1  OpenSBI banner              (firmware ran, platform OK)
    stage 2  "Linux version"             (kernel decompressed + entered)
    stage 3  "Run /init as init process"  (initramfs unpacked, init exec'd)
    stage 4  GPCP init banner            (our /init script ran)
    stage 5  "--- uname ---" + uname line (userspace syscalls work)
    stage 6  "MemTotal:" from /proc        (proc mounted, memory probe OK)
    stage 7  "=== CGRA TEST DONE ==="      (/dev/mem + mmap + CGRA from user)
    stage 8  "--- done ---" / tb exit       (clean poweroff / exit register)

Usage:
    tools/checks/check_linux.py build/linux_boot.log [--require 7] [--quiet]

Exit status is 0 iff the log reached the required stage.  Also reports the
simulated cycles and the kernel timestamp of the last line (so you can tell
"the log is short because the run is still going" from "it hung").
"""
import argparse
import re
import sys

STAGES = [
    (0, "boot", []),
    (1, "OpenSBI", ["OpenSBI v"]),
    (2, "kernel entered", ["Linux version "]),
    (3, "initramfs unpacked", ["Run /init as init process", "Run /sbin/init as init process"]),
    (4, "GPCP init script", ["=== GPCP Linux (CVA6 + CGRA) ==="]),
    (5, "userspace (uname)", ["--- uname ---"]),
    (6, "/proc + memory", ["MemTotal:"]),
    (7, "CGRA from user space", ["=== CGRA TEST DONE ==="]),
    (8, "clean exit", ["--- done ---"]),
]

# the three accumulator rows are also checked, because "the program ran" is not
# the same as "the CGRA produced the right numbers" (C = A * B from cgra_u.c).
EXPECT_ACC = {
    (0, 0): 2, (0, 1): 31, (0, 2): 24, (0, 3): -5,
    (1, 0): 26, (1, 1): 63, (1, 2): 14, (1, 3): 10,
    (2, 0): 12, (2, 1): 23, (2, 2): 24, (2, 3): 5,
    (3, 0): 23, (3, 1): 44, (3, 2): 19, (3, 3): 2,
}

ACC_RE = re.compile(r"C\[(\d)\]\[(\d)\] = (\d+)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("log", nargs="?", default="build/linux_boot.log")
    ap.add_argument("--require", type=int, default=7,
                    help="stage that must be reached (default 7 = CGRA from user space)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        with open(args.log, "r", errors="replace") as f:
            text = f.read()
    except OSError as e:
        print(f"[FAIL] cannot read {args.log}: {e}")
        return 1

    reached = 0
    # Two things corrupt the markers: the 16550 path wraps at 80 columns and
    # can break inside a word ("Linux ver / sion 6.6.52"), and the RTL may
    # interleave diagnostics ([trap]/[tb]/[pc] lines) into the console.  Strip
    # the diagnostics first, then match on the raw text, on a copy with
    # whitespace collapsed to one space and on a copy without whitespace.
    clean = re.sub(r"\[(trap|tb|pc)\][^\n]*", "", text)
    flat = re.sub(r"\s+", " ", clean)
    flat_ns = re.sub(r"\s+", "", clean)
    for stage, name, pats in STAGES:
        if stage and any((p in clean) or (p in flat) or
                         (re.sub(r"\s+", "", p) in flat_ns) for p in pats):
            reached = stage

    # last simulated cycle count and last kernel timestamp
    cyc = None
    m = re.findall(r"\[tb\] (?:exit|TIMEOUT) after (\d+) cycles", text)
    if m:
        cyc = int(m[-1])
    else:
        m = re.findall(r"\[tb\] (\d+) cycles\.\.\.", text)
        if m:
            cyc = int(m[-1])
    stamps = re.findall(r"\[    (\d+\.\d+)\]", text)
    ktime = stamps[-1] if stamps else None

    ac = re.findall(ACC_RE, text)
    acc_bad = []
    if ac:                                   # only judge the matrix if it ran
        for r, c, v in ac:
            want = EXPECT_ACC[(int(r), int(c))]
            if int(v) != want:
                acc_bad.append(f"C[{r}][{c}]={v} want {want}")

    if not args.quiet:
        for stage, name, _ in STAGES:
            print(f"  [{'x' if reached >= stage else ' '}] stage {stage}: {name}")
        if cyc is not None:
            print(f"  cycles: {cyc:,}" + (f"  (last kernel timestamp {ktime} s)" if ktime else ""))
        if ac:
            print(f"  CGRA accumulators: {len(ac) - len(acc_bad)}/{len(ac)} correct")
            for b in acc_bad[:6]:
                print(f"    {b}")

    ok = reached >= args.require and not acc_bad
    if ok:
        print(f"[ OK ] Linux boot: stage {reached} "
              f"({STAGES[reached][1]})" + (f", {cyc:,} cycles" if cyc else ""))
        return 0
    if reached < args.require:
        print(f"[FAIL] Linux boot stopped at stage {reached} "
              f"({STAGES[reached][1]}); need stage {args.require} "
              f"({STAGES[args.require][1]})"
              + (f" after {cyc:,} cycles" if cyc else ""))
    else:
        print(f"[FAIL] accumulators wrong: {len(acc_bad)}/{len(ac)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
