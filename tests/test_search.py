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
        # PE 私有寄存器堆 -> mul/add 同 PE -> II=2，64 次迭代：(64-1)*2 + 2
        self.assertEqual(sched.ii, 2)
        self.assertEqual(c.compute_cycles, 128)
        self.assertEqual(c.overhead, 32)            # 模型里的 T0 实测开销
        self.assertEqual(c.cycles, 160)
        self.assertAlmostEqual(c.efficiency, 2 / (16 * 2))

    def test_search_prefers_pipelined_loop(self):
        res = search(self.loop, self.model)
        self.assertIsNotNone(res.best)
        # 非流水的 list 会被 R7 拒（回边没有 II），只能选 modulo
        self.assertEqual(res.best.cost.compute_cycles, 128)
        self.assertEqual(res.best.schedule.ii, 2)
        self.assertTrue(res.best.label.startswith("modulo/"))

    def test_search_on_dag(self):
        res = search(self.dag, self.model)
        self.assertIsNotNone(res.best)
        self.assertTrue(res.best.ok)
        # 一条依赖链在一颗 PE 上串行：mul -> mul -> add -> mul = 4 拍
        self.assertEqual(res.best.cost.compute_cycles, 4)
        self.assertEqual(res.best.cost.cycles, 36)

    def test_every_candidate_reports_status(self):
        res = search(self.dag, self.model)
        for c in res.candidates:
            self.assertEqual(c.ok, c.schedule is not None and not c.violations and c.error is None)
            if c.schedule is not None:
                self.assertIsNotNone(c.cost)


if __name__ == "__main__":
    unittest.main(verbosity=2)
