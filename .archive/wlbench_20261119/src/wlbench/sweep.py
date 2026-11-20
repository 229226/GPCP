#!/usr/bin/env python3
"""Parameter sweep: build one bundle per parameter point and prove the data
actually changed.  A parameter whose variants all produce identical stimulus
and golden bytes is reported STALE_GOLDEN instead of being passed off as data.
"""
import itertools
import json
import os

from wlbench import build as B


def _value(v):
    v = v.strip()
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def parse_set(items):
    matrix = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("--set expects KEY=V1,V2, got %r" % item)
        k, vals = item.split("=", 1)
        matrix[k.strip()] = [_value(x) for x in vals.split(",") if x.strip()]
    return matrix


def run(scenario, set_args, out):
    matrix = parse_set(set_args)
    keys = sorted(matrix)
    combos = [dict(zip(keys, vals))
              for vals in itertools.product(*(matrix[k] for k in keys))]
    rows = []
    base = os.path.join(out, scenario)
    os.makedirs(base, exist_ok=True)

    for combo in combos:
        label = "_".join("%s%s" % (k, str(v).replace(".", "p"))
                         for k, v in combo.items())
        d = os.path.join(base, label)
        r = B.build_one(scenario, d, static_check=False, overrides=combo)
        with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
            mani = json.load(f)
        with open(os.path.join(d, "golden.json"), encoding="utf-8") as f:
            gold = json.load(f)
        art = mani["artifacts"]
        rows.append({
            "label": label,
            "params": combo,
            "ir_sha16": r["ir_sha16"],
            "stimulus_sha12": art["stimulus.bin"]["sha256"][:12],
            "stimulus_bytes": art["stimulus.bin"]["bytes"],
            "golden_sha12": art["golden.json"]["sha256"][:12],
            "anchors": gold.get("anchors", {}),
        })

    stale = []
    for k in keys:
        byval = {}
        for row in rows:
            byval.setdefault(row["params"][k], set()).add(
                (row["stimulus_sha12"], row["golden_sha12"]))
        if len(byval) > 1 and len({frozenset(s) for s in byval.values()}) == 1:
            stale.append(k)

    report = {
        "schema": "wlbench-sweep/1",
        "scenario": scenario,
        "matrix": {k: matrix[k] for k in keys},
        "points": rows,
        "stale_parameters": stale,
        "verdict": "OK" if not stale else "STALE_GOLDEN",
    }
    with open(os.path.join(base, "sweep_report.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    print("%-34s %-14s %-14s %10s  %s" %
          ("point", "ir", "stimulus", "bytes", "anchors"))
    for row in rows:
        a = row["anchors"]
        brief = ",".join("%s=%s" % (k, a[k]) for k in
                         ("layers", "rx", "n_re", "nbits") if k in a)
        print("%-34s %-14s %-14s %10d  %s" %
              (row["label"], row["ir_sha16"], row["stimulus_sha12"],
               row["stimulus_bytes"], brief))
    print("points: %d   verdict: %s" % (len(rows), report["verdict"]))
    print("report: %s" % os.path.join(base, "sweep_report.json"))
    if stale:
        print("[WARN] these parameters did not change stimulus/golden: %s"
              % ", ".join(stale))
    return 0 if not stale else 1
