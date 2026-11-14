#!/usr/bin/env python3
"""wlbench command line: build / verify / evaluate."""
import argparse
import json
import os
import sys

from wlbench import build as B
from wlbench import verify as V
from wlbench import metrics as M

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_OUT = os.path.join(ROOT, "build", "bundles")
DEFAULT_LOCK = os.path.join(ROOT, "regression", "lock.json")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wlbench")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="generate workload bundles")
    b.add_argument("--scenario")
    b.add_argument("--all", action="store_true")
    b.add_argument("--out", default=DEFAULT_OUT)
    b.add_argument("--no-check", action="store_true")

    v = sub.add_parser("verify", help="check bundles against the P0 regression lock")
    v.add_argument("--build", default=DEFAULT_OUT)
    v.add_argument("--lock", default=DEFAULT_LOCK)

    e = sub.add_parser("evaluate", help="decide protocol timing from backend metrics")
    e.add_argument("--bundle", required=True)
    e.add_argument("--metrics", required=True)

    a = ap.parse_args(argv)

    if a.cmd == "build":
        if not (a.all or a.scenario):
            print("[FAIL] use --all or --scenario NAME")
            return 2
        check = not a.no_check
        if a.all:
            results = B.build_all(a.out, check)
            for r in results:
                print("  %-24s ir=%s  stimulus=%d B" %
                      (r["scenario"], r["ir_sha16"], r["stimulus_bytes"]))
            print("built %d bundles -> %s" % (len(results), a.out))
        else:
            r = B.build_one(a.scenario, os.path.join(a.out, a.scenario), check)
            print("built %s -> %s (ir=%s)" %
                  (r["scenario"], os.path.join(a.out, a.scenario), r["ir_sha16"]))
        return 0

    if a.cmd == "verify":
        rows, failures = V.verify(a.lock, a.build)
        return V.print_report(rows, failures)

    if a.cmd == "evaluate":
        m = M.load_json(a.metrics)
        rep = M.evaluate(a.bundle, m)
        print(json.dumps(rep, indent=2, ensure_ascii=False))
        return 0 if (rep["correct"] and rep["on_time"]) else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
