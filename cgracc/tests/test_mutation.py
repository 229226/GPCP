"""变异测试：负向用例必须真的依赖检查器。

做法：把 `cgracc.conformance.check` 换成"永远返回无违规"（等价于检查器失效），
再跑一遍 conformance。四个负向用例这时**必须全部失败**；如果它们仍然通过，
说明这些用例是空转的，不能证明接入的架构被真正约束住了。

这是"测试的测试"：正向用例证明架构能被编译，变异测试证明负向用例有牙齿。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cgracc.conformance as conformance              # noqa: E402
from cgracc.archreg import load_arch                  # noqa: E402

NEGATIVE = {"neg_bank", "neg_run", "neg_reg", "neg_page"}
ARCHS = ("gpcp_4x4", "gpcp_8x8")


class TestMutation(unittest.TestCase):
    def test_negative_cases_depend_on_checker(self):
        models = {name: load_arch(name, ROOT / "arch")[0] for name in ARCHS}
        orig = conformance.check
        conformance.check = lambda *a, **k: []        # 检查器失效
        try:
            for arch, model in models.items():
                failed = {r.name for r in conformance.run_all(model) if not r.ok}
                self.assertEqual(
                    failed, NEGATIVE,
                    f"{arch}: 检查器失效时应失败的负向用例集合不符（实际 {sorted(failed)}）",
                )
        finally:
            conformance.check = orig

    def test_restored_checker_still_rejects(self):
        """变异测试跑完后，真实的检查器必须恢复（防止用例互相污染）。"""
        model, _ = load_arch("gpcp_4x4", ROOT / "arch")
        results = {r.name: r for r in conformance.run_all(model)}
        for name in NEGATIVE:
            self.assertTrue(results[name].ok, f"{name} 在真实检查器下应当通过")


if __name__ == "__main__":
    unittest.main(verbosity=2)
