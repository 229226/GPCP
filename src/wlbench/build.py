#!/usr/bin/env python3
"""Build a workload bundle: ir.json + stimulus.bin + golden.json + manifest.json.

Byte-compatible with GPCP proto/compile.py (ir.json) and proto/emit.py
(stimulus.bin / golden.json) so the P0 regression lock can be checked exactly.
No target geometry, no clock, no result-buffer layout, no architecture words.
"""
import hashlib
import importlib.util
import json
import os
import sys

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


def build_one(name, outdir, static_check=True):
    spath = os.path.join(SCENARIOS, name + ".py")
    mpath = os.path.join(MODELS, name + ".py")
    for p in (spath, mpath):
        if not os.path.exists(p):
            raise SystemExit("[FAIL] missing %s" % p)

    if static_check:
        import check_hll as CHK
        bad = CHK.check_file(spath)
        if bad:
            for line, msg in sorted(bad):
                print("  %s:%d  %s" % (spath, line, msg))
            raise SystemExit("[FAIL] %s violates the frozen verb list" % name)

    S = load_module(spath, "wlbench_scenario").S
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
        "note": "Time budgets are in ms; cycle budgets are derived by the backend's clock_hz.",
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

    return {"scenario": name, "ir_sha16": ir_sha[:16], "stimulus_bytes": len(stim)}


def build_all(outdir, static_check=True):
    out = []
    for name in scenario_names():
        out.append(build_one(name, os.path.join(outdir, name), static_check))
    return out
