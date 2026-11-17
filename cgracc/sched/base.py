"""调度器接口。

编译器核心只认这个接口：给它 (DFG, MachineModel)，还它一个 Schedule。
换执行模型 = 换一个实现，不改调用方。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..ir.dfg import DFG
from ..ir.model import MachineModel
from ..ir.schedule import Schedule


class Scheduler(ABC):
    name: str = "abstract"

    @abstractmethod
    def schedule(self, dfg: DFG, model: MachineModel) -> Schedule:
        """生成一个调度；是否合法由 verify.legality.check 判定，调度器自己不做保证。"""
        raise NotImplementedError
