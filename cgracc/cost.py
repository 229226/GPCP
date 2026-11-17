"""代价模型：把 Schedule 折算成周期代价。

口径与检查器 R3 完全一致（总周期 = max(run_cycles, 流水/非流水折算)），
再加上模型里的 run 启动开销。搜索器用它给候选方案排序。

现在只估周期；面积/能耗要等 emitter 能给出资源占用后再加。
"""
from __future__ import annotations

from dataclasses import dataclass

from .ir.dfg import DFG
from .ir.model import MachineModel
from .ir.schedule import Schedule


@dataclass(frozen=True)
class Cost:
    cycles: int            # 总周期（含启动开销）
    compute_cycles: int    # 纯计算/循环周期
    overhead: int          # run 启动开销（来自模型）
    span: int              # kernel 跨度
    ii: int | None
    ops: int               # 一次迭代的操作数
    slots_used: int        # 占用的 (unit, kernel 槽位) 数
    efficiency: float      # 占用率 = ops / (units × 槽位数)

    def line(self) -> str:
        ii = f"{self.ii}" if self.ii else "-"
        return (f"{self.cycles:>6} cyc  (compute {self.compute_cycles} + ovh {self.overhead})"
                f"  span {self.span}  II {ii}  ops {self.ops}  eff {self.efficiency:.1%}")


def estimate(schedule: Schedule, dfg: DFG, model: MachineModel) -> Cost:
    span = schedule.cycles()
    compute = schedule.total_cycles(dfg.trip_count)
    overhead = model.time.run_overhead_cycles
    kernel = schedule.ii or span or 1
    used = len({(s.unit, s.cycle % kernel) for s in schedule.slots})
    denom = model.compute.units * kernel
    ops = len(schedule.slots)
    return Cost(
        cycles=compute + overhead,
        compute_cycles=compute,
        overhead=overhead,
        span=span,
        ii=schedule.ii,
        ops=ops,
        slots_used=used,
        efficiency=(ops / denom) if denom else 0.0,
    )
