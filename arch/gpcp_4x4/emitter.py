"""GPCP 4x4 的真实 IMEM 编码发射器。

把 Schedule 变成可以写进 IMEM 的 32-bit 指令字（每拍 16 个 PE，PE0 在低 32 位），
输出成 GPCP 仓库 `sw/soc/gen/*_prog.h` 同一格式的 C 头文件。

编码（`rtl/cgra/cgra_pkg.sv` 的 pack_inst）：
  [31] rsvd | [30:27] op | [26:23] src_a | [22:19] src_b | [18:16] dst | [15:0] imm

关于循环：GPCP 没有硬件循环计数器，一次 run 是从第 0 行顺序执行。因此
modulo 调度的 II < 跨度时必须**展开**：第 k 次迭代的操作放在 k*II + cycle，
微码长度 = (trip-1)*II + span。展开后同一 (cycle, unit) 不允许两条指令。
"""
from __future__ import annotations

from cgracc.emit.base import Emitter
from cgracc.ir.dfg import DFG, Node
from cgracc.ir.model import MachineModel
from cgracc.ir.schedule import Schedule

# 与 cgra_pkg.sv 的 OP_* 一一对应
OPS = {
    "nop": 0x0, "mov": 0x1, "add": 0x2, "sub": 0x3, "mul": 0x4, "mac": 0x5,
    "lacc": 0x6, "shr": 0x7, "shl": 0x8, "and": 0x9, "xor": 0xA, "max": 0xB,
    "min": 0xC, "macl": 0xD, "ld": 0xE, "st": 0xF,
}
# 操作数选择器（SRC_*）
SRC = {
    "r0": 0, "r1": 1, "r2": 2, "r3": 3, "r4": 4, "r5": 5, "r6": 6, "r7": 7,
    "n": 8, "e": 9, "s": 10, "w": 11, "zero": 12, "imm": 13, "acc": 14, "link": 15,
}
NOP_WORD = 0


class EmitError(ValueError):
    """DFG 里有无法编码成该 ISA 的东西。"""


def pack_inst(op: int, src_a: int, src_b: int, dst: int, imm: int) -> int:
    return (((op & 0xF) << 27) | ((src_a & 0xF) << 23) | ((src_b & 0xF) << 19)
            | ((dst & 0x7) << 16) | (imm & 0xFFFF))


def unpack_inst(word: int) -> dict:
    """反向解码（测试与调试用）。"""
    return {
        "op": (word >> 27) & 0xF,
        "src_a": (word >> 23) & 0xF,
        "src_b": (word >> 19) & 0xF,
        "dst": (word >> 16) & 0x7,
        "imm": word & 0xFFFF,
    }


class Gpcp4x4Emitter(Emitter):
    def emit(self, schedule: Schedule, dfg: DFG, model: MachineModel) -> str:
        units = model.compute.units
        span = schedule.cycles()
        step = schedule.ii or span
        trip = max(dfg.trip_count, 1)
        total = schedule.total_cycles(trip)
        if total > model.time.max_run_cycles:
            raise EmitError(
                f"展开后 {total} 拍超过一次 run 上限 {model.time.max_run_cycles}（需要拆 run）")
        if units != 16:
            raise EmitError(f"GPCP 4x4 的 IMEM 一行是 16 个 PE，模型里是 {units}")

        by_id = {n.id: n for n in dfg.nodes}
        grid = [[NOP_WORD] * units for _ in range(total)]

        # 存储访问点：把编译器选定的坐标作为宏导出，C 侧不再写死数字
        mem_pts: list[tuple[str, str, int]] = []
        for n in dfg.nodes:
            if n.space is not None and n.imm is not None:
                mem_pts.append((n.id, n.space, n.imm))

        for it in range(trip):
            for slot in schedule.slots:
                t = it * step + slot.cycle
                if t >= total:
                    raise EmitError(f"节点 '{slot.node}' 展开到第 {t} 拍，超出 {total}")
                if grid[t][slot.unit] != NOP_WORD:
                    raise EmitError(
                        f"(cycle {t}, unit {slot.unit}) 被两条指令占用（展开后冲突）")
                grid[t][slot.unit] = self._encode(by_id[slot.node], by_id)

        return self._header(dfg, schedule, total, units, grid, model, mem_pts)

    # ---------------- 单条指令 ----------------
    def _encode(self, node: Node, by_id: dict[str, Node]) -> int:
        op = OPS.get(node.op)
        if op is None:
            raise EmitError(f"算子 '{node.op}' 不在 GPCP ISA 里（{', '.join(sorted(OPS))}）")

        ins = list(node.inputs)
        src_a = self._sel(ins[0], by_id) if len(ins) > 0 else SRC["zero"]
        src_b = self._sel(ins[1], by_id) if len(ins) > 1 else SRC["zero"]

        dst = 0
        if node.dst:
            if not (node.dst.startswith("r") and node.dst[1:].isdigit()):
                raise EmitError(f"节点 '{node.id}' 的 dst='{node.dst}' 不是 r0..r7")
            dst = int(node.dst[1:])
            if dst > 7:
                raise EmitError(f"节点 '{node.id}' 的 dst={node.dst} 超出 r0..r7")

        imm = (node.imm or 0) & 0xFFFF
        return pack_inst(op, src_a, src_b, dst, imm)

    @staticmethod
    def _sel(name: str, by_id: dict[str, Node]) -> int:
        if name in SRC:
            return SRC[name]
        if name in by_id:
            producer = by_id[name]
            if producer.dst and producer.dst.startswith("r") and producer.dst[1:].isdigit():
                idx = int(producer.dst[1:])
                if idx <= 7:
                    return idx
            if producer.op == "macl":        # macl 的用途就是把结果发到 link
                return SRC["link"]
            if producer.op in ("mac", "lacc"):
                return SRC["acc"]
            raise EmitError(
                f"操作数 '{name}' 由节点 '{producer.id}'（{producer.op}）产生，"
                f"但它没有写 r0..r7，无法作为源")
        raise EmitError(
            f"操作数 '{name}' 无法编码：只支持 r0..r7 / n / e / s / w / zero / imm / acc / link 或节点 id")

    # ---------------- 输出 ----------------
    @staticmethod
    def _header(dfg: DFG, schedule: Schedule, total: int, units: int,
                grid: list[list[int]], model: MachineModel,
                mem_pts: list[tuple[str, str, int]]) -> str:
        sym = "".join(c if c.isalnum() else "_" for c in dfg.name).lower()
        macro = sym.upper()
        lines = [
            "/* 由 cgracc 生成，勿手改。",
            f" * DFG {dfg.name}：{len(dfg.nodes)} 个算子，"
            f"span {schedule.cycles()}，II {schedule.ii or '-'}，"
            f"迭代 {dfg.trip_count} 次 → 展开 {total} 拍 x {units} PE。",
            " * 编码：pack_inst(op, src_a, src_b, dst, imm)；PE0 在每行低 32 位。 */",
            f"#ifndef {macro}_PROG_H",
            f"#define {macro}_PROG_H",
            "",
            f"#define {macro}_PROG_CYCLES {total}",
            f"#define {macro}_PROG_PES {units}",
        ]
        for nid, space, addr in mem_pts:
            sp = model.store.get(space)
            if sp is None:
                continue
            tag = "".join(c if c.isalnum() else "_" for c in nid).upper()
            # 导出的是**逻辑** bank/word（主机窗口/软件 ABI 的坐标）；
            # 物理映射由 RTL 的 spad_swz 在访问时完成。
            lines += [
                f"#define {macro}_{tag}_SPACE \"{space}\"",
                f"#define {macro}_{tag}_BANK {addr // sp.words_per_bank}",
                f"#define {macro}_{tag}_WORD {addr % sp.words_per_bank}",
            ]
        lines += [
            "",
            f"static const unsigned int {sym}_prog[{total * units}]",
            "    __attribute__((aligned(8))) = {",
        ]
        for row in grid:
            lines.append(",".join(f"0x{w:08x}u" for w in row) + ",")
        lines += ["};", "", f"#endif /* {macro}_PROG_H */", ""]
        return "\n".join(lines)
