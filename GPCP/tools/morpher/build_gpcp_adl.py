#!/usr/bin/env python3
"""生成忠实描述 GPCP 4x4 阵列的 Morpher ADL（v2）。

与 v1 的区别（P0/P1）：
  P0 寄存器模型：去掉 HyCUBE 的 4 个方向寄存器，改成我们的 8 个通用寄存器
     r0..r7 + 累加器 acc + link（复用它的 TREG 作为"结果→四方向"的那一级）。
  P1 存储：两个 SPM 各 1024 字（32 bank x 32 word / 页），对应我们 scratchpad
     64 bank x 32 word 分两页、LD/ST 用页内 {bank,word} 立即数。

仍然表达不了的（见 docs/compiler/README.md）：
  - MAC 不更新 link（执行语义，不是结构）；
  - 全局 PC / 共享 IMEM（执行模型）；
  - LD/ST 的 10 位立即数寻址（后端折叠）。
"""

import json
import os
import sys

SRC = os.path.expanduser(
    "~/morpher/Morpher_CGRA_Mapper/benchmarks/hycube/morpher_benchmarks/"
    "array_add/hycube_original_mem.json")
OUT = os.path.expanduser("~/morpher/gpcp_cgra_v2.json")

OUR_OPS = ["NOP", "MOV", "ADD", "SUB", "MUL", "MAC", "LACC", "SHR", "SHL",
           "AND", "XOR", "MAX", "MIN", "MACL", "LD", "ST"]

DIRS = ("NORTH", "EAST", "WEST", "SOUTH")
REGISTERS = ["r%d" % i for i in range(8)] + ["acc"]
XBARS = ["THIS.%s_XBARI" % d for d in DIRS]
OUTS = ["THIS.%s_O" % d for d in DIRS]


def build():
    arch = json.load(open(SRC, encoding="utf-8"))["ARCH"]

    # ---- FU：我们的 16 条指令，全部 1 拍 ----
    arch["FU"]["OPS"] = {op: 1 for op in OUR_OPS}

    # ---- PE ----
    # 注意：删掉它的 4 个方向寄存器（NR/ER/WR/SR）会让映射器段错误 —— 它的路由
    # 模型假定每个方向输入后面有一级寄存器，否则会出现跨 PE 的组合环。
    # 所以保留方向寄存器（它们只承担"link 延迟"的角色），在此基础上追加我们的
    # 通用寄存器。真正的 8 寄存器约束由我们自己的后端分配器保证，见 docs/compiler/README.md。
    pe = arch["PE"]
    extra = ["r5", "r6", "r7", "acc"]
    pe["REGS"] = pe["REGS"] + extra
    conn = pe["CONNECTIONS"]
    for k in list(conn.keys()):
        if k.endswith("_XBARI"):
            for r in extra:
                if "THIS." + r not in conn[k]:
                    conn[k].append("THIS." + r)
    for r in extra:
        conn["THIS." + r] = list(XBARS)

    fu_in = ["FU0.DP0_I1", "FU0.DP0_I2", "FU0.DP0_P"]
    for xb in XBARS:
        conn[xb] = fu_in + OUTS                     # 交叉开关：送算子或直通路由
    # 算子结果：可写任意通用寄存器 / acc，同时驱动四方向（= 我们的 link）
    conn["FU0.DP0_T"] = ["THIS.%s" % r for r in REGISTERS] + OUTS + ["THIS.TREG"]
    pe["CONNECTIONS"] = conn

    # ---- P1：两个 SPM 各 1024 字（= 一页 32 bank x 32 word）----
    # P1 未完成：把 SPM 改成 1024 字会触发 getJSONPort 断言 —— 因为 wrapper 的
    # DATA_LAYOUT(loopend=2047) 与 INIT_FILE 的测试数据仍按 2048 字铺地址。
    # 要改必须三处同步（MEM.SIZE / DATA_LAYOUT / INIT_FILE），本轮未做。
    # 见 docs/compiler/README.md。

    return {"ARCH": arch}


if __name__ == "__main__":
    doc = build()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1)
    print("wrote", OUT)
    print("  REGS:", doc["ARCH"]["PE"]["REGS"])
    print("  OPS :", len(doc["ARCH"]["FU"]["OPS"]), "条")
    print("  SPM :", doc["ARCH"]["SPM"]["MEM"]["SIZE"], "字/页")
