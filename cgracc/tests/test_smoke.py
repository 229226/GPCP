"""端到端冒烟测试：模型 -> 调度 -> 检查 -> 发射，以及必须被拒绝的负例。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cgracc.archreg import load_arch                      # noqa: E402
from cgracc.conformance import conflicting_pair, run_all     # noqa: E402
from cgracc.emit.listing import ListingEmitter            # noqa: E402
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
        dfg = load_dfg(ROOT / "examples" / "imem_dot.json")
        sched = ListScheduler().schedule(dfg, self.model)
        text = self.emitter.emit(sched, dfg, self.model)
        self.assertIn("_PROG_CYCLES", text)
        self.assertIn("_PROG_H", text)

    def test_bank_conflict_is_rejected(self):
        sp = self.model.store["spad"]
        a0, a1 = conflicting_pair(sp)       # 同一物理 bank、不同物理字
        dfg = DFG("two_loads", (
            Node("a", "ld", imm=a0, dst="r0", space="spad"),
            Node("b", "ld", imm=a1, dst="r1", space="spad"),
        ))
        sched = Schedule("two_loads", run_cycles=1, slots=(
            Slot("a", "ld", unit=0, cycle=0, dst="r0", addr=a0, space="spad"),
            Slot("b", "ld", unit=1, cycle=0, dst="r1", addr=a1, space="spad"),
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

    def test_second_arch_needs_only_yaml(self):
        """架构 1 只提供 model.yaml（没有 emitter.py），接口仍然成立。"""
        model8, emitter8 = load_arch("gpcp_8x8", ROOT / "arch")
        self.assertEqual(model8.compute.units, 64)
        self.assertEqual(model8.time.max_run_cycles, 128)
        self.assertEqual(model8.store["spad"].banks, 128)
        self.assertIsInstance(emitter8, ListingEmitter)

        dfg = load_dfg(ROOT / "examples" / "wide20.json")
        s8 = ListScheduler().schedule(dfg, model8)
        self.assertEqual(check(s8, dfg, model8), [])
        self.assertEqual(s8.run_cycles, 1)          # 20 个独立 op < 64 units

        # 同一 DFG 在 4x4 上要两拍（16 units），说明差异确实来自模型
        s4 = ListScheduler().schedule(dfg, self.model)
        self.assertEqual(check(s4, dfg, self.model), [])
        self.assertEqual(s4.run_cycles, 2)

    def test_conformance_on_every_arch(self):
        for name in ("gpcp_4x4", "gpcp_8x8"):
            model, _ = load_arch(name, ROOT / "arch")
            results = run_all(model)
            failed = [r.name for r in results if not r.ok]
            self.assertEqual(failed, [], f"{name}: {failed}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
