#!/usr/bin/env python3
"""Backend conformance suite.

Any hardware backend must satisfy these before its metrics are accepted:
  1. the frozen 17-scenario workload bundles still reproduce byte-for-byte;
  2. two backends with different clocks produce the expected timing verdicts
     from the *same* bundle (only clock_hz / cycles differ);
  3. no architecture-specific vocabulary appears in business source files;
  4. the metrics contract rejects architecture leakage and malformed values;
  5. the continuous-arrival timeline matches load.profile()/load.simulate().
"""
import glob
import json
import os
import re
import sys
import tempfile

from wlbench import load as LOAD
from wlbench import metrics as M
from wlbench import verify as V

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXCLUDED_SCAN = {"check_hll.py", "metrics.py", "conformance.py", "params.py"}


def _check(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": detail}


def _tmp_bundle(traffic, slot_ms=1.0):
    d = tempfile.mkdtemp(prefix="wlbench-conf-")
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": "wlbench-bundle/1", "scenario": "conf",
                   "cfg": {}, "derived": {}, "traffic": traffic,
                   "budget": {"slot_ms": slot_ms}}, f)
    return d


def run(root=ROOT):
    checks = []

    lock = os.path.join(root, "regression", "lock.json")
    bundles = os.path.join(root, "build", "bundles")
    rows, failures = V.verify(lock, bundles)
    # 每场景 3 个产物 + 1 份账本；数量跟着场景数走，不写死 52。
    import json as _json
    with open(lock, encoding="utf-8") as f:
        n_scen = len(_json.load(f)["scenarios"])
    expected = 3 * n_scen + 1
    checks.append(_check("frozen bundles byte-identical",
                         not failures and len(rows) == expected,
                         "%d/%d artifacts" % (len(rows) - len(failures), len(rows))))

    nr = os.path.join(bundles, "nr_mimo")
    if os.path.isdir(nr):
        a = M.evaluate(nr, {"backend": "a", "clock_hz": 1053000000,
                            "total_cycles": 408874, "correct": True})
        b = M.evaluate(nr, {"backend": "b", "clock_hz": 264000000,
                            "total_cycles": 9169306, "correct": True})
        checks.append(_check("same bundle, two clocks, different verdicts",
                             a["on_time"] and not b["on_time"],
                             "a=%.3f ms on_time=%s / b=%.2f ms on_time=%s"
                             % (a["wall_ms"], a["on_time"],
                                b["wall_ms"], b["on_time"])))
        checks.append(_check("cycle budget derived from each clock",
                             a["budget_cycles"] == 1053000 and
                             b["budget_cycles"] == 264000,
                             "%d vs %d" % (a["budget_cycles"], b["budget_cycles"])))

    hits = []
    for p in glob.glob(os.path.join(root, "src", "wlbench", "**", "*.py"),
                       recursive=True):
        if os.path.basename(p) in EXCLUDED_SCAN:
            continue
        with open(p, encoding="utf-8") as f:
            txt = f.read()
        for m in M.FORBIDDEN_RE.finditer(txt):
            hits.append("%s: %s" % (os.path.relpath(p, root), m.group(0)))
    checks.append(_check("no architecture words in workload source",
                         not hits, "; ".join(hits[:3])))

    rejected = 0
    for bad in (
        {"backend": "x", "clock_hz": 1, "total_cycles": 1, "correct": True,
         "array_runs": 3},
        {"backend": "x", "clock_hz": 0, "total_cycles": 1, "correct": True},
        {"backend": "x", "clock_hz": 1, "total_cycles": 1},
    ):
        try:
            M.validate_metrics(bad)
        except ValueError:
            rejected += 1
    checks.append(_check("metrics contract rejects bad/leaking inputs",
                         rejected == 3, "%d/3 rejected" % rejected))

    traffic = {"arrival": "periodic", "jobs_per_slot": 1, "slots": 40, "util": 1.0}
    d = _tmp_bundle(traffic)
    r = M.evaluate(d, {"backend": "conf", "clock_hz": 1000000000,
                       "total_cycles": 1000, "correct": True})
    ref_rows, ref_late = LOAD.simulate(1000, LOAD.profile(traffic, 40), 1000000)
    ok = (r["timeline"] is not None and
          r["timeline"]["late_slots"] == ref_late and
          len(r["timeline"]["rows"]) == len(ref_rows))
    checks.append(_check("timeline matches load.simulate", ok,
                         "late=%s rate=%.2f" % (r["timeline"]["late_slots"],
                                                r["timeline"]["on_time_rate"])
                         if r["timeline"] else "no timeline"))
    import shutil
    shutil.rmtree(d, ignore_errors=True)

    demo = os.path.join(root, "examples", "reference_backend.py")
    if os.path.exists(demo):
        import subprocess
        r = subprocess.run([sys.executable, demo, "--scenario", "nr_mimo"],
                           capture_output=True, text=True)
        lines = (r.stdout or r.stderr).strip().splitlines()
        checks.append(_check("demo backend attaches through metrics contract",
                             r.returncode == 0,
                             lines[-1] if lines else "no output"))
    else:
        checks.append(_check("demo backend attaches through metrics contract",
                             False, "examples/reference_backend.py missing"))

    return checks


def print_report(checks):
    for c in checks:
        print("  [%s] %-52s %s" %
              ("OK" if c["ok"] else "FAIL", c["name"], c["detail"]))
    bad = [c for c in checks if not c["ok"]]
    print("conformance: %d/%d passed" % (len(checks) - len(bad), len(checks)))
    return 0 if not bad else 1
