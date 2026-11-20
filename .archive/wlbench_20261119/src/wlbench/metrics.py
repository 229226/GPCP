#!/usr/bin/env python3
"""Evaluation contract: a backend reports clock_hz / cycles / correct,
wlbench decides whether the protocol timing requirement was met.

No architecture concepts live here.  Time budgets are in ms and live in the
workload bundle; cycle budgets are derived from the backend's clock_hz.
Continuous-arrival on-time rate reuses load.profile()/load.simulate() so it
stays identical to the original GPCP timeline model.
"""
import json
import os
import re

from wlbench import load as LOAD

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

# Architecture-specific words a backend must never put into a metric key.
FORBIDDEN = ("spad", "imem", "microcode", "array_runs", "pe_array",
             "bank_conflict", "tiling", "context_page", "planned_runs",
             "dma_beats", "proto_where")
FORBIDDEN_RE = re.compile("|".join(re.escape(w) for w in FORBIDDEN), re.I)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_metrics(m):
    """Validate the generic contract and reject architecture leakage."""
    if not isinstance(m, dict):
        raise ValueError("metrics must be an object")
    missing = [k for k in METRICS_SCHEMA["required"] if k not in m]
    if missing:
        raise ValueError("metrics missing required fields: %s" % missing)
    if not isinstance(m["clock_hz"], int) or m["clock_hz"] <= 0:
        raise ValueError("clock_hz must be a positive integer")
    if not isinstance(m["total_cycles"], int) or m["total_cycles"] < 0:
        raise ValueError("total_cycles must be a non-negative integer")
    if not isinstance(m["correct"], bool):
        raise ValueError("correct must be a boolean")
    for key in m:
        if FORBIDDEN_RE.search(key):
            raise ValueError("architecture-specific metric key rejected: %s" % key)
    for st in m.get("stages", []):
        for need in ("id", "op", "cycles"):
            if need not in st:
                raise ValueError("stage missing %s" % need)
        if FORBIDDEN_RE.search(" ".join(str(k) for k in st)):
            raise ValueError("architecture-specific stage key rejected")
    return m


def evaluate(bundle_dir, metrics):
    """Pure function: workload bundle + backend metrics -> timing verdict."""
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
    slots = int(traffic.get("slots", 0) or 0)
    counts = LOAD.profile(traffic, slots) if (slots and budget_cycles) else []
    timeline = None
    if counts:
        rows, late = LOAD.simulate(total, counts, budget_cycles)
        timeline = {
            "slots": len(counts),
            "cost_per_job": total,
            "budget_cycles": budget_cycles,
            "on_time_rate": (len(counts) - late) / float(len(counts)),
            "late_slots": late,
            "worst_backlog": max((r["backlog"] for r in rows), default=0),
            "rows": rows,
        }

    primary_on_time = (
        bool(timeline and timeline["on_time_rate"] >= 1.0)
        if timeline else on_time
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
        "primary_on_time": primary_on_time,
        "correct": bool(m["correct"]),
        "stages": stages,
        "timeline": timeline,
    }
