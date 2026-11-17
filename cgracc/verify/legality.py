"""合法性检查器：Schedule + DFG + MachineModel -> 违规列表。

所有规则都从 model 读参数，检查器本身不含任何架构常量。
新增一个架构时，只要模型描述得对，这套规则就是该架构的"可做/不可做"判据。

规则编号（违规信息里会带）：
  R1 算子不在模型里
  R2 执行单元越界
  R3 拍号越界 / run 超长
  R4 同一 unit 同一拍发射数超限
  R5 目标寄存器越界（rN 的 N < 模型寄存器数）
  R6 存储端口超限（每拍每 bank 的读/写）
  R7 依赖顺序（生产者延迟未满足）
  R8 DFG 节点覆盖（遗漏 / 重复 / 算子不匹配）
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..ir.dfg import DFG
from ..ir.model import MachineModel
from ..ir.schedule import Schedule


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str
    node: str | None = None

    def __str__(self) -> str:
        where = f" [{self.node}]" if self.node else ""
        return f"{self.rule}{where}: {self.message}"


def check(schedule: Schedule, dfg: DFG, model: MachineModel) -> list[Violation]:
    out: list[Violation] = []

    # R8: 覆盖性
    seen: dict[str, int] = defaultdict(int)
    for s in schedule.slots:
        seen[s.node] += 1
    for n in dfg.nodes:
        c = seen.get(n.id, 0)
        if c == 0:
            out.append(Violation("R8", "DFG 节点没有出现在调度里", n.id))
        elif c > 1:
            out.append(Violation("R8", f"节点被调度了 {c} 次", n.id))
        else:
            s = schedule.slot(n.id)
            if s.op != n.op:
                out.append(Violation("R8", f"调度里的算子 '{s.op}' 与 DFG 的 '{n.op}' 不一致", n.id))
    for nid in seen:
        if nid not in {n.id for n in dfg.nodes}:
            out.append(Violation("R8", "调度里出现了 DFG 之外的节点", nid))

    # R1/R2/R3/R5/R6
    issue: dict[tuple[int, int], int] = defaultdict(int)
    rd: dict[tuple[int, str, int], int] = defaultdict(int)
    wr: dict[tuple[int, str, int], int] = defaultdict(int)

    for s in schedule.slots:
        # R1
        try:
            op = model.compute.op(s.op)
        except Exception as e:  # ModelError
            out.append(Violation("R1", str(e), s.node))
            continue

        # R2
        if not (0 <= s.unit < model.compute.units):
            out.append(Violation("R2", f"unit={s.unit} 越界（0..{model.compute.units - 1}）", s.node))

        # R3
        if s.cycle < 0 or s.cycle >= model.time.max_run_cycles:
            out.append(Violation(
                "R3", f"cycle={s.cycle} 越界（0..{model.time.max_run_cycles - 1}）", s.node))

        # R4
        issue[(s.unit, s.cycle)] += 1

        # R5
        if s.dst and s.dst.startswith("r") and s.dst[1:].isdigit():
            idx = int(s.dst[1:])
            if idx >= model.compute.registers:
                out.append(Violation(
                    "R5", f"目标寄存器 {s.dst} 越界（只有 r0..r{model.compute.registers - 1}）", s.node))

        # R6
        if "mem" in op.writes:
            if s.space is None or s.addr is None:
                out.append(Violation("R6", "写存储的算子缺少 space/addr", s.node))
            elif s.space in model.store:
                wr[(s.cycle, s.space, model.store[s.space].bank_of(s.addr))] += 1
        elif s.space is not None:
            if s.addr is None:
                out.append(Violation("R6", "读存储的算子缺少 addr", s.node))
            elif s.space in model.store:
                rd[(s.cycle, s.space, model.store[s.space].bank_of(s.addr))] += 1

    for (unit, cyc), n in issue.items():
        if n > model.compute.issue_per_unit_per_cycle:
            out.append(Violation(
                "R4", f"unit {unit} 在第 {cyc} 拍发射了 {n} 条（上限 {model.compute.issue_per_unit_per_cycle}）"))

    for (cyc, space, bank), n in rd.items():
        cap = model.store[space].read_ports_per_bank
        if n > cap:
            out.append(Violation(
                "R6", f"{space} 第 {cyc} 拍 bank {bank} 读了 {n} 次（上限 {cap}）"))
    for (cyc, space, bank), n in wr.items():
        cap = model.store[space].write_ports_per_bank
        if n > cap:
            out.append(Violation(
                "R6", f"{space} 第 {cyc} 拍 bank {bank} 写了 {n} 次（上限 {cap}）"))

    # R3: run 长度
    run = max(schedule.run_cycles, schedule.cycles())
    if run > model.time.max_run_cycles:
        out.append(Violation(
            "R3", f"run 长度 {run} 超过上限 {model.time.max_run_cycles}"))

    # R7: 依赖顺序（生产者延迟）
    ids = {n.id for n in dfg.nodes}
    for n in dfg.nodes:
        try:
            consumer = schedule.slot(n.id)
        except KeyError:
            continue
        for src in n.inputs:
            if src not in ids:
                continue
            try:
                prod = schedule.slot(src)
            except KeyError:
                continue
            lat = model.compute.op(prod.op).latency
            if prod.cycle + lat > consumer.cycle:
                out.append(Violation(
                    "R7",
                    f"节点 '{n.id}' 在第 {consumer.cycle} 拍读 '{src}'，"
                    f"但它第 {prod.cycle} 拍才发出、延迟 {lat} 拍", n.id))

    return out
