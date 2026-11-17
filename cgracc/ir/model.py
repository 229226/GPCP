"""机器模型：架构方提供给编译器的唯一描述。

五段契约（docs/porting.md）：
  time_model  时间怎么推进、单次运行多长
  compute     有哪些算子、几位宽、几拍、每拍能发几条
  store       存储容量 / 银行 / 端口 / 读写时序
  move        搬运引擎的粒度、并发、同步方式
  （发射器单独提供：arch/<name>/emitter.py）

这里的字段有两个义务：要么被生成器查询，要么被检查器验证。做不到的字段不要加。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

API_VERSION = 1


class ModelError(ValueError):
    """模型文件不合法（比"生成的调度不合法"更早，属于接入错误）。"""


@dataclass(frozen=True)
class Op:
    name: str
    inputs: int
    latency: int
    writes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Compute:
    units: int
    issue_per_unit_per_cycle: int
    registers: int
    acc: bool
    link: bool
    ops: dict[str, Op]
    private_registers: bool = False   # 每个 unit 有私有寄存器堆：寄存器依赖必须同 unit

    def op(self, name: str) -> Op:
        if name not in self.ops:
            raise ModelError(f"算子 '{name}' 不在模型里（可用：{', '.join(sorted(self.ops))}）")
        return self.ops[name]


@dataclass(frozen=True)
class Space:
    name: str
    banks: int
    words_per_bank: int
    pages: int
    read_ports_per_bank: int
    write_ports_per_bank: int
    read_timing: str
    write_timing: str
    broadcast: str

    def bank_of(self, addr: int) -> int:
        return addr // self.words_per_bank


@dataclass(frozen=True)
class Engine:
    name: str
    beat_bits: int
    max_burst_beats: int
    overlap_compute: bool
    targets: tuple[str, ...]
    sync: str


@dataclass(frozen=True)
class TimeModel:
    kind: str
    max_run_cycles: int
    run_overhead_cycles: int = 0      # 一次 run 的固定启动开销（软/硬启动 + DMA 编排）


@dataclass(frozen=True)
class MachineModel:
    api_version: int
    name: str
    time: TimeModel
    compute: Compute
    store: dict[str, Space] = field(default_factory=dict)
    move: dict[str, Engine] = field(default_factory=dict)

    def space(self, name: str) -> Space:
        if name not in self.store:
            raise ModelError(f"存储空间 '{name}' 不在模型里（可用：{', '.join(sorted(self.store))}）")
        return self.store[name]

    def summary(self) -> str:
        lines = [
            f"arch      : {self.name} (api v{self.api_version})",
            f"time      : {self.time.kind}, max_run_cycles={self.time.max_run_cycles}",
            f"compute   : {self.compute.units} units x {self.compute.issue_per_unit_per_cycle} op/cycle, "
            f"rf={self.compute.registers}, acc={self.compute.acc}, link={self.compute.link}",
            f"ops       : {', '.join(sorted(self.compute.ops))}",
        ]
        for s in self.store.values():
            lines.append(
                f"store     : {s.name} {s.banks}x{s.words_per_bank} words, pages={s.pages}, "
                f"ports R{s.read_ports_per_bank}/W{s.write_ports_per_bank}, "
                f"R={s.read_timing}, W={s.write_timing}, broadcast={s.broadcast}"
            )
        for e in self.move.values():
            lines.append(
                f"move      : {e.name} {e.beat_bits}bit x {e.max_burst_beats} beats, "
                f"overlap={e.overlap_compute}, sync={e.sync}, targets={','.join(e.targets)}"
            )
        return "\n".join(lines)


def _need(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise ModelError(f"{where} 缺少字段 '{key}'")
    return d[key]


def load_model(path: str | Path) -> MachineModel:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ModelError(f"{path}: 顶层必须是映射")

    ver = int(_need(raw, "api_version", "顶层"))
    if ver != API_VERSION:
        raise ModelError(f"api_version={ver}，编译器只支持 {API_VERSION}")

    tm = _need(raw, "time_model", "顶层")
    time = TimeModel(
        kind=str(_need(tm, "kind", "time_model")),
        max_run_cycles=int(_need(tm, "max_run_cycles", "time_model")),
        run_overhead_cycles=int(tm.get("run_overhead_cycles", 0)),
    )
    if time.max_run_cycles <= 0:
        raise ModelError("time_model.max_run_cycles 必须 > 0")

    c = _need(raw, "compute", "顶层")
    ops_raw = _need(c, "ops", "compute")
    ops: dict[str, Op] = {}
    for name, spec in ops_raw.items():
        spec = spec or {}
        ops[name] = Op(
            name=name,
            inputs=int(spec.get("inputs", 0)),
            latency=int(spec.get("latency", 1)),
            writes=tuple(spec.get("writes", ())),
        )
    if not ops:
        raise ModelError("compute.ops 不能为空")
    compute = Compute(
        units=int(_need(c, "units", "compute")),
        issue_per_unit_per_cycle=int(c.get("issue_per_unit_per_cycle", 1)),
        registers=int(_need(c, "registers", "compute")),
        acc=bool(c.get("acc", False)),
        link=bool(c.get("link", False)),
        ops=ops,
        private_registers=bool(c.get("private_registers", False)),
    )
    if compute.units <= 0 or compute.registers <= 0:
        raise ModelError("compute.units / registers 必须 > 0")

    store: dict[str, Space] = {}
    for name, s in _need(raw, "store", "顶层").get("spaces", {}).items():
        store[name] = Space(
            name=name,
            banks=int(_need(s, "banks", f"store.{name}")),
            words_per_bank=int(_need(s, "words_per_bank", f"store.{name}")),
            pages=int(s.get("pages", 1)),
            read_ports_per_bank=int(s.get("read_ports_per_bank", 1)),
            write_ports_per_bank=int(s.get("write_ports_per_bank", 1)),
            read_timing=str(s.get("read_timing", "combinational")),
            write_timing=str(s.get("write_timing", "next_cycle")),
            broadcast=str(s.get("broadcast", "none")),
        )
        if store[name].banks <= 0 or store[name].words_per_bank <= 0:
            raise ModelError(f"store.{name} 的 banks / words_per_bank 必须 > 0")

    move: dict[str, Engine] = {}
    for name, e in _need(raw, "move", "顶层").get("engines", {}).items():
        move[name] = Engine(
            name=name,
            beat_bits=int(_need(e, "beat_bits", f"move.{name}")),
            max_burst_beats=int(e.get("max_burst_beats", 1)),
            overlap_compute=bool(e.get("overlap_compute", False)),
            targets=tuple(e.get("targets", ())),
            sync=str(e.get("sync", "polling")),
        )

    return MachineModel(ver, str(_need(raw, "name", "顶层")), time, compute, store, move)
