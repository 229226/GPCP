#!/usr/bin/env python3
"""scratchpad bank 分配器（开发计划书 L2-A）：区间图着色。

输入：每个值（叶子 / 汇）的生命区间 [def_cycle, last_use_cycle]。
输出：值名 -> bank（0..31）。

我们的 spad 自检模型：输入（叶子）在程序开始前由 DMA/CPU 一次性写入，整个运行
期间常驻；汇在运行结束时写回、由校验器读取。因此同一 bank 不能同时容纳两个值——
我们给叶子区间 [0, 程序末拍]、给汇区间 [st, 程序末拍]，使它们在着色意义下两两重叠，
强制分配不同 bank。分配器本身是通用的区间图着色，未来若运行时能按拍重灌叶子，
可放宽区间以复用 bank（见 docs/18 §L2-A）。

硬约束：bank <= 31（spad_idx 只保留 5 位），超了直接报错，不许静默降级。
兼容：spec 里显式写的 bank（hint）强制绑定，复现固定布局（如 BN/CN 的 m_k->k）。
"""
import sys

import target as _hw                            # 同目录：硬件真相只在 cgra/target.yaml
N_BANKS = _hw.load().banks


def _overlap(a, b):
    """半开区间 [a0,a1) 与 [b0,b1) 是否重叠。"""
    return a[0] < b[1] and b[0] < a[1]


def alloc_banks(liveness, hints=None, n_banks=N_BANKS):
    """liveness: {name: (def_cycle, last_use_cycle)}（只含叶子与汇）。
    hints:    {name: bank} 强制绑定（来自 YAML 显式 bank）。
    返回 {name: bank}。
    """
    if n_banks > 32:
        raise SystemExit(f"n_banks={n_banks} 超过 spad_idx 的 5 位上限（32）")
    hints = hints or {}
    occupied = {}          # bank -> [(start, end), ...]  半开区间
    assigned = {}
    # 1) 先放下强制绑定，并检测冲突（我们的 spad 模型下所有值重叠，不能共享 bank）
    for n, b in hints.items():
        if not (0 <= b <= 31):
            raise SystemExit(f"hint 给 {n} 的 bank {b} 越界（必须 0..31）")
        if b in assigned.values():
            raise SystemExit(f"hint 冲突：bank {b} 被多个值强制绑定"
                             f"（我们的 spad 模型下不能共享）")
        ds, de = liveness.get(n, (0, 0))
        occupied.setdefault(b, []).append((ds, de))
        assigned[n] = b
    # 2) 其余按起始拍贪心着色（最早开始优先，着色更稳定）
    rest = [n for n in liveness if n not in assigned]
    rest.sort(key=lambda n: (liveness[n][0], liveness[n][1]))
    for n in rest:
        ds, de = liveness[n]
        placed = False
        for b in range(n_banks):
            if any(_overlap((ds, de), o) for o in occupied.get(b, [])):
                continue
            assigned[n] = b
            occupied.setdefault(b, []).append((ds, de))
            placed = True
            break
        if not placed:
            raise SystemExit(
                f"bank 分配失败：{n} 在 {n_banks} 个 bank 内找不到不冲突的槽"
                f"（live [{ds},{de})，已用 bank={sorted(occupied)}）")
    return assigned
