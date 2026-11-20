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
"""Symbolize the PC samples printed by the SoC bench (--trace-pc / LINUX_TRACE).

The bench prints one line per sample:

    [pc] cyc=40000000 pc=0xffffffff80002e10

This turns those into "cycles -> function (file:line)", collapses runs of
identical PCs and flags a run that never changes in the silent part of a boot,
which is the quickest way to tell "slow but progressing" from "hung".

    tools/sym_pc.py build/linux_trace.log
    tools/sym_pc.py build/linux_trace.log --vmlinux third_party/linux-6.6.52/vmlinux
"""
import argparse
import os
import re
import subprocess
import sys

PC_RE = re.compile(r"\[pc\] cyc=(\d+) pc=0x([0-9a-fA-F]+)")
COLS = ("addr2line", "riscv64-linux-gnu-addr2line", "riscv64-unknown-elf-addr2line")


def find_addr2line():
    for c in COLS:
        from shutil import which
        if which(c):
            return which(c)
    return None


def symbolize(addr2line, vmlinux, addrs):
    if not addr2line or not vmlinux or not addrs:
        return {a: "" for a in addrs}
    p = subprocess.run([addr2line, "-f", "-e", vmlinux] + addrs,
                       capture_output=True, text=True)
    out = [l for l in p.stdout.splitlines() if l.strip()]
    res = {}
    for i, a in enumerate(addrs):
        if 2 * i + 1 < len(out):
            res[a] = f"{out[2 * i]} ({out[2 * i + 1]})"
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--vmlinux", default="third_party/linux-6.6.52/vmlinux")
    a = ap.parse_args()

    samples = [(int(c), p.lower()) for c, p in PC_RE.findall(open(a.log, errors="replace").read())]
    if not samples:
        print(f"no [pc] lines in {a.log} (run with LINUX_TRACE=N)")
        return 1

    # collapse consecutive identical PCs into runs
    runs = []
    for cyc, pc in samples:
        if runs and runs[-1][2] == pc:
            runs[-1][1] = cyc
        else:
            runs.append([cyc, cyc, pc])

    uniq = sorted({pc for _, _, pc in runs})
    sym = symbolize(find_addr2line(), a.vmlinux, uniq)

    print(f"{'cycles':>12} .. {'cycles':>12}   pc                 symbol")
    for c0, c1, pc in runs:
        where = sym.get(pc, "")
        print(f"{c0:12d} .. {c1:12d}  {pc}  {where}")
    longest = max(runs, key=lambda r: r[1] - r[0])
    span = longest[1] - longest[0]
    print(f"\nlongest run: {span:,} cycles at 0x{longest[2]}"
          + (f" -> {sym.get(longest[2])}" if sym.get(longest[2]) else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
