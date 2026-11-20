"""代价模型与落点搜索测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cgracc.archreg import load_arch                          # noqa: E402
from cgracc.cost import estimate                              # noqa: E402
from cgracc.ir.dfg import BackEdge, DFG, Node, load_dfg       # noqa: E402
from cgracc.ir.schedule import plan_trips                     # noqa: E402
from cgracc.sched.list_sched import ListScheduler             # noqa: E402
from cgracc.sched.modulo import ModuloScheduler               # noqa: E402
from cgracc.search import search                              # noqa: E402
from cgracc.verify.legality import check                      # noqa: E402


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

    def test_multi_run_split(self):
        """迭代次数超过一次 run 的容量时，自动切成多次 run（tpr 整除 trip）。"""
        dfg = DFG("long_loop", (
            Node("mul", "mul", ("x", "c"), dst="r0"),
            Node("acc", "add", ("acc", "mul"), dst="r1"),
        ), back_edges=(BackEdge("acc", "acc", 1),), trip_count=1024)
        res = search(dfg, self.model)
        self.assertTrue(res.best.ok)
        sched = res.best.schedule
        # II=2、跨度 2：一次 run 最多 (512-2)//2+1 = 256 次迭代 -> 4 次 run
        self.assertEqual((sched.ii, sched.cycles()), (2, 2))
        self.assertEqual((sched.trips_per_run, sched.runs), (256, 4))
        self.assertEqual(dfg.trip_count % sched.trips_per_run, 0)
        self.assertEqual(sched.per_run_cycles(dfg.trip_count), 512)
        self.assertEqual(sched.total_cycles(dfg.trip_count), 2048)   # 4 x 512
        cost = res.best.cost
        self.assertEqual(cost.overhead, 32 * 4)      # 启动开销按 run 累加
        self.assertEqual(cost.cycles, 2048 + 128)

    def test_plan_trips_picks_divisor(self):
        """不能整除时向下找最大的因子，避免最后一次 run 多跑几次迭代。"""
        tpr, runs = plan_trips(1000, 2, 2, 512)
        self.assertEqual(tpr * runs, 1000)
        self.assertEqual(1000 % tpr, 0)
        self.assertLessEqual((tpr - 1) * 2 + 2, 512)
        self.assertEqual(plan_trips(64, 2, 2, 512), (64, 1))   # 装得下就不拆

    def test_pack_bind_is_a_search_dimension(self):
        """分量 -> PE 装箱：长链和短链各占一颗 PE，且不劣于首次适配。"""
        nodes = [
            Node("a0", "mul", ("r0", "r1"), dst="r2"),
            Node("a1", "add", ("a0",), dst="r3"),
            Node("a2", "mul", ("a1", "r1"), dst="r4"),
            Node("a3", "add", ("a2",), dst="r5"),
        ]
        for i in range(6):
            nodes += [Node(f"s{i}a", "mul", ("r0", "r1"), dst="r2"),
                      Node(f"s{i}b", "add", (f"s{i}a",), dst="r3")]
        dfg = DFG("mixed", tuple(nodes))

        decl = ListScheduler(bind="decl").schedule(dfg, self.model)
        pack = ListScheduler(bind="pack").schedule(dfg, self.model)
        self.assertEqual(check(decl, dfg, self.model), [])
        self.assertEqual(check(pack, dfg, self.model), [])
        self.assertLessEqual(pack.run_cycles, decl.run_cycles)
        # 4 节点长链整条落在同一颗 PE 上（私有寄存器堆）
        self.assertEqual(len({s.unit for s in pack.slots if s.node.startswith("a")}), 1)

        labels = {c.label for c in search(dfg, self.model).candidates}
        self.assertIn("list/topo/pack", labels)
        self.assertIn("list/topo/decl", labels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
