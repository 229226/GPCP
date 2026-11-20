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
"""硬件闭环检查：cgracc 生成的 IMEM 编码在真实 RTL 上算对了吗？

sw/soc/bench/cgracc_dot_bm.c 把 cgracc 产物（examples/imem_dot.json ->
gen/cgracc_dot_prog.h）逐字写进 IMEM，预置每颗 PE 的 r0=3/r1=5，跑 3 拍，
回读 spad[bank0,word1]。期望 mul(3,5)=15 -> mul(15,5)=75 -> st。

判据：marker、IMEM 拍数、STATUS.done、spad 值、CYCLE_CNT、结尾字。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import parse_dump, words32            # noqa: E402

MARKER = 0x0FF1C1A1
EXPECT = 75            # (3*5)*5
CYCLES = 3


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "sim/soc/build/cgracc_dot_dump.txt"
    st = parse_dump(path)
    w = words32(st)
    code = st.get("exit_code", 0)
    ok = True

    def check(name: str, cond: bool, got: str) -> None:
        nonlocal ok
        print(f"[{'OK' if cond else 'FAIL'}] {name}: {got}")
        ok = ok and cond

    check("exit code 0x600D", code == 0x600D, hex(code))
    if len(w) < 6:
        print(f"[FAIL] dump 太短：{len(w)} words")
        return 2

    check("marker", w[0] == MARKER, hex(w[0]))
    check("IMEM cycles", w[1] == CYCLES, str(w[1]))
    check("STATUS.done", (w[2] & 0x2) != 0, hex(w[2]))
    check("spad[0,1] == 75", w[3] == EXPECT, str(w[3]))
    # CYCLE_CNT 停在 N-1（最后一拍 busy 清零，计数器不再递增）
    check("CYCLE_CNT == cycles-1", w[4] == CYCLES - 1, str(w[4]))
    check("epilogue 0x600D", w[5] == 0x600D, hex(w[5]))
    check("no timeout", st.get("timeout", 0) == 0, str(st.get("timeout", 0)))

    print(f"cgracc hardware loopback: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
