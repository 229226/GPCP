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

    def schedule(self, dfg: DFG, model: MachineModel) -> Schedule:
        order = dfg.topo_order()
        by_id = {n.id: n for n in dfg.nodes}
        when: dict[str, int] = {}

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
            while True:
                if cycle >= model.time.max_run_cycles:
                    raise RuntimeError(f"节点 '{node.id}' 在 {model.time.max_run_cycles} 拍内放不下")
                unit = self._pick_unit(i, cycle, model, issue)
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
                   issue: dict[tuple[int, int], int]) -> int | None:
        n = model.compute.units
        for k in range(n):
            unit = (i + k) % n
            if issue[(unit, cycle)] < model.compute.issue_per_unit_per_cycle:
                return unit
        return None
