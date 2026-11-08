#!/usr/bin/env python3
"""铁律：微码只许编译器生成，不许手写。

规则原文见 AGENTS.md 第 4 节第 18 条。唯一合法路径是

    cgra/kernels/<name>.yaml  ->  cgra/tools/gen_kernel_prog.py  ->  sw/soc/<name>_prog.h

生成的头部带 "由 cgra/tools/gen_kernel_prog.py 生成" 横幅。**任何**在 C 里用
gpcp_cgra_inst() 现拼指令的做法都算手写微码 —— 它绕过了 YAML、绕过了调度器、
绕过了逐值自检，是这类项目最容易腐烂的地方（同一个 polar node 程序曾被复制 7 份）。

本脚本在每次 verify 里跑，失败即中断。四项判定：

  1. sw/ 下出现 gpcp_cgra_inst() 而文件不在 ALLOW 白名单  -> 违规
  2. 白名单里的文件已清空或已删除（迁移完成却没删登记）    -> 违规（强制收缩）
  3. sw/soc/*_prog.h 缺生成横幅（旧路径产物）             -> 违规
  4. tools/cgra.py 里 ^def kernel_ 数量增长                -> 违规

白名单条目带迁移阶段与理由；总数钉在 PIN 上，只减不增：

    P1  已有编译器产物，纯重复，可直接清退
    P2  基础设施自测/示例程序，需要先写一份 YAML
    P3  编译器能力缺口：FFT 引擎（MAC/acc、每 PE 多实例、二维 spad 地址、>512 拍分块）
    P4  编译器能力缺口：多 kernel 融合（spad 传值），即 rxchain 的 127 拍镜像

用法：
    python3 tools/check_handprog.py          # 门禁
    python3 tools/check_handprog.py --list   # 打印每一项的明细
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "gpcp_cgra_inst"
# 合法的生成器：统一入口 + 两个兼容薄壳（它们内部 import gen_kernel_prog）。
BLESSED = ("gen_kernel_prog.py", "gen_bn_prog.py", "gen_cn_prog.py")

STAGES = {
    "P1": "已有编译器产物，纯重复",
    "P2": "自测/示例程序，需先写 YAML",
    "P3": "编译器缺 MAC/acc + 每 PE 多实例 + 二维地址",
    "P4": "编译器缺多 kernel 融合（spad 传值）",
}

# 文件 -> (阶段, 说明)。迁移完成一项就删一行，并把 PIN 减一。
ALLOW = {
    # ---- P3：编译器能力缺口（每 PE 时间复用 + 指令级分块 DFG） ----
    "sw/soc/fft_bm.c":         ("P3", "FFT 引擎 448 拍 stage + 336 拍 block（28 个蝶形 emit）"),
    "sw/soc/ssb_fft.inc":      ("P3", "同一 FFT 引擎，被 4 个 SSB 基准 include"),
    # ---- P4：编译器能力缺口 ----
    "sw/soc/rxchain_bm.c":     ("P4", "4 阶段融合 127 拍镜像（chNest/MMSE/16QAM/CN，95 个 emit）"),
}

# 白名单总数（只减不增）。迁移一项就减一，diff 里看得见。
PIN = 3

# 旧路径产物：由 tools/gen_prach_prog.py 从 tools/cgra.py 的手写 kernel 导出。
LEGACY_HEADERS = {}   # 旧路径头文件已全部清退（prach_prog.h 于 2026-11-08 删除）

# tools/cgra.py 的手写 kernel 函数数量（只减不增）。
PIN_KERNELS = 14


def rel(p):
    return str(p.relative_to(ROOT)).replace(os.sep, "/")


def scan_sources():
    hits = {}
    for p in sorted((ROOT / "sw").rglob("*")):
        if p.suffix not in (".c", ".inc"):
            continue
        n = p.read_text(errors="replace").count(TOKEN)
        if n:
            hits[rel(p)] = n
    return hits


def scan_headers():
    bad = []
    for p in sorted((ROOT / "sw" / "soc").glob("*_prog.h")):
        head = p.read_text(errors="replace")[:400]
        if any(b in head for b in BLESSED):
            continue
        bad.append(rel(p))
    return bad


def count_kernels():
    src = (ROOT / "tools" / "cgra.py").read_text(errors="replace")
    return len([l for l in src.splitlines()
                if l.startswith("def kernel_") and not l.startswith("def kernel_registry")])


def main(a):
    errs, warns = [], []
    hits = scan_sources()

    # 1) 新增手写微码
    for f, n in sorted(hits.items()):
        if f not in ALLOW:
            errs.append("%s: %d 处 %s() —— 不允许手写微码\n"
                        "       -> 写 cgra/kernels/<name>.yaml，跑 make kernels" % (f, n, TOKEN))

    # 2) 白名单收缩：登记了但已经没有调用点
    for f, (stage, why) in sorted(ALLOW.items()):
        if f not in hits:
            errs.append("%s: 白名单项已无手写微码（%s）—— 迁移完成，请删掉这一行并把 PIN 减一"
                        % (f, stage))

    # 3) 旧路径产物
    for h in scan_headers():
        if h in LEGACY_HEADERS:
            continue
        errs.append("%s: 缺生成横幅，不是 gen_kernel_prog.py 的产物 —— 禁止手工维护微码头文件" % h)
    for h, (stage, why) in sorted(LEGACY_HEADERS.items()):
        if not (ROOT / h).exists():
            errs.append("%s: 旧路径头文件已删除 —— 请从 LEGACY_HEADERS 移除登记" % h)

    # 4) 手写 kernel 不许增长
    nk = count_kernels()
    if nk > PIN_KERNELS:
        errs.append("tools/cgra.py: 手写 kernel 函数 %d 个 > 上限 %d —— 新 kernel 必须走 YAML"
                    % (nk, PIN_KERNELS))

    # 5) 白名单总预算
    if len(ALLOW) > PIN:
        errs.append("白名单 %d 项 > PIN %d —— 只许收缩，不许扩充" % (len(ALLOW), PIN))

    by_stage = {}
    for stage, _ in ALLOW.values():
        by_stage[stage] = by_stage.get(stage, 0) + 1
    total_calls = sum(hits.values())

    print("手写微码存量：%d 个文件 / %d 处调用点（PIN %d，只减不增）"
          % (len(hits), total_calls, PIN))
    print("  " + "   ".join("%s %s=%d" % (s, STAGES[s][:12], by_stage.get(s, 0))
                            for s in ("P1", "P2", "P3", "P4")))

    if a.list:
        for f, n in sorted(hits.items(), key=lambda kv: (ALLOW.get(kv[0], ("P9",))[0], kv[0])):
            stage, why = ALLOW.get(f, ("--", "未登记"))
            print("  %-3s %-28s %3d  %s" % (stage, f, n, why))

    for w in warns:
        print("WARN  %s" % w)
    if errs:
        for e in errs:
            print("[FAIL] %s" % e)
        print("[FAIL] 手写微码门禁：%d 项违规（规则见 AGENTS.md §4.18）" % len(errs))
        return 1

    print("[ OK ] 无新增手写微码；%d 项存量已登记，目标 0" % len(ALLOW))
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="逐项打印白名单明细")
    sys.exit(main(ap.parse_args()))
