"""代价模型与落点搜索测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cgracc.archreg import load_arch                          # noqa: E402
from cgracc.cost import estimate                              # noqa: E402
from cgracc.ir.dfg import load_dfg                            # noqa: E402
from cgracc.sched.modulo import ModuloScheduler               # noqa: E402
from cgracc.search import search                              # noqa: E402


class TestCostAndSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model, _ = load_arch("gpcp_4x4", ROOT / "arch")
        cls.loop = load_dfg(ROOT / "examples" / "loop_dot.json")
        cls.dag = load_dfg(ROOT / "examples" / "dot4.json")

    def test_cost_includes_run_overhead(self):
        sched = ModuloScheduler().schedule(self.loop, self.model)
        c = estimate(sched, self.loop, self.model)
        self.assertEqual(c.compute_cycles, 65)      # (64-1)*1 + 2
        self.assertEqual(c.overhead, 32)            # 模型里的 T0 实测开销
        self.assertEqual(c.cycles, 97)
        self.assertEqual(c.ii, 1)
        self.assertAlmostEqual(c.efficiency, 2 / 16)  # 2 个算子 / (16 units × II=1)

    def test_search_prefers_pipelined_loop(self):
        res = search(self.loop, self.model)
        self.assertIsNotNone(res.best)
        # list 非流水要 64×2 = 128 周期；modulo II=1 只要 65
        self.assertEqual(res.best.cost.compute_cycles, 65)
        self.assertEqual(res.best.schedule.ii, 1)
        self.assertTrue(res.best.label.startswith("modulo/"))

    def test_search_on_dag(self):
        res = search(self.dag, self.model)
        self.assertIsNotNone(res.best)
        self.assertTrue(res.best.ok)
        self.assertEqual(res.best.cost.compute_cycles, 3)   # mul -> add -> mul
        self.assertEqual(res.best.cost.cycles, 35)

    def test_every_candidate_reports_status(self):
        res = search(self.dag, self.model)
        for c in res.candidates:
            self.assertEqual(c.ok, c.schedule is not None and not c.violations and c.error is None)
            if c.schedule is not None:
                self.assertIsNotNone(c.cost)


if __name__ == "__main__":
    unittest.main(verbosity=2)
