#!/usr/bin/env python3
"""布局契约生成器（AGENTS.md 4.19 的 C 侧一半）。

    cgra/target.yaml -> sw/include/gpcp_layout.h   全局几何 / 命名区域 / ABI 位宽
    kernel 编译结果  -> sw/soc/gen/<name>_layout.h     每个 leaf/sink 的 flat spad 索引

C 代码不再写 bank/word 数字，只 include 契约、引用宏。换 target 时 ABI 宏、
本地布局宏、DMA 描述符基址一起随编译产物重算。生成文件只在内容变化时重写。
"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
GLOBAL_PATH = os.path.join(ROOT, "sw", "include", "gpcp_layout.h")
KERNEL_DIR = os.path.join(ROOT, "sw", "soc", "gen")


def sym(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).upper()


def _write(path, text):
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            old = f.read()
    if old == text:
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return True


def write_global(T):
    L = ["/* 由 cgra/tools/layout_contract.py 生成，勿手改。",
         f" * 硬件目标：{T.name}；改几何只改 cgra/target.yaml 后重跑 `make kernels`。",
         " * C 侧的 spad 算术与命名区域只允许引用本文件。 */",
         "#ifndef GPCP_LAYOUT_H",
         "#define GPCP_LAYOUT_H",
         "",
         f'#define GPCP_LAYOUT_TARGET       "{T.name}"',
         f"#define GPCP_LAYOUT_NPES         {T.npes}u",
         f"#define GPCP_LAYOUT_BANKS        {T.banks}u",
         f"#define GPCP_LAYOUT_WORDS        {T.words}u",
         f"#define GPCP_LAYOUT_WORD_BITS    {T.word_bits}u",
         f"#define GPCP_LAYOUT_WORD_MASK    ((1u << GPCP_LAYOUT_WORD_BITS) - 1u)",
         f"#define GPCP_LAYOUT_BANK_MASK    ((1u << {T.bank_bits}u) - 1u)",
         f"#define GPCP_LAYOUT_IDX_BANK_MASK ((GPCP_LAYOUT_BANKS * 2u) - 1u)",
         f"#define GPCP_LAYOUT_PAGE_BANKS   {T.banks}u",
         f"#define GPCP_LAYOUT_FLAT_MAX     {T.flat_max}u",
         f"#define GPCP_LAYOUT_REGS         {T.regs}u",
         f"#define GPCP_LAYOUT_IMEM_CYCLES  {T.max_cycles}u",
         ""]
    for name, val in sorted(T.regions.items(), key=lambda kv: (kv[1], kv[0])):
        L.append(f"#define GPCP_LAYOUT_REGION_{sym(name):<12} {val}u")
    L += ["", "#endif /* GPCP_LAYOUT_H */", ""]
    return _write(GLOBAL_PATH, "\n".join(L))


def write_kernel(name, entries, T, note=""):
    """entries: {逻辑名: (flat_base, stride)}，宏 <SYM>_SPAD_<逻辑名>(inst)。"""
    S = sym(name)
    L = ["/* 由 cgra/tools/layout_contract.py 生成，勿手改。",
         f" * kernel: {name}；target: {T.name}。",
         " * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；",
         " * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。"]
    if note:
        L.append(f" * {note}")
    L += [" */",
          f"#ifndef {S}_LAYOUT_H",
          f"#define {S}_LAYOUT_H",
          "",
          f'#define {S}_LAYOUT_TARGET      "{T.name}"',
          f"#define {S}_LAYOUT_NPES        {T.npes}u",
          f"#define {S}_LAYOUT_WORDS       {T.words}u",
          f"#define {S}_LAYOUT_BANKS       {T.banks}u",
          ""]
    for key in sorted(entries):
        base, stride = entries[key]
        K = sym(key)
        L.append(f"#define {S}_SPAD_{K}_BASE   {base}u")
        L.append(f"#define {S}_SPAD_{K}_STRIDE {stride}u")
        L.append(f"#define {S}_SPAD_{K}(i)     ({base}u + {stride}u * (i))")
    if not entries:
        L.append("/* 该 kernel 的布局由专用调度器管理；C 侧用 gpcp_layout.h 的几何宏。 */")
    L += ["", f"#endif /* {S}_LAYOUT_H */", ""]
    return _write(os.path.join(KERNEL_DIR, f"{name.lower()}_layout.h"), "\n".join(L))
