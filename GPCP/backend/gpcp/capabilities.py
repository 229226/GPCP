#!/usr/bin/env python3
"""能力报告：wlbench 的每个 bundle 在 GPCP 上能不能跑，为什么不能。

    python3 capabilities.py            # 表格式
    python3 capabilities.py --json     # 机器可读（给 conformance 用）

判据只有两条，都是硬件事实，不是策略：
  1. lowering 能不能成（目标装不下 → 不可行）；
  2. 有没有 `sw/soc/ops/<场景>_ops.inc`（算子序列的硬件实现）。

外加一条参数范围：`mmse_full` 现在按 2 层写死，`layers > 2` 的 MMSE
workload 只会被错误地映射，所以显式判为不支持，而不是跑出错的数。
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import adapter as A  # noqa: E402


def bundles():
    root = A.BUNDLES
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if (p / "ir.json").exists())


def _layers(ir):
    out = 0
    for nd in ir.get("nodes") or []:
        if nd.get("op") != "mimo_equalise":
            continue
        out = max(out, int((nd.get("params") or {}).get("layers", 2)))
    return out


def report(bundle):
    name = bundle.name
    rec = {"bundle": name, "supported": False, "reasons": [],
           "stages": 0, "array_runs": 0, "impls": []}
    try:
        ir, _blob, plan = A.plan_for(name, bundle=bundle)
    except SystemExit as e:
        rec["reasons"].append(str(e))
        return rec
    rec["stages"] = len(plan["stages"])
    rec["array_runs"] = sum(int(s.get("array_runs", 0)) for s in plan["stages"])
    rec["impls"] = sorted({(s.get("impl") or "-") for s in plan["stages"]})

    if _layers(ir) > 2:
        rec["reasons"].append(
            "mmse_full 只实现 2 层，bundle 要 %d 层" % _layers(ir))
    if not (A.GPCP / "sw" / "soc" / "ops" / (name + "_ops.inc")).exists():
        rec["reasons"].append("没有 sw/soc/ops/%s_ops.inc（算子无硬件实现）" % name)
    rec["supported"] = not rec["reasons"]
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpcp-capabilities")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="有不可执行的 bundle 时返回 1")
    a = ap.parse_args(argv)

    rows = [report(b) for b in bundles()]
    if a.json:
        print(json.dumps({"bundles": rows}, indent=1, ensure_ascii=False))
        return 0 if rows else 2

    print("%-20s %-9s %6s %8s  %s" % ("bundle", "supported", "stages", "runs", "reasons"))
    for r in rows:
        print("%-20s %-9s %6d %8d  %s" % (
            r["bundle"], "yes" if r["supported"] else "NO",
            r["stages"], r["array_runs"], "; ".join(r["reasons"]) or ""))
    ok = sum(1 for r in rows if r["supported"])
    print("\n%d/%d 可执行" % (ok, len(rows)))
    if a.strict:
        return 0 if ok == len(rows) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
