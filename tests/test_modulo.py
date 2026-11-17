"""modulo 调度器测试：MII 下界、II 感知的资源投影、回边约束。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cgracc.archreg import load_arch                          # noqa: E402
from cgracc.ir.dfg import BackEdge, DFG, Node, load_dfg       # noqa: E402
from cgracc.ir.schedule import Schedule, Slot                 # noqa: E402
from cgracc.sched.modulo import ModuloScheduler               # noqa: E402
from cgracc.verify.legality import check                      # noqa: E402


class TestModulo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model, _ = load_arch("gpcp_4x4", ROOT / "arch")
        cls.loop = load_dfg(ROOT / "examples" / "loop_dot.json")

    def test_self_loop_mii_and_ii(self):
        s = ModuloScheduler()
        self.assertEqual(s._res_mii(self.loop, self.model), 1)   # 2 个算子 / 16 单元
        self.assertEqual(s._rec_mii(self.loop, self.model), 1)   # add 延迟 1 / 距离 1
        sched = s.schedule(self.loop, self.model)
        # MII 下界是 1，但 GPCP 的寄存器堆是 PE 私有的：mul 与 add 必须在同一 PE，
        # 一个 PE 每拍只能发一条，所以每个迭代至少 2 拍 -> 实际 II=2。
        self.assertEqual(sched.ii, 2)
        self.assertEqual(check(sched, self.loop, self.model), [])
        self.assertEqual(sched.run_cycles, 63 * 2 + sched.cycles())

    def test_two_node_cycle_sets_rec_mii(self):
        dfg = DFG("iir", (
            Node("p", "add", ("q", "x"), dst="r0"),
            Node("q", "add", ("p", "y"), dst="r1"),
        ), back_edges=(BackEdge("q", "p", 1),), trip_count=16)
        s = ModuloScheduler()
        self.assertEqual(s._rec_mii(dfg, self.model), 2)         # (1+1) / 1
        sched = s.schedule(dfg, self.model)
        self.assertGreaterEqual(sched.ii, 2)
        self.assertEqual(check(sched, dfg, self.model), [])

    def test_back_edge_without_ii_is_rejected(self):
        sched = Schedule("loop_dot", run_cycles=2, slots=(
            Slot("mul", "mul", unit=0, cycle=0, dst="r0"),
            Slot("acc", "add", unit=1, cycle=1, dst="r1"),
        ))
        rules = {v.rule for v in check(sched, self.loop, self.model)}
        self.assertIn("R7", rules)

    def test_resource_check_projects_onto_kernel_slot(self):
        # II=1：cycle 0 与 cycle 1 属于同一个 kernel 槽位，同一 unit 放两条 -> R4
        sched = Schedule("loop_dot", run_cycles=2, ii=1, slots=(
            Slot("mul", "mul", unit=0, cycle=0, dst="r0"),
            Slot("acc", "add", unit=0, cycle=1, dst="r1"),
        ))
        rules = {v.rule for v in check(sched, self.loop, self.model)}
        self.assertIn("R4", rules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
