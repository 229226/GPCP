"""落点搜索：生成候选 -> 合法性检查 -> 代价估计 -> 选最优。

候选维度（当前）：
  list    × {拓扑序, 关键路径序}
  modulo  × II ∈ [MII, MII + window)

每个候选都必须过 `verify.legality.check`；非法候选只出现在报告里，不会被选中。
这是"编译器不需要知道架构"的落点搜索起点：候选由模型参数生成，代价由模型参数计算。
"""
from __future__ import annotations

from dataclasses import dataclass

from .cost import Cost, estimate
from .ir.dfg import DFG
from .ir.model import MachineModel
from .ir.schedule import Schedule
from .sched.list_sched import ListScheduler
from .sched.modulo import ModuloScheduler
from .verify.legality import Violation, check


@dataclass(frozen=True)
class Candidate:
    label: str
    schedule: Schedule | None
    cost: Cost | None
    violations: tuple[Violation, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.schedule is not None and not self.violations and not self.error


@dataclass(frozen=True)
class SearchResult:
    best: Candidate | None
    candidates: tuple[Candidate, ...]


def _eval(label: str, sched: Schedule, dfg: DFG, model: MachineModel) -> Candidate:
    return Candidate(
        label=label,
        schedule=sched,
        cost=estimate(sched, dfg, model),
        violations=tuple(check(sched, dfg, model)),
    )


def search(dfg: DFG, model: MachineModel, ii_window: int = 4) -> SearchResult:
    cands: list[Candidate] = []

    for order in ("topo", "height"):
        label = f"list/{order}"
        try:
            cands.append(_eval(label, ListScheduler(order).schedule(dfg, model), dfg, model))
        except Exception as e:                      # 调度器抛错也算一个失败的候选
            cands.append(Candidate(label, None, None, (), f"{type(e).__name__}: {e}"))

    # modulo 只对"会重复执行"的 kernel 有意义（有回边或 trip_count > 1）；
    # 单次 DAG 上强行给 II 只会让标签失真。
    if dfg.back_edges or dfg.trip_count > 1:
        mii = ModuloScheduler().mii(dfg, model)
        for ii in range(mii, mii + max(ii_window, 1)):
            label = f"modulo/II={ii}"
            try:
                sched = ModuloScheduler(ii_min=ii, ii_cap=ii).schedule(dfg, model)
                cands.append(_eval(label, sched, dfg, model))
            except Exception as e:
                cands.append(Candidate(label, None, None, (), f"{type(e).__name__}: {e}"))

    legal = [c for c in cands if c.ok]
    legal.sort(key=lambda c: (c.cost.cycles, -c.cost.efficiency))
    return SearchResult(legal[0] if legal else None, tuple(cands))
