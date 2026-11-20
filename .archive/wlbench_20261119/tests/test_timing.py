#!/usr/bin/env python3
"""The same workload, two backends: only clock_hz and cycles differ."""
import json
import os
import tempfile
import unittest

from wlbench import metrics as M

BUNDLE = {
    "schema": "wlbench-bundle/1",
    "scenario": "nr_mimo",
    "rat": "nr",
    "direction": "dl",
    "cfg": {"layers": 2, "rx": 2},
    "derived": {},
    "traffic": {"arrival": "single"},
    "budget": {"slot_ms": 1.0},
}


def write_bundle(root):
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(BUNDLE, f, ensure_ascii=False)
    return root


class TimingVerdict(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bundle = write_bundle(os.path.join(self.tmp.name, "nr_mimo"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_cgra_meets_deadline(self):
        m = {"backend": "gpcp-cgra", "clock_hz": 1053000000,
             "clock_source": "estimated-pre-layout",
             "total_cycles": 408874, "correct": True}
        r = M.evaluate(self.bundle, m)
        self.assertTrue(r["on_time"])
        self.assertEqual(r["budget_cycles"], 1053000)
        self.assertAlmostEqual(r["wall_ms"], 0.3883, places=3)
        self.assertTrue(r["correct"])

    def test_cpu_misses_deadline(self):
        m = {"backend": "cva6-scalar", "clock_hz": 264000000,
             "clock_source": "estimated-pre-layout",
             "total_cycles": 9169306, "correct": True}
        r = M.evaluate(self.bundle, m)
        self.assertFalse(r["on_time"])
        self.assertEqual(r["budget_cycles"], 264000)
        self.assertAlmostEqual(r["overrun_x"], 34.73, places=1)

    def test_budget_is_derived_not_frozen(self):
        self.assertNotIn("budget_cycles", BUNDLE["budget"])
        a = M.evaluate(self.bundle, {"backend": "a", "clock_hz": 1000000,
                                     "total_cycles": 1, "correct": True})
        b = M.evaluate(self.bundle, {"backend": "b", "clock_hz": 2000000,
                                     "total_cycles": 1, "correct": True})
        self.assertEqual(b["budget_cycles"], 2 * a["budget_cycles"])

    def test_stage_gap_table(self):
        m = {"backend": "x", "clock_hz": 1000000000,
             "total_cycles": 100, "correct": True,
             "stages": [{"id": 0, "op": "received", "cycles": 10, "correct": True},
                        {"id": 1, "op": "ofdm_demodulate", "cycles": 90}]}
        r = M.evaluate(self.bundle, m)
        self.assertEqual(len(r["stages"]), 2)
        self.assertAlmostEqual(r["stages"][1]["share"], 0.9)
        self.assertTrue(r["stages"][1]["correct"])

    def test_metrics_schema_has_no_architecture_words(self):
        blob = json.dumps(M.METRICS_SCHEMA).lower()
        for w in ("spad", "imem", "microcode", "array_runs", "dma", "pe_array"):
            self.assertNotIn(w, blob)


if __name__ == "__main__":
    unittest.main()
