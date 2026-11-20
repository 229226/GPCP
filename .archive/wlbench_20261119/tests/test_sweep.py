#!/usr/bin/env python3
"""Parameter sweeps must produce genuinely different workload data."""
import json
import os
import tempfile
import unittest

from wlbench import build as B
from wlbench import cfg_schema
from wlbench import numerology


class ParameterisedWorkload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_layers_rx_change_every_artifact(self):
        a = os.path.join(self.tmp.name, "a")
        b = os.path.join(self.tmp.name, "b")
        ra = B.build_one("mmse_scalable", a, False, {"layers": 2, "rx": 2})
        rb = B.build_one("mmse_scalable", b, False, {"layers": 4, "rx": 4})
        self.assertNotEqual(ra["ir_sha16"], rb["ir_sha16"])
        self.assertNotEqual(ra["stimulus_bytes"], rb["stimulus_bytes"])
        with open(os.path.join(a, "golden.json"), encoding="utf-8") as f:
            ga = json.load(f)
        with open(os.path.join(b, "golden.json"), encoding="utf-8") as f:
            gb = json.load(f)
        self.assertEqual(ga["anchors"]["layers"], 2)
        self.assertEqual(gb["anchors"]["layers"], 4)
        self.assertEqual(gb["anchors"]["nbits"], 180 * 4 * 2)
        self.assertNotEqual(ga["bits"]["packed"], gb["bits"]["packed"])

    def test_received_length_scales_with_mimo_size(self):
        d = os.path.join(self.tmp.name, "c")
        B.build_one("mmse_scalable", d, False, {"layers": 8, "rx": 8})
        with open(os.path.join(d, "ir.json"), encoding="utf-8") as f:
            ir = json.load(f)
        received = [n for n in ir["nodes"] if n["op"] == "received"][0]
        self.assertEqual(received["length"],
                         2 * 2 * (288 + 4096) * (8 * 8) // (2 * 2))
        self.assertEqual(ir["cfg"]["layers"], 8)

    def test_cfg_schema_rejects_unknown_keys(self):
        with self.assertRaises(ValueError):
            cfg_schema.validate({"layers": 2, "mystery": 1})
        with self.assertRaises(ValueError):
            cfg_schema.validate({"layers": 0})

    def test_numerology_matches_frozen_values(self):
        d = numerology.derive({"bw_mhz": 20, "scs_khz": 15, "qm": 2,
                               "n_id": 1, "a_bits": 720})
        self.assertEqual(d["n_rb"], 106)
        self.assertEqual(d["n_subcarriers"], 1272)
        self.assertEqual(d["n_fft_min"], 2048)
        self.assertEqual(d["qm"], 2)
        with self.assertRaises(ValueError):
            numerology.derive({"bw_mhz": 25, "scs_khz": 15})


if __name__ == "__main__":
    unittest.main()
