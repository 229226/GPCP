"""端到端冒烟测试：模型 -> 调度 -> 检查 -> 发射，以及必须被拒绝的负例。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cgracc.archreg import load_arch                      # noqa: E402
from cgracc.ir.dfg import DFG, Node, load_dfg             # noqa: E402
from cgracc.ir.schedule import Schedule, Slot             # noqa: E402
from cgracc.sched.list_sched import ListScheduler         # noqa: E402
from cgracc.verify.legality import check                  # noqa: E402


class TestSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model, cls.emitter = load_arch("gpcp_4x4", ROOT / "arch")

    def test_model_fields(self):
        m = self.model
        self.assertEqual(m.compute.units, 16)
        self.assertEqual(m.compute.registers, 8)
        self.assertEqual(m.time.max_run_cycles, 512)
        self.assertEqual(m.store["spad"].banks, 64)
        self.assertEqual(m.store["spad"].read_ports_per_bank, 1)
        self.assertIn("dma", m.move)

    def test_schedule_then_check_is_clean(self):
        dfg = load_dfg(ROOT / "examples" / "dot4.json")
        sched = ListScheduler().schedule(dfg, self.model)
        self.assertEqual(len(sched.slots), 4)
        self.assertEqual(check(sched, dfg, self.model), [])
        self.assertGreaterEqual(sched.run_cycles, 3)   # mul -> add -> mul 链

    def test_emit(self):
        dfg = load_dfg(ROOT / "examples" / "dot4.json")
        sched = ListScheduler().schedule(dfg, self.model)
        text = self.emitter.emit(sched, dfg, self.model)
        self.assertIn("run_cycles", text)
        self.assertIn("cyc", text)

    def test_bank_conflict_is_rejected(self):
        dfg = DFG("two_loads", (
            Node("a", "ld", imm=0, dst="r0", space="spad"),
            Node("b", "ld", imm=1, dst="r1", space="spad"),
        ))
        # 地址 0 和 1 都落在 bank 0（32 字一 bank），同拍两次读 = 端口超限
        sched = Schedule("two_loads", run_cycles=1, slots=(
            Slot("a", "ld", unit=0, cycle=0, dst="r0", addr=0, space="spad"),
            Slot("b", "ld", unit=1, cycle=0, dst="r1", addr=1, space="spad"),
        ))
        rules = {v.rule for v in check(sched, dfg, self.model)}
        self.assertIn("R6", rules)

    def test_dependency_order_is_enforced(self):
        dfg = DFG("chain", (
            Node("p", "mul", ("a", "b"), dst="r0"),
            Node("q", "add", ("p", "x"), dst="r1"),
        ))
        # 消费者与生产者同一拍：mul 的结果下一拍才可见 -> R7
        sched = Schedule("chain", run_cycles=1, slots=(
            Slot("p", "mul", unit=0, cycle=0, dst="r0"),
            Slot("q", "add", unit=1, cycle=0, dst="r1"),
        ))
        rules = {v.rule for v in check(sched, dfg, self.model)}
        self.assertIn("R7", rules)

    def test_run_length_limit(self):
        dfg = DFG("one", (Node("n", "mov", ("a",), dst="r0"),))
        sched = Schedule("one", run_cycles=600, slots=(
            Slot("n", "mov", unit=0, cycle=600, dst="r0"),
        ))
        rules = {v.rule for v in check(sched, dfg, self.model)}
        self.assertIn("R3", rules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
