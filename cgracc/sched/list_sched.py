"""参考调度器：列表调度（list scheduling）。

目标不是 QoR，而是给接口一个**能跑通的最小实现**：
按拓扑序遍历，每条操作放在"依赖满足 + 资源空闲"的最早一拍。

资源约束全部从 model 读：单元数、每拍发射数、bank 读写端口数。
"""
from __future__ import annotations

from collections import defaultdict

from ..ir.dfg import DFG
from ..ir.model import MachineModel
from ..ir.schedule import Schedule, Slot
from .base import Scheduler


class ListScheduler(Scheduler):
    name = "list"

    def __init__(self, order: str = "topo"):
        if order not in ("topo", "height"):
            raise ValueError(f"未知排序 '{order}'（topo / height）")
        self.order = order

    def schedule(self, dfg: DFG, model: MachineModel) -> Schedule:
        order = self._ordered(dfg, model)
        by_id = {n.id: n for n in dfg.nodes}
        when: dict[str, int] = {}

        # 私有寄存器堆：同一依赖分量的节点必须落在同一个 unit
        comp = dfg.components() if model.compute.private_registers else {}
        unit_of_comp: dict[str, int] = {}

        issue: dict[tuple[int, int], int] = defaultdict(int)
        rd: dict[tuple[str, int, int], int] = defaultdict(int)
        wr: dict[tuple[str, int, int], int] = defaultdict(int)
        slots: list[Slot] = []

        for i, node in enumerate(order):
            op = model.compute.op(node.op)

            # 依赖下界：所有节点输入的产出时刻 + 生产者延迟
            earliest = 0
            for src in node.inputs:
                if src in when:
                    src_op = model.compute.op(by_id[src].op)
                    earliest = max(earliest, when[src] + src_op.latency)

            space = node.space
            if space is None and "mem" in op.writes:
                space = next(iter(model.store), None)
            addr = node.imm if space is not None else None
            bank = model.store[space].bank_of(addr) if (space in model.store and addr is not None) else None

            cycle = earliest
            required = unit_of_comp.get(comp.get(node.id, "")) if comp else None
            while True:
                if cycle >= model.time.max_run_cycles:
                    raise RuntimeError(f"节点 '{node.id}' 在 {model.time.max_run_cycles} 拍内放不下")
                unit = self._pick_unit(i, cycle, model, issue, required)
                if unit is None:
                    cycle += 1
                    continue
                if bank is not None:
                    key = (space, cycle, bank)
                    if "mem" in op.writes:
                        if wr[key] >= model.store[space].write_ports_per_bank:
                            cycle += 1
                            continue
                    else:
                        if rd[key] >= model.store[space].read_ports_per_bank:
                            cycle += 1
                            continue
                break

            issue[(unit, cycle)] += 1
            if comp:
                unit_of_comp.setdefault(comp[node.id], unit)
            if bank is not None:
                key = (space, cycle, bank)
                (wr if "mem" in op.writes else rd)[key] += 1
            when[node.id] = cycle
            slots.append(Slot(
                node=node.id, op=node.op, unit=unit, cycle=cycle,
                dst=node.dst, addr=addr, space=space,
            ))

        run = max(when.values(), default=-1) + 1
        return Schedule(name=dfg.name, run_cycles=run, slots=tuple(slots))

    @staticmethod
    def _pick_unit(i: int, cycle: int, model: MachineModel,
                   issue: dict[tuple[int, int], int],
                   required: int | None = None) -> int | None:
        if required is not None:
            return required if issue[(required, cycle)] < model.compute.issue_per_unit_per_cycle else None
        n = model.compute.units
        for k in range(n):
            unit = (i + k) % n
            if issue[(unit, cycle)] < model.compute.issue_per_unit_per_cycle:
                return unit
        return None

    def _ordered(self, dfg: DFG, model: MachineModel) -> list:
        """拓扑序 / 关键路径高度序（后者通常把长依赖链排得更紧）。"""
        if self.order == "topo":
            return dfg.topo_order()
        succ = dfg.forward_edges()
        lat = {n.id: model.compute.op(n.op).latency for n in dfg.nodes}
        memo: dict[str, int] = {}

        def height(u: str) -> int:
            if u in memo:
                return memo[u]
            best = 0
            for s in succ.get(u, ()):
                best = max(best, height(s))
            memo[u] = lat[u] + best
            return memo[u]

        # 在拓扑序上做稳定排序：latency=0 的依赖仍保持"生产者在前"。
        topo = dfg.topo_order()
        return sorted(topo, key=lambda n: -height(n.id))
