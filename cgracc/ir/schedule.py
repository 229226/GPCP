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
    ii: int | None = None             # modulo 调度的启动间隔；None = 非流水调度
    trips_per_run: int | None = None  # 一次 run 展开多少次迭代（None = DFG 的 trip_count）
    runs: int = 1                     # 主机需要重复执行几次这个 run

    def slot(self, nid: str) -> Slot:
        for s in self.slots:
            if s.node == nid:
                return s
        raise KeyError(f"Schedule 里没有节点 '{nid}'")

    def cycles(self) -> int:
        """kernel 跨度（不是循环总周期；总周期看 total_cycles）。"""
        return max((s.cycle for s in self.slots), default=-1) + 1

    def per_run_cycles(self, trip_count: int = 1) -> int:
        """单次 run 的周期数（主机重复 runs 次）。"""
        tpr = self.trips_per_run or max(trip_count, 1)
        span = self.cycles()
        if self.ii:
            total = (max(tpr, 1) - 1) * self.ii + span
        else:
            total = span * max(tpr, 1)
        return max(self.run_cycles, total)

    def total_cycles(self, trip_count: int = 1) -> int:
        """整段程序的总周期（含多次 run）。"""
        return max(self.runs, 1) * self.per_run_cycles(trip_count)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "run_cycles": self.run_cycles or self.cycles(),
            "ii": self.ii,
            "trips_per_run": self.trips_per_run,
            "runs": self.runs,
            "slots": [asdict(s) for s in self.slots],
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def plan_trips(trip: int, ii: int | None, span: int, max_run: int) -> tuple[int, int]:
    """把 trip 次迭代拆成 (每次 run 的迭代数, run 次数)。

    GPCP 没有硬件循环：一次 run 从第 0 行顺序执行，所以循环必须展开；展开后
    超过 max_run 就得由主机重复执行同一个 run。寄存器/spad 跨 run 保持，因此
    拆分要求 tpr 整除 trip —— 否则最后一次 run 会多执行几次迭代（对累加器是错的）。
    """
    trip = max(trip, 1)
    step = ii or span
    if step <= 0 or span > max_run:
        return trip, 1
    tpr_max = max(1, min(trip, (max_run - span) // step + 1))
    if tpr_max >= trip:
        return trip, 1
    for tpr in range(tpr_max, 0, -1):
        if trip % tpr == 0:
            return tpr, trip // tpr
    return 1, trip


def load_schedule(path: str | Path) -> Schedule:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    slots = tuple(Slot(**s) for s in raw.get("slots", []))
    ii = raw.get("ii")
    tpr = raw.get("trips_per_run")
    return Schedule(name=str(raw.get("name", "schedule")),
                    run_cycles=int(raw.get("run_cycles", 0)), slots=slots,
                    ii=None if ii is None else int(ii),
                    trips_per_run=None if tpr is None else int(tpr),
                    runs=int(raw.get("runs", 1)))
