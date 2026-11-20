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
"""SoC 自检 bench 的结果复核（tools/checks/check_new_kernels_hw.py）。

读 Verilator dump 的 0x80010000 结果区：
  [0] marker 'GKCN'  [1] errors  [2] cases  [3] array_runs  [4] 0x600D
  [8+s] got / [8+1024+s] want
判据：marker / 魔数正确、7 个 case 全跑、7 次阵列运行、C 侧 errors==0，
      且每个 (got, want) 逐值相等。
"""
import sys

MARKER = 0x4E4B4347
MAGIC = 0x600D
WANT_OFF = 1024
NCASES = 7
NSLOTS = 21 * 32 + 20 * 3      # 最后一个 case 占用的最大 slot


def main(path):
    words = []
    for line in open(path):
        p = line.split()
        if len(p) >= 3 and p[0] == "word" and p[2].startswith("0x"):
            w = int(p[2], 16)
            words.append(w & 0xFFFFFFFF)
            words.append((w >> 32) & 0xFFFFFFFF)
    need = 8 + WANT_OFF + NSLOTS
    if len(words) < need:
        print("[FAIL] dump 太短：%d 字，需要 %d 字" % (len(words), need))
        return 1
    bad = []
    if words[0] != MARKER:
        bad.append("marker 0x%08x != 0x%08x" % (words[0], MARKER))
    if words[4] != MAGIC:
        bad.append("magic 0x%08x != 0x%08x" % (words[4], MAGIC))
    if words[1] != 0:
        bad.append("C 侧 errors = %d（应为 0）" % words[1])
    if words[2] != NCASES:
        bad.append("cases = %d（应为 %d）" % (words[2], NCASES))
    if words[3] != NCASES:
        bad.append("array_runs = %d（应为 %d）" % (words[3], NCASES))
    diff = 0
    for s in range(NSLOTS):
        got = words[8 + s]
        want = words[8 + WANT_OFF + s]
        if got != want:
            diff += 1
            if diff <= 5:
                print("  slot %d: got 0x%08x want 0x%08x" % (s, got, want))
    if diff:
        bad.append("%d/%d 个汇值不一致" % (diff, NSLOTS))
    if bad:
        for b in bad:
            print("[FAIL]", b)
        return 1
    print("[ OK ] 新增 7 个 CGRA kernel 在 SoC 上逐值对账通过（%d 个汇值，7 次阵列运行）"
          % NSLOTS)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
