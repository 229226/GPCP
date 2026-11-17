"""Schedule：时间展开后的调度结果。

一个 Slot = "第 cycle 拍，第 unit 个执行单元，执行节点 node 的算子"。
物理地址只在这里出现（addr 指向 store 里的某个空间）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Slot:
    node: str
    op: str
    unit: int
    cycle: int
    dst: str | None = None
    addr: int | None = None
    space: str | None = None          # 这条 LD/ST 访问哪个存储空间
    page: int | None = None           # 双缓冲页号；None = 不涉及页
    agent: str = "array"              # 谁发起这次访问：array / dma（页分离规则用）


@dataclass(frozen=True)
class Schedule:
    name: str
    run_cycles: int = 0
    slots: tuple[Slot, ...] = field(default_factory=tuple)

    def slot(self, nid: str) -> Slot:
        for s in self.slots:
            if s.node == nid:
                return s
        raise KeyError(f"Schedule 里没有节点 '{nid}'")

    def cycles(self) -> int:
        return max((s.cycle for s in self.slots), default=-1) + 1

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "run_cycles": self.run_cycles or self.cycles(),
            "slots": [asdict(s) for s in self.slots],
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_schedule(path: str | Path) -> Schedule:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    slots = tuple(Slot(**s) for s in raw.get("slots", []))
    return Schedule(name=str(raw.get("name", "schedule")),
                    run_cycles=int(raw.get("run_cycles", 0)), slots=slots)
