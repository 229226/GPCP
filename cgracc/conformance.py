"""Conformance suite：任何新架构接入后都必须通过的用例集。

六个正向 + 四个负向：
  vec_add     纯计算 + 资源规模（run 长度必须等于 2）
  dot4        依赖链（run 至少 3 拍）
  fft8        存储读写 + 每 bank 端口不冲突（load/store 地址按 bank 摊开）
  matmul4     16 乘 + 12 加，验证绑定与组合
  bank_split  addr 0 与 addr W 同拍必须通过（卡住 bank 宽度，正向负例）
  loop_dot    modulo 调度：自回边 + trip_count，II 必须 ≥ 1 且总周期合规
  neg_bank    同拍同 bank 两次读 —— **必须**报 R6
  neg_run     超长 run —— **必须**报 R3
  neg_reg     目标寄存器越界一位 —— **必须**报 R5
  neg_page    array 与 dma 同拍同页 —— **必须**报 R9

注意：这是结构级 conformance（合法性、长度、端口、页），不是数值级。
数值对拍要等 DFG 带上算子语义、并接到黄金模型之后。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .ir.dfg import DFG, BackEdge, Node
from .ir.model import MachineModel
from .ir.schedule import Schedule, Slot
from .sched.list_sched import ListScheduler
from .sched.modulo import ModuloScheduler
from .verify.legality import check


@dataclass(frozen=True)
class CaseResult:
    name: str
    failures: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


def _legal(dfg: DFG, sched: Schedule, model: MachineModel, tag: str) -> list[str]:
    return [f"{tag}: {v}" for v in check(sched, dfg, model)]


def _need_spad(model: MachineModel) -> str | None:
    return "spad" if "spad" in model.store else None


def case_vec_add(model: MachineModel) -> list[str]:
    n = 2 * model.compute.units
    nodes = tuple(
        Node(f"v{i}", "add", (f"a{i}", f"b{i}"), dst=f"r{i % model.compute.registers}")
        for i in range(n)
    )
    dfg = DFG("conformance_vec_add", nodes)
    sched = ListScheduler().schedule(dfg, model)
    fails = _legal(dfg, sched, model, "vec_add")
    if sched.run_cycles != 2:
        fails.append(f"vec_add: run_cycles={sched.run_cycles}，期望 2（{n} 个独立算子 / {model.compute.units} 单元）")
    return fails


def case_dot4(model: MachineModel) -> list[str]:
    dfg = DFG("conformance_dot4", (
        Node("p0", "mul", ("a", "b"), dst="r0"),
        Node("p1", "mul", ("c", "d"), dst="r1"),
        Node("acc", "add", ("p0", "p1"), dst="r2"),
        Node("sq", "mul", ("acc", "acc"), dst="r3"),
    ))
    sched = ListScheduler().schedule(dfg, model)
    fails = _legal(dfg, sched, model, "dot4")
    if sched.run_cycles < 3:
        fails.append(f"dot4: run_cycles={sched.run_cycles}，依赖链至少 3 拍")
    return fails


def case_fft8(model: MachineModel) -> list[str]:
    spad = _need_spad(model)
    if spad is None:
        return ["fft8: 模型没有 spad 空间（存储用例无法执行）"]
    w = model.store[spad].words_per_bank
    nodes: list[Node] = []
    for k in range(4):
        ra, rb = 2 * k, 2 * k + 1
        nodes += [
            Node(f"l{k}a", "ld", imm=ra * w, dst="r0", space=spad),
            Node(f"l{k}b", "ld", imm=rb * w, dst="r1", space=spad),
            Node(f"s{k}", "add", (f"l{k}a", f"l{k}b"), dst="r2"),
            Node(f"d{k}", "sub", (f"l{k}a", f"l{k}b"), dst="r3"),
            Node(f"t{k}a", "st", (f"s{k}",), imm=ra * w, space=spad),
            Node(f"t{k}b", "st", (f"d{k}",), imm=rb * w, space=spad),
        ]
    dfg = DFG("conformance_fft8", tuple(nodes))
    sched = ListScheduler().schedule(dfg, model)
    fails = _legal(dfg, sched, model, "fft8")
    if sched.run_cycles < 3:
        fails.append(f"fft8: run_cycles={sched.run_cycles}，load→alu→store 至少 3 拍")
    return fails


def case_matmul4(model: MachineModel) -> list[str]:
    nodes: list[Node] = []
    for i in range(4):
        for j in range(4):
            prev: str | None = None
            for k in range(4):
                mul = f"m{i}{j}_{k}"
                nodes.append(Node(mul, "mul", (f"a{i}{k}", f"b{k}{j}"), dst="r0"))
                if prev is None:
                    prev = mul
                else:
                    acc = f"s{i}{j}_{k}"
                    nodes.append(Node(acc, "add", (prev, mul), dst="r1"))
                    prev = acc
    dfg = DFG("conformance_matmul4", tuple(nodes))
    sched = ListScheduler().schedule(dfg, model)
    fails = _legal(dfg, sched, model, "matmul4")
    if len(sched.slots) != len(nodes):
        fails.append(f"matmul4: 调度了 {len(sched.slots)} 个操作，期望 {len(nodes)}")
    return fails


def case_neg_bank_conflict(model: MachineModel) -> list[str]:
    if _need_spad(model) is None:
        return ["neg_bank: 模型没有 spad 空间"]
    dfg = DFG("conformance_neg_bank", (
        Node("a", "ld", imm=0, dst="r0", space="spad"),
        Node("b", "ld", imm=1, dst="r1", space="spad"),
    ))
    # 地址 0 / 1 同属 bank 0，同拍两次读 -> 必须被拒绝
    sched = Schedule("conformance_neg_bank", run_cycles=1, slots=(
        Slot("a", "ld", unit=0, cycle=0, dst="r0", addr=0, space="spad"),
        Slot("b", "ld", unit=1, cycle=0, dst="r1", addr=1, space="spad"),
    ))
    rules = {v.rule for v in check(sched, dfg, model)}
    if "R6" not in rules:
        return [f"neg_bank: 期望 R6，实际报告 {sorted(rules) or '无违规'}"]
    return []


def case_neg_run_too_long(model: MachineModel) -> list[str]:
    dfg = DFG("conformance_neg_run", (Node("n", "mov", ("a",), dst="r0"),))
    cyc = model.time.max_run_cycles
    sched = Schedule("conformance_neg_run", run_cycles=cyc + 1, slots=(
        Slot("n", "mov", unit=0, cycle=cyc, dst="r0"),
    ))
    rules = {v.rule for v in check(sched, dfg, model)}
    if "R3" not in rules:
        return [f"neg_run: 期望 R3，实际报告 {sorted(rules) or '无违规'}"]
    return []


def case_loop_dot(model: MachineModel) -> list[str]:
    """modulo 调度：mul + 自回边累加（dot product 的一个循环体）。"""
    trip = 64
    if trip > model.time.max_run_cycles:
        trip = max(2, model.time.max_run_cycles // 2)
    dfg = DFG("conformance_loop_dot", (
        Node("mul", "mul", ("x", "c"), dst="r0"),
        Node("acc", "add", ("acc", "mul"), dst="r1"),
    ), back_edges=(BackEdge("acc", "acc", 1),), trip_count=trip)
    sched = ModuloScheduler().schedule(dfg, model)
    fails = _legal(dfg, sched, model, "loop_dot")
    if not sched.ii or sched.ii < 1:
        fails.append(f"loop_dot: II={sched.ii}，modulo 调度必须给出 II ≥ 1")
    if sched.run_cycles > model.time.max_run_cycles:
        fails.append(f"loop_dot: 总周期 {sched.run_cycles} 超过 {model.time.max_run_cycles}")
    return fails


def case_bank_split(model: MachineModel) -> list[str]:
    """正向负例：addr 0 与 addr words_per_bank 属于不同 bank，同拍必须通过。

    它专门卡住"bank 宽度"这件事：如果检查器把 bank 宽度写死（而不是从模型读），
    或把 bank 算成 addr % W，这两个地址就会落到同一个 bank，误报 R6。
    """
    spad = _need_spad(model)
    if spad is None:
        return ["bank_split: 模型没有 spad 空间"]
    w = model.store[spad].words_per_bank
    dfg = DFG("conformance_bank_split", (
        Node("a", "ld", imm=0, dst="r0", space=spad),
        Node("b", "ld", imm=w, dst="r1", space=spad),
    ))
    sched = Schedule("conformance_bank_split", run_cycles=1, slots=(
        Slot("a", "ld", unit=0, cycle=0, dst="r0", addr=0, space=spad),
        Slot("b", "ld", unit=1, cycle=0, dst="r1", addr=w, space=spad),
    ))
    rules = {v.rule for v in check(sched, dfg, model)}
    if "R6" in rules:
        return [f"bank_split: addr 0 与 {w} 不同 bank，不应报 R6（实际 {sorted(rules)}）"]
    return []


def case_neg_reg(model: MachineModel) -> list[str]:
    regs = model.compute.registers
    bad = f"r{regs}"                       # 恰好越界一位
    dfg = DFG("conformance_neg_reg", (Node("n", "mov", ("a",), dst=bad),))
    sched = Schedule("conformance_neg_reg", run_cycles=1, slots=(
        Slot("n", "mov", unit=0, cycle=0, dst=bad),
    ))
    rules = {v.rule for v in check(sched, dfg, model)}
    if "R5" not in rules:
        return [f"neg_reg: 期望 R5（{bad} 越界），实际 {sorted(rules) or '无违规'}"]
    return []


def case_neg_page(model: MachineModel) -> list[str]:
    spad = _need_spad(model)
    if spad is None:
        return ["neg_page: 模型没有 spad 空间"]
    if model.store[spad].pages < 2:
        return [f"neg_page: {spad} 只有 {model.store[spad].pages} 页，无法验证双缓冲"]
    dfg = DFG("conformance_neg_page", (
        Node("a", "ld", imm=0, dst="r0", space=spad),
        Node("w", "st", ("x",), imm=1, space=spad),
    ))
    # array 与 dma 同拍、同空间、**同页** -> 必须被拒绝
    sched = Schedule("conformance_neg_page", run_cycles=1, slots=(
        Slot("a", "ld", unit=0, cycle=0, dst="r0", addr=0, space=spad,
             page=0, agent="array"),
        Slot("w", "st", unit=1, cycle=0, addr=1, space=spad, page=0, agent="dma"),
    ))
    rules = {v.rule for v in check(sched, dfg, model)}
    if "R9" not in rules:
        return [f"neg_page: 期望 R9（同页冲突），实际 {sorted(rules) or '无违规'}"]
    return []


CASES: tuple[tuple[str, Callable[[MachineModel], list[str]]], ...] = (
    ("vec_add", case_vec_add),
    ("dot4", case_dot4),
    ("fft8", case_fft8),
    ("matmul4", case_matmul4),
    ("bank_split", case_bank_split),
    ("loop_dot", case_loop_dot),
    ("neg_bank", case_neg_bank_conflict),
    ("neg_run", case_neg_run_too_long),
    ("neg_reg", case_neg_reg),
    ("neg_page", case_neg_page),
)


def run_all(model: MachineModel) -> list[CaseResult]:
    out: list[CaseResult] = []
    for name, fn in CASES:
        try:
            out.append(CaseResult(name, tuple(fn(model))))
        except Exception as e:                      # 调度器抛错也算失败
            out.append(CaseResult(name, (f"{name}: 异常 {type(e).__name__}: {e}",)))
    return out
