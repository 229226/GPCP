#!/usr/bin/env python3
"""Demo backend: a third architecture attaches in one file.

It consumes the same bundle as any real backend, produces the generic metrics
contract (clock_hz / cycles / correct) and lets wlbench decide on-time status.

IMPORTANT: the cycles below are SYNTHETIC (a node-count + bit-count cost model).
They exist only to demonstrate the interface; they are never performance data.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLES = ROOT / "build" / "bundles"

sys.path.insert(0, str(ROOT / "src"))
from wlbench import metrics as M  # noqa: E402


def synthetic_metrics(bundle, clock_hz):
    ir = json.loads((bundle / "ir.json").read_text(encoding="utf-8"))
    gold = json.loads((bundle / "golden.json").read_text(encoding="utf-8"))
    nodes = ir["nodes"]
    nbits = int((gold.get("anchors") or {}).get("nbits", 0) or 0)
    per_node_bits = max(1, nbits // max(1, len(nodes)))
    cycles = [800 + 2 * per_node_bits for _ in nodes]
    stages = [{"id": n["id"], "op": n["op"], "cycles": c, "correct": True}
              for n, c in zip(nodes, cycles)]
    return {
        "backend": "demo-python",
        "clock_hz": clock_hz,
        "clock_source": "simulation",
        "total_cycles": sum(cycles),
        "correct": True,
        "stages": stages,
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--clock-hz", type=int, default=500_000_000)
    a = ap.parse_args(argv)

    if a.all:
        names = sorted(p.name for p in BUNDLES.iterdir() if p.is_dir())
    elif a.scenario:
        names = [a.scenario]
    else:
        print("use --scenario NAME or --all")
        return 2

    print("%-24s %10s %10s %8s %6s" %
          ("scenario", "cycles", "wall_ms", "on_time", "rate"))
    for name in names:
        b = BUNDLES / name
        m = synthetic_metrics(b, a.clock_hz)
        rep = M.evaluate(str(b), m)
        t = rep.get("timeline")
        rate = "%.2f" % t["on_time_rate"] if t else "n/a"
        print("%-24s %10d %10.3f %8s %6s" %
              (name, rep["total_cycles"], rep["wall_ms"],
               rep["primary_on_time"], rate))
    return 0


if __name__ == "__main__":
    sys.exit(main())
