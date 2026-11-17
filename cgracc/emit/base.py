"""发射器接口：Schedule -> 该架构的微码 / 配置位流 / 汇编。

由架构方实现（arch/<name>/emitter.py）。编译器只负责调用与落盘。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..ir.dfg import DFG
from ..ir.model import MachineModel
from ..ir.schedule import Schedule


class Emitter(ABC):
    @abstractmethod
    def emit(self, schedule: Schedule, dfg: DFG, model: MachineModel) -> str:
        raise NotImplementedError
