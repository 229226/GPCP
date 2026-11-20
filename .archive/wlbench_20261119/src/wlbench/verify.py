#!/usr/bin/env python3
"""Verify freshly built bundles against the frozen P0 regression lock."""
import hashlib
import json
import os

ARTIFACTS = ("ir.json", "stimulus.bin", "golden.json")


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def verify(lock_path, build_root):
    with open(lock_path, encoding="utf-8") as f:
        lock = json.load(f)
    rows, failures = [], []
    for name, exp in sorted(lock["scenarios"].items()):
        for art in ARTIFACTS:
            path = os.path.join(build_root, name, art)
            if not os.path.exists(path):
                failures.append("%s/%s : MISSING" % (name, art))
                rows.append((name, art, "MISSING"))
                continue
            got = sha256_file(path)
            size = os.path.getsize(path)
            ok = (got == exp[art]["sha256"] and size == exp[art]["bytes"])
            rows.append((name, art, "OK" if ok else "MISMATCH"))
            if not ok:
                failures.append("%s/%s : sha=%s bytes=%d (expected %s / %d)"
                                % (name, art, got[:12], size,
                                   exp[art]["sha256"][:12], exp[art]["bytes"]))
    ledger = os.path.join(os.path.dirname(lock_path), "compare_all.csv")
    if os.path.exists(ledger):
        got = sha256_file(ledger)
        ok = got == lock["ledger"]["sha256"]
        rows.append(("ledger", "compare_all.csv", "OK" if ok else "MISMATCH"))
        if not ok:
            failures.append("compare_all.csv sha mismatch")
    return rows, failures


def print_report(rows, failures):
    for name, art, status in rows:
        if status != "OK":
            print("  %-24s %-16s %s" % (name, art, status))
    total = len(rows)
    ok = total - len(failures)
    print("verified %d/%d artifacts" % (ok, total))
    for f in failures:
        print("  [FAIL] " + f)
    return 0 if not failures else 1
