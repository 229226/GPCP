#!/usr/bin/env python3
"""GPCP CGRA microcode assembler, kernel library and simulation driver (M0).

The toolchain mirrors the real flow used later for baseband kernels:

    kernel (Python)  ->  microcode words + PE preload + sample stream
                     ->  Verilator RTL simulation (driven over AXI4-Lite)
                     ->  dump + golden-model comparison

Usage:
    python3 tools/cgra.py test                 # build sim (if needed) and run all kernels
    python3 tools/cgra.py test --kernel fir16
    python3 tools/cgra.py build                # only build the Verilator model
"""

import argparse
import glob
import os
import random
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
SIM_DIR = ROOT / "sim" / "cgra"
SIM_BIN = SIM_DIR / "build" / "Vcgra_axi_lite"
BUILD_DIR = ROOT / "build" / "kernels"

CGRA_N = 4
NPES = CGRA_N * CGRA_N

# ---------------------------------------------------------------- instruction set
OPS = {
    "nop": 0, "mov": 1, "add": 2, "sub": 3, "mul": 4, "mac": 5, "lacc": 6,
    "shr": 7, "shl": 8, "and": 9, "xor": 10, "max": 11, "min": 12, "macl": 13,
}
SRC = {
    "r0": 0, "r1": 1, "r2": 2, "r3": 3, "r4": 4, "r5": 5, "r6": 6, "r7": 7,
    "n": 8, "e": 9, "s": 10, "w": 11, "zero": 12, "imm": 13, "acc": 14, "link": 15,
}


def sel(v) -> int:
    if isinstance(v, int):
        if not 0 <= v < 8:
            raise ValueError(f"register index out of range: {v}")
        return v
    return SRC[v]


def enc(op: str, a="zero", b="zero", dst=0, imm=0) -> int:
    word = (OPS[op] << 27) | (sel(a) << 23) | (sel(b) << 19) | ((dst & 7) << 16) | (imm & 0xFFFF)
    return word


class Program:
    """Spatial VLIW microcode: one instruction per (cycle, PE)."""

    def __init__(self, n: int = CGRA_N):
        self.n = n
        self.words: Dict[Tuple[int, int], int] = {}

    def set(self, cycle: int, pe: int, op: str, a="zero", b="zero", dst=0, imm=0):
        if not 0 <= pe < self.n * self.n:
            raise ValueError(f"PE out of range: {pe}")
        self.words[(cycle, pe)] = enc(op, a, b, dst, imm)

    def fill(self, cycle: int, op: str, a="zero", b="zero", dst=0, imm=0):
        for pe in range(self.n * self.n):
            self.set(cycle, pe, op, a, b, dst, imm)

    def build(self, cycles: int) -> List[int]:
        out = []
        for c in range(cycles):
            for pe in range(self.n * self.n):
                out.append(self.words.get((c, pe), enc("nop")))
        return out


@dataclass
class Kernel:
    name: str
    cycles: int
    words: List[int]
    preload: List[Tuple[int, int]] = field(default_factory=list)
    din: List[int] = field(default_factory=list)
    check: object = None


def sx16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# ---------------------------------------------------------------- kernels
def kernel_matmul(a: List[List[int]], b: List[List[int]], settle: int = 2) -> Kernel:
    """C = A x B on a 4x4 array: PE(i,j) holds A row i in r0..r3 and B col j in r4..r7."""
    prog = Program()
    for k in range(4):
        prog.fill(k, "mac", a=k, b=4 + k)
    cycles = 4 + settle

    preload = []
    for i in range(4):
        for j in range(4):
            pe = i * 4 + j
            for k in range(4):
                preload.append((0x1000 + pe * 32 + k * 4, a[i][k] & 0xFFFF))
            for k in range(4):
                preload.append((0x1000 + pe * 32 + (4 + k) * 4, b[k][j] & 0xFFFF))

    def check(st):
        errs = []
        for i in range(4):
            for j in range(4):
                pe = i * 4 + j
                exp = sum(sx16(a[i][k]) * sx16(b[k][j]) for k in range(4))
                got = st.get((pe, "acc"), None)
                exp32 = exp & 0xFFFFFFFF
                if got != exp32:
                    errs.append(f"PE{i}{j}: acc=0x{got:08x} expected 0x{exp32:08x}")
        return errs

    return Kernel("matmul4x4", cycles, prog.build(cycles), preload, [], check)


PATH = [
    (0, 0), (0, 1), (0, 2), (0, 3),
    (1, 3), (1, 2), (1, 1), (1, 0),
    (2, 0), (2, 1), (2, 2), (2, 3),
    (3, 3), (3, 2), (3, 1), (3, 0),
]


def _prev_dir(idx: int) -> str:
    if idx == 0:
        return "w"  # streaming boundary input
    pi, pj = PATH[idx - 1]
    ci, cj = PATH[idx]
    if pi == ci and pj == cj - 1:
        return "w"  # previous PE is our west neighbour
    if pi == ci and pj == cj + 1:
        return "e"
    if pi == ci - 1 and pj == cj:
        return "n"
    if pi == ci + 1 and pj == cj:
        return "s"
    raise ValueError(f"path is not contiguous at index {idx}")


def kernel_fir16(h: List[int], x: List[int]) -> Kernel:
    """16-tap FIR front-end: a sample stream is shifted along a snake path,
    every PE multiplies its own coefficient by the sample that reaches it.
    Even cycles shift (MOV), odd cycles accumulate (MAC)."""
    k = len(x)
    cycles = 2 * k
    prog = Program()

    for pos, (pi, pj) in enumerate(PATH):
        pe = pi * 4 + pj
        d = _prev_dir(pos)
        for c in range(k):
            prog.set(2 * c, pe, "mov", a=d, dst=1)
            prog.set(2 * c + 1, pe, "mac", a=0, b=1)

    preload = []
    for pos, (pi, pj) in enumerate(PATH):
        pe = pi * 4 + pj
        preload.append((0x1000 + pe * 32 + 0 * 4, h[pos] & 0xFFFF))

    din = []
    for c in range(k):
        din.append(x[c] & 0xFFFF)
        din.append(0)

    def check(st):
        errs = []
        for pos in range(16):
            pi, pj = PATH[pos]
            pe = pi * 4 + pj
            # last sample that reached this PE
            exp_r1 = x[k - 1 - pos] & 0xFFFF if (k - 1 - pos) >= 0 else 0
            got_r1 = st.get((pe, "r1"), None)
            if got_r1 != exp_r1:
                errs.append(f"PE{pi}{pj} r1=0x{got_r1:04x} expected 0x{exp_r1:04x} (path idx {pos})")
            # accumulated product: h[pos] * sum_{m=0}^{k-1-pos} x[m]
            window = sum(sx16(x[m]) for m in range(k - pos)) if k - pos > 0 else 0
            exp_acc = (sx16(h[pos]) * window) & 0xFFFFFFFF
            got_acc = st.get((pe, "acc"), None)
            if got_acc != exp_acc:
                errs.append(f"PE{pi}{pj} acc=0x{got_acc:08x} expected 0x{exp_acc:08x}")
        return errs

    return Kernel("fir16", cycles, prog.build(cycles), preload, din, check)


# ---------------------------------------------------------------- driver
def find_verilator() -> str:
    if os.environ.get("VERILATOR"):
        return os.environ["VERILATOR"]
    for p in sorted(glob.glob(os.path.expanduser("~/Downloads/oss-cad-suite*/oss-cad-suite/bin/verilator"))):
        return p
    for p in glob.glob("/opt/oss-cad-suite/bin/verilator"):
        return p
    return "verilator"


def clean_env() -> dict:
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)  # keep the host g++/libstdc++ from being shadowed
    vdir = str(Path(find_verilator()).parent)
    env["PATH"] = vdir + ":" + env.get("PATH", "")
    return env


def build_sim(force: bool = False) -> None:
    if SIM_BIN.exists() and not force:
        return
    print(f"[build] verilator model -> {SIM_BIN}")
    subprocess.run(["make", "-j8"], cwd=SIM_DIR, env=clean_env(), check=True)


def write_kernel(k: Kernel, d: Path) -> Dict[str, Path]:
    d.mkdir(parents=True, exist_ok=True)
    prog = d / "prog.hex"
    prog.write_text("\n".join(f"0x{w:08x}" for w in k.words) + "\n")
    pre = d / "preload.txt"
    pre.write_text("\n".join(f"0x{a:04x} 0x{v:08x}" for a, v in k.preload) + "\n")
    din = d / "din.txt"
    din.write_text("\n".join(f"0x{v & 0xFFFF:04x}" for v in k.din) + "\n")
    return {"prog": prog, "preload": pre, "din": din, "dump": d / "dump.txt", "dir": d}


def parse_dump(path: Path) -> dict:
    st = {}
    for line in path.read_text().splitlines():
        t = line.split()
        if not t:
            continue
        if t[0] == "cycles":
            st["cycles"] = int(t[1])
        elif t[0] == "pe":
            st[(int(t[1]), t[2])] = int(t[3], 16)
    return st


def run_kernel(k: Kernel, verbose: bool = True) -> bool:
    paths = write_kernel(k, BUILD_DIR / k.name)
    cmd = [str(SIM_BIN), "--prog", str(paths["prog"]), "--cycles", str(k.cycles),
           "--preload", str(paths["preload"]), "--dump", str(paths["dump"])]
    if k.din:
        cmd += ["--din", str(paths["din"])]
    r = subprocess.run(cmd, env=clean_env(), capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[FAIL] {k.name}: simulator error rc={r.returncode}\n{r.stderr}")
        return False
    st = parse_dump(paths["dump"])
    errs = k.check(st) if k.check else []
    if errs:
        print(f"[FAIL] {k.name}: {len(errs)} mismatch(es)")
        for e in errs[:10]:
            print("        " + e)
        return False
    if verbose:
        print(f"[ OK ] {k.name}: {k.cycles} cycles, {len(k.words)} microcode words, "
              f"{len(k.preload)} preload writes, {len(k.din)} samples")
    return True


# ---------------------------------------------------------------- test vectors
def build_matmul_kernel() -> Kernel:
    rnd = random.Random(7)
    a = [[rnd.randint(-8, 8) for _ in range(4)] for _ in range(4)]
    b = [[rnd.randint(-8, 8) for _ in range(4)] for _ in range(4)]
    return kernel_matmul(a, b)


def build_fir_kernel() -> Kernel:
    h = [1, -2, 3, -4, 5, -6, 7, -8, 9, -10, 11, -12, 13, -14, 15, -16]
    x = [3, -1, 4, -1, 5, -9, 2, 6]
    return kernel_fir16(h, x)


KERNELS = {"matmul": build_matmul_kernel, "fir16": build_fir_kernel}


def main() -> int:
    ap = argparse.ArgumentParser(description="GPCP CGRA M0 toolchain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build")
    p_build.add_argument("--force", action="store_true")
    p_test = sub.add_parser("test")
    p_test.add_argument("--kernel", default="all", choices=["all"] + list(KERNELS))
    p_test.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    if args.cmd == "build":
        build_sim(force=args.force)
        return 0

    build_sim(force=getattr(args, "rebuild", False))
    names = list(KERNELS) if args.kernel == "all" else [args.kernel]
    ok = True
    for n in names:
        ok &= run_kernel(KERNELS[n]())
    print("ALL TESTS PASSED" if ok else "SOME TESTS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
