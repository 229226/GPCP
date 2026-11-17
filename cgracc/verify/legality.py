"""合法性检查器：Schedule + DFG + MachineModel -> 违规列表。

所有规则都从 model 读参数，检查器本身不含任何架构常量。
新增一个架构时，只要模型描述得对，这套规则就是该架构的"可做/不可做"判据。

规则编号（违规信息里会带）：
  R1 算子不在模型里
  R2 执行单元越界
  R3 拍号越界 / run 超长
  R4 同一 unit 同一槽位（cycle % II）发射数超限
  R5 目标寄存器越界（rN 的 N < 模型寄存器数）
  R6 存储端口超限（每槽位每 bank 的读/写）
  R7 依赖顺序（生产者延迟未满足）
  R8 DFG 节点覆盖（遗漏 / 重复 / 算子不匹配）
  R9 页分离（双缓冲：同拍两个 agent 访问同一空间时必须异页；page 越界）
  R10 私有寄存器堆：有寄存器依赖的节点必须在同一个 unit
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

    # II（modulo 调度的启动间隔）：None = 非流水调度
    ii = schedule.ii
    if ii is not None and ii <= 0:
        out.append(Violation("R3", f"II={ii} 非法（必须 ≥ 1）"))
        ii = None
    if ii is not None and ii > model.time.max_run_cycles:
        out.append(Violation("R3", f"II={ii} 超过 run 上限 {model.time.max_run_cycles}"))

    def slot_of(cycle: int) -> int:
        """资源按 kernel 槽位计：modulo 调度下同一槽位每 II 拍复用一次。"""
        return cycle % ii if ii else cycle

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
    rd: dict[tuple[int, str, int], set[int]] = defaultdict(set)
    wr: dict[tuple[int, str, int], set[int]] = defaultdict(set)

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
        issue[(s.unit, slot_of(s.cycle))] += 1

        # R5
        if s.dst and s.dst.startswith("r") and s.dst[1:].isdigit():
            idx = int(s.dst[1:])
            if idx >= model.compute.registers:
                out.append(Violation(
                    "R5", f"目标寄存器 {s.dst} 越界（只有 r0..r{model.compute.registers - 1}）", s.node))

        # R6：按物理 bank 计端口；同字多读走广播，不占第二个端口
        if "mem" in op.writes:
            if s.space is None or s.addr is None:
                out.append(Violation("R6", "写存储的算子缺少 space/addr", s.node))
            elif s.space in model.store:
                sp = model.store[s.space]
                wr[(slot_of(s.cycle), s.space, sp.phys_bank(s.addr))].add(sp.phys_word(s.addr))
        elif s.space is not None:
            if s.addr is None:
                out.append(Violation("R6", "读存储的算子缺少 addr", s.node))
            elif s.space in model.store:
                sp = model.store[s.space]
                rd[(slot_of(s.cycle), s.space, sp.phys_bank(s.addr))].add(sp.phys_word(s.addr))

    for (unit, cyc), n in issue.items():
        if n > model.compute.issue_per_unit_per_cycle:
            out.append(Violation(
                "R4", f"unit {unit} 在槽位 {cyc} 发射了 {n} 条"
                      f"（上限 {model.compute.issue_per_unit_per_cycle}）"))

    for (cyc, space, bank), words in rd.items():
        cap = model.store[space].read_ports_per_bank
        if len(words) > cap:
            out.append(Violation(
                "R6", f"{space} 槽位 {cyc} 物理 bank {bank} 读了 {len(words)} 个不同字（上限 {cap}）"))
    for (cyc, space, bank), words in wr.items():
        cap = model.store[space].write_ports_per_bank
        if len(words) > cap:
            out.append(Violation(
                "R6", f"{space} 槽位 {cyc} 物理 bank {bank} 写了 {len(words)} 个不同字（上限 {cap}）"))

    # R3: 总周期 = (迭代数-1)*II + kernel 跨度（非流水时 = 跨度*迭代数）
    span = schedule.cycles()
    total = schedule.total_cycles(dfg.trip_count)
    if total > model.time.max_run_cycles:
        out.append(Violation(
            "R3", f"run 长度 {total} 超过上限 {model.time.max_run_cycles}"
                  f"（{dfg.trip_count} 次迭代，II={ii or '无'}，跨度 {span}）"))

    # R9: 页分离（双缓冲）。同拍、同一空间、两个 agent -> 必须异页。
    page_use: dict[tuple[int, str], dict[str, set[int]]] = defaultdict(
        lambda: defaultdict(set))
    for s in schedule.slots:
        if s.space is None or s.space not in model.store:
            continue
        sp = model.store[s.space]
        if s.page is not None and not (0 <= s.page < sp.pages):
            out.append(Violation(
                "R9", f"page={s.page} 越界（{s.space} 只有 0..{sp.pages - 1} 页）", s.node))
            continue
        if s.page is not None:
            page_use[(s.cycle, s.space)][s.agent].add(s.page)

    for (cyc, space), by_agent in page_use.items():
        if model.store[space].pages <= 1 or len(by_agent) < 2:
            continue
        common = set.intersection(*by_agent.values())
        if common:
            agents = ", ".join(f"{a}{sorted(p)}" for a, p in sorted(by_agent.items()))
            out.append(Violation(
                "R9", f"{space} 第 {cyc} 拍页冲突：{agents}（双缓冲必须异页）"))

    # R7: 依赖顺序（前向边用绝对拍；回边用距离×II 折算）
    ids = {n.id for n in dfg.nodes}
    back = dfg.back_edge_pairs()
    for n in dfg.nodes:
        try:
            consumer = schedule.slot(n.id)
        except KeyError:
            continue
        for src in n.inputs:
            if src not in ids or (src, n.id) in back:
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

    for be in dfg.back_edges:
        if ii is None:
            out.append(Violation(
                "R7", f"回边 {be.src}->{be.dst} 需要 modulo 调度（II 未设置）", be.dst))
            continue
        try:
            prod = schedule.slot(be.src)
            cons = schedule.slot(be.dst)
        except KeyError:
            continue
        lat = model.compute.op(prod.op).latency
        if prod.cycle + lat > cons.cycle + be.distance * ii:
            out.append(Violation(
                "R7",
                f"回边 {be.src}->{be.dst}（距离 {be.distance}）不满足："
                f"{prod.cycle}+{lat} > {cons.cycle}+{be.distance}×{ii}", be.dst))

    # R10: 私有寄存器堆 —— 有依赖的节点必须在同一个 unit
    if model.compute.private_registers:
        for n in dfg.nodes:
            try:
                cons = schedule.slot(n.id)
            except KeyError:
                continue
            for src in n.inputs:
                if src not in ids:
                    continue
                try:
                    prod = schedule.slot(src)
                except KeyError:
                    continue
                if prod.unit != cons.unit:
                    out.append(Violation(
                        "R10",
                        f"节点 '{n.id}' 在 unit {cons.unit} 读 '{src}'（unit {prod.unit}），"
                        f"私有寄存器堆不允许跨 unit 直连", n.id))

    return out
