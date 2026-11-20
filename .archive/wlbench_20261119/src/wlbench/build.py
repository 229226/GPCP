#!/usr/bin/env python3
"""Build workload bundles: ir.json + stimulus.bin + golden.json + manifest.json.

Byte-compatible with GPCP proto/compile.py and proto/emit.py for the frozen v1
scenarios.  A sweep may inject parameter overrides; those change the IR and,
for parameterised reference models, stimulus and golden too.
"""
import hashlib
import importlib.util
import json
import os
import sys

from wlbench import cfg_schema
from wlbench import params

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "tools")
SCENARIOS = os.path.join(HERE, "scenarios")
MODELS = os.path.join(HERE, "models")

for p in (HERE, VENDOR):
    if p not in sys.path:
        sys.path.insert(0, p)

ARTIFACTS = ("ir.json", "stimulus.bin", "golden.json")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with open(path, "rb") as f:
        return sha256_bytes(f.read())


def load_module(path, prefix):
    name = prefix + "_" + os.path.basename(path).replace(".py", "")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def scenario_names():
    return sorted(
        os.path.basename(p)[:-3]
        for p in os.listdir(SCENARIOS)
        if p.endswith(".py") and not p.startswith("_")
    )


def parse_overrides(items):
    """Parse KEY=VALUE strings into typed values."""
    out = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("--set expects KEY=VALUE, got %r" % item)
        k, v = item.split("=", 1)
        v = v.strip()
        try:
            val = int(v)
        except ValueError:
            try:
                val = float(v)
            except ValueError:
                val = v
        out[k.strip()] = val
    return out


def _apply_overrides(S, overrides):
    """Update cfg, derived and node tensors so the IR reflects the new point."""
    from wlbench.hll import _derive
    base_rx = S.cfg_kw.get("rx", 2)
    base_layers = S.cfg_kw.get("layers", 2)
    S.cfg_kw.update(overrides)
    cfg_schema.validate(S.cfg_kw)
    S.derived = _derive(S.cfg_kw)
    new_rx = overrides.get("rx", base_rx)
    new_layers = overrides.get("layers", base_layers)
    scale_mimo = ("rx" in overrides or "layers" in overrides)
    for n in S.nodes:
        p = n["params"]
        for k, v in overrides.items():
            if k in p:
                p[k] = v
        if scale_mimo and n["op"] == "received" and base_rx * base_layers:
            n["length"] = int(n["length"] * new_rx * new_layers /
                              (base_rx * base_layers))


def build_one(name, outdir, static_check=True, overrides=None):
    spath = os.path.join(SCENARIOS, name + ".py")
    mpath = os.path.join(MODELS, name + ".py")
    for p in (spath, mpath):
        if not os.path.exists(p):
            raise SystemExit("[FAIL] missing %s" % p)

    overrides = dict(overrides or {})
    params.set_current(overrides)
    try:
        if static_check:
            import check_hll as CHK
            bad = CHK.check_file(spath)
            if bad:
                for line, msg in sorted(bad):
                    print("  %s:%d  %s" % (spath, line, msg))
                raise SystemExit("[FAIL] %s violates the frozen verb list" % name)

        S = load_module(spath, "wlbench_scenario").S
        if overrides:
            _apply_overrides(S, overrides)
        cfg_schema.validate(S.cfg_kw)
        ir_bytes = S.ir_bytes()

        model = load_module(mpath, "wlbench_model")
        stim = model.stimulus_bytes()
        gold = model.golden()
        params_hash = int(sha256_bytes(ir_bytes + stim)[:8], 16)
        gold["scenario"] = name
        gold["params_hash"] = "0x%08x" % params_hash
        gold["stimulus_words"] = len(stim) // 4

        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "ir.json"), "wb") as f:
            f.write(ir_bytes)
        with open(os.path.join(outdir, "stimulus.bin"), "wb") as f:
            f.write(stim)
        with open(os.path.join(outdir, "golden.json"), "w", encoding="utf-8") as f:
            json.dump(gold, f, indent=1, ensure_ascii=False)

        ir_sha = sha256_bytes(ir_bytes)
        manifest = {
            "schema": "wlbench-bundle/1",
            "scenario": name,
            "rat": S.rat,
            "direction": S.direction,
            "cfg": S.cfg_kw,
            "derived": S.derived,
            "traffic": S.traffic_kw,
            "budget": S.budget_kw,
            "overrides": overrides,
            "note": "Time budgets are in ms; cycle budgets are derived from clock_hz.",
            "ir_sha256": ir_sha,
            "ir_sha16": ir_sha[:16],
            "artifacts": {
                a: {"sha256": sha256_file(os.path.join(outdir, a)),
                    "bytes": os.path.getsize(os.path.join(outdir, a))}
                for a in ARTIFACTS
            },
        }
        with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")

        return {"scenario": name, "ir_sha16": ir_sha[:16],
                "stimulus_bytes": len(stim), "overrides": overrides}
    finally:
        params.set_current({})


def build_all(outdir, static_check=True):
    return [build_one(n, os.path.join(outdir, n), static_check)
            for n in scenario_names()]
