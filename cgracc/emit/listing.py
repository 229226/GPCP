"""通用 listing 发射器（架构无关的调试用后端）。

同族架构（静态同步拍、算子集相同）可以直接复用它：`arch/<name>/` 里
不放 emitter.py 时，注册表会自动回退到这里。它不是真实微码编码，
只是把 Schedule 打印成人能读的拍×单元表。
"""
from __future__ import annotations

from ..ir.dfg import DFG
from ..ir.model import MachineModel
from ..ir.schedule import Schedule
from .base import Emitter


class ListingEmitter(Emitter):
    def emit(self, schedule: Schedule, dfg: DFG, model: MachineModel) -> str:
        nodes = {n.id: n for n in dfg.nodes}
        lines = [
            f"# {model.name} reference listing (not the real machine encoding)",
            f"# dfg={dfg.name} run_cycles={schedule.run_cycles} slots={len(schedule.slots)}",
            "",
            f"{'cyc':>4}  {'unit':>4}  {'op':<5} {'dst':<4} {'srcs':<18} {'addr':>8}",
        ]
        for s in sorted(schedule.slots, key=lambda x: (x.cycle, x.unit)):
            n = nodes.get(s.node)
            srcs = ",".join(n.inputs) if n else ""
            addr = "-" if s.addr is None else f"{s.space or '?'}:{s.addr}"
            lines.append(
                f"{s.cycle:>4}  {s.unit:>4}  {s.op:<5} {(s.dst or '-'):<4} {srcs:<18} {addr:>8}"
            )
        return "\n".join(lines) + "\n"
