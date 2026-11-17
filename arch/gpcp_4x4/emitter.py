"""GPCP 4x4 的参考发射器。

只输出人可读的 listing，**不是**真实 IMEM 编码（真实编码在 GPCP 仓库的
cgra/tools/gen_kernel_prog.py 里，需要 ISA 位域与 spad 布局）。它的价值是：
证明"Schedule -> 架构产物"这一段的接口成立，且能用于调试调度结果。
"""
from __future__ import annotations

from cgracc.emit.base import Emitter
from cgracc.ir.dfg import DFG
from cgracc.ir.model import MachineModel
from cgracc.ir.schedule import Schedule


class Gpcp4x4Emitter(Emitter):
    def emit(self, schedule: Schedule, dfg: DFG, model: MachineModel) -> str:
        nodes = {n.id: n for n in dfg.nodes}
        lines = [
            f"# {model.name} reference listing (not the real IMEM encoding)",
            f"# dfg={dfg.name} run_cycles={schedule.run_cycles} slots={len(schedule.slots)}",
            "",
            f"{'cyc':>4}  {'unit':>4}  {'op':<5} {'dst':<4} {'srcs':<18} {'addr':>6}",
        ]
        for s in sorted(schedule.slots, key=lambda x: (x.cycle, x.unit)):
            n = nodes.get(s.node)
            srcs = ",".join(n.inputs) if n else ""
            addr = "-" if s.addr is None else f"{s.space or '?'}:{s.addr}"
            lines.append(
                f"{s.cycle:>4}  {s.unit:>4}  {s.op:<5} {(s.dst or '-'):<4} {srcs:<18} {addr:>6}"
            )
        return "\n".join(lines) + "\n"
