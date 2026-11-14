#!/usr/bin/env python3
"""The evaluation contract: backend reports clock_hz / cycles / correct,
wlbench decides whether the protocol timing requirement was met.

This module contains no architecture concepts.  A backend may be a CPU, a CGRA,
an FPGA, a GPU or a pure-software reference - all of them report the same three
generic quantities.  Time budgets live in the workload bundle (ms); cycle
budgets are derived here from the backend's clock_hz.
"""
import json
import os

METRICS_SCHEMA = {
    "schema": "wlbench-metrics/1",
    "required": ["backend", "clock_hz", "total_cycles", "correct"],
    "properties": {
        "backend": {"type": "string"},
        "clock_hz": {"type": "integer", "minimum": 1},
        "clock_source": {"enum": ["measured", "estimated-pre-layout", "simulation"]},
        "total_cycles": {"type": "integer", "minimum": 0},
        "correct": {"type": "boolean"},
        "stages": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "op", "cycles"],
                "properties": {
                    "id": {"type": "integer"},
                    "op": {"type": "string"},
                    "cycles": {"type": "integer", "minimum": 0},
                    "correct": {"type": "boolean"},
                },
            },
        },
    },
}


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_metrics(m):
    missing = [k for k in METRICS_SCHEMA["required"] if k not in m]
    if missing:
        raise ValueError("metrics missing required fields: %s" % missing)
    if int(m["clock_hz"]) <= 0:
        raise ValueError("clock_hz must be positive")
    return m


def evaluate(bundle_dir, metrics):
    """Pure function: workload bundle + backend metrics -> timing verdict.

    Returns on_time, wall time, derived cycle budget and a per-stage gap table.
    Continuous-arrival on-time rate is exposed as `timeline` only when the
    workload declares a traffic model; that part is aligned with GPCP load.py
    in P2.
    """
    m = validate_metrics(metrics)
    manifest = load_json(os.path.join(bundle_dir, "manifest.json"))
    budget = manifest.get("budget") or {}
    slot_ms = float(budget.get("slot_ms", 0.0))
    clock_hz = int(m["clock_hz"])
    total = int(m["total_cycles"])

    budget_cycles = int(round(slot_ms * clock_hz / 1000.0)) if slot_ms else 0
    wall_ms = total / clock_hz * 1000.0
    on_time = bool(budget_cycles and total <= budget_cycles)

    stages = []
    for st in m.get("stages", []):
        c = int(st["cycles"])
        stages.append({
            "id": st["id"],
            "op": st["op"],
            "cycles": c,
            "time_ms": c / clock_hz * 1000.0,
            "share": (c / total) if total else 0.0,
            "correct": bool(st.get("correct", True)),
        })

    traffic = manifest.get("traffic") or {}
    arrival = traffic.get("arrival", "single")
    timeline_note = (
        "single-arrival verdict only; continuous timeline is aligned in P2"
        if arrival in ("single", "none", None, "") else
        "continuous arrival declared; timeline aligned with load.py in P2"
    )

    return {
        "schema": "wlbench-evaluation/1",
        "scenario": manifest["scenario"],
        "backend": m["backend"],
        "clock_hz": clock_hz,
        "clock_source": m.get("clock_source", "unspecified"),
        "slot_ms": slot_ms,
        "budget_cycles": budget_cycles,
        "total_cycles": total,
        "wall_ms": wall_ms,
        "overrun_x": (total / budget_cycles) if budget_cycles else None,
        "on_time": on_time,
        "correct": bool(m["correct"]),
        "stages": stages,
        "timeline": timeline_note,
    }
