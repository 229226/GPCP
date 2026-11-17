"""真实 IMEM 编码发射器测试：编码字段、NOP 填充、循环展开、非法操作数。"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cgracc.archreg import load_arch                          # noqa: E402
from cgracc.ir.dfg import load_dfg                            # noqa: E402
from cgracc.sched.list_sched import ListScheduler             # noqa: E402
from cgracc.sched.modulo import ModuloScheduler               # noqa: E402
from arch.gpcp_4x4.emitter import EmitError, unpack_inst      # noqa: E402


def parse_words(header: str) -> list[int]:
    body = header.split("= {", 1)[1].split("};", 1)[0]
    return [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{8})u", body)]


class TestImemEmitter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model, cls.emitter = load_arch("gpcp_4x4", ROOT / "arch")

    def test_fields_and_nop_fill(self):
        dfg = load_dfg(ROOT / "examples" / "imem_dot.json")
        sched = ListScheduler().schedule(dfg, self.model)
        header = self.emitter.emit(sched, dfg, self.model)
        words = parse_words(header)
        self.assertEqual(len(words), sched.cycles() * 16)

        for slot in sched.slots:
            inst = unpack_inst(words[slot.cycle * 16 + slot.unit])
            node = dfg.node(slot.node)
            self.assertEqual(inst["op"], {"mul": 4, "ld": 0xE, "st": 0xF}[node.op])
            if node.dst:
                self.assertEqual(inst["dst"], int(node.dst[1:]))
            if node.imm is not None:
                self.assertEqual(inst["imm"], node.imm)

        # m0 = mul(r0, r1) -> r2
        m0 = sched.slot("m0")
        inst = unpack_inst(words[m0.cycle * 16 + m0.unit])
        self.assertEqual((inst["src_a"], inst["src_b"], inst["dst"]), (0, 1, 2))
        # st0 = st(r3) -> spad[1]
        st0 = sched.slot("st0")
        inst = unpack_inst(words[st0.cycle * 16 + st0.unit])
        self.assertEqual((inst["src_a"], inst["dst"], inst["imm"]), (3, 0, 1))

        used = {(s.cycle, s.unit) for s in sched.slots}
        for cyc in range(sched.cycles()):
            for unit in range(16):
                if (cyc, unit) not in used:
                    self.assertEqual(words[cyc * 16 + unit], 0, f"({cyc},{unit}) 应为 NOP")

    def test_loop_is_unrolled(self):
        dfg = load_dfg(ROOT / "examples" / "imem_loop.json")
        sched = ModuloScheduler().schedule(dfg, self.model)
        # mul->mac 有寄存器依赖，PE 私有寄存器堆 -> 同 PE -> 每迭代 2 拍
        self.assertEqual(sched.ii, 2)
        header = self.emitter.emit(sched, dfg, self.model)
        words = parse_words(header)
        total = sched.total_cycles(dfg.trip_count)
        self.assertEqual(total, 3 * 2 + 2)         # (4-1)*2 + span 2
        self.assertEqual(len(words), total * 16)

        # 第 k 次迭代：mul 在 2k 拍，mac 在 2k+1 拍，同一 PE
        mul = sched.slot("mul")
        mac = sched.slot("mac")
        self.assertEqual(mul.unit, mac.unit)
        for it in range(4):
            inst = unpack_inst(words[(it * 2 + mul.cycle) * 16 + mul.unit])
            self.assertEqual(inst["op"], 4)
            inst = unpack_inst(words[(it * 2 + mac.cycle) * 16 + mac.unit])
            self.assertEqual(inst["op"], 5)        # mac，dst 字段为 0
            self.assertEqual(inst["dst"], 0)
        # 最后一拍只有第 4 次迭代的 mac（同一颗 PE），没有残留的 mul
        last = total - 1
        self.assertEqual(unpack_inst(words[last * 16 + mul.unit])["op"], 5)

    def test_illegal_operand_rejected(self):
        dfg = load_dfg(ROOT / "examples" / "loop_dot.json")   # 输入是 x / c，无法编码
        sched = ModuloScheduler().schedule(dfg, self.model)
        with self.assertRaises(EmitError):
            self.emitter.emit(sched, dfg, self.model)

    def test_header_symbols(self):
        dfg = load_dfg(ROOT / "examples" / "imem_dot.json")
        sched = ListScheduler().schedule(dfg, self.model)
        header = self.emitter.emit(sched, dfg, self.model)
        self.assertIn("#define IMEM_DOT_PROG_CYCLES", header)
        self.assertIn("static const unsigned int imem_dot_prog[", header)
        # 存储访问点由编译器导出，C 侧不再写死 bank/word
        self.assertIn("#define IMEM_DOT_ST0_BANK 0", header)
        self.assertIn("#define IMEM_DOT_ST0_WORD 1", header)


    def test_add2_parity_with_rule_mapper(self):
        """与 GPCP 规则映射器的 add2 微码逐字对账（参考文件存在时才跑）。"""
        import subprocess
        ref = ROOT.parent / "GPCP" / "sw" / "soc" / "gen" / "add2_prog.h"
        if not ref.exists():
            self.skipTest(f"参考文件不存在：{ref}")
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "compare_add2.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("0 差异", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
