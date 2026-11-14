#!/usr/bin/env python3
"""汇总 17 个场景的双跑账本 build/proto/compare_all.csv。

之前这张表是手工拼的：每个场景单独跑 tools/proto_compare.py，再把
compare_<场景>.csv 拼起来。手工拼的直接后果是"账本滞后"——dump 重跑了、
表没重生成，docs/22、docs/24 的加速比就都挂在旧数上（见 backend/gpcp
README 的 drift 说明）。本脚本把这一步固定成一条命令：

    python3 tools/gen_proto_ledger.py            # 重生成并打印变化
    python3 tools/gen_proto_ledger.py --dry-run  # 只打印变化，不写文件

输入是 sim/soc/build/proto_<场景>_dump.txt 与 ..._cpu_dump.txt；
缺 dump 的场景会被跳过并明确报出来，不会静默少一行。
"""
import argparse
import csv
import glob
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "proto"
LEDGER = BUILD / "compare_all.csv"
FIELDS = ["scenario", "id", "op", "impl", "cgra_cycles",
          "cpu_cycles", "cpu_over_cgra", "verdict"]


def scenarios():
    names = []
    for p in sorted(glob.glob(str(ROOT / "proto" / "scenarios" / "*.py"))):
        n = os.path.basename(p)[:-3]
        if n.startswith("_"):
            continue
        names.append(n)
    return names


def build_one(name, outdir):
    cgra = ROOT / "sim" / "soc" / "build" / ("proto_%s_dump.txt" % name)
    cpu = ROOT / "sim" / "soc" / "build" / ("proto_%s_cpu_dump.txt" % name)
    missing = [str(p) for p in (cgra, cpu) if not p.exists()]
    if missing:
        return None, missing
    out = Path(outdir) / ("compare_%s.csv" % name)
    cmd = [sys.executable, str(ROOT / "tools" / "proto_compare.py"),
           "--scenario", name, "--cgra", str(cgra), "--cpu", str(cpu),
           "--out", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return None, ["proto_compare 失败: %s" % r.stderr.strip().splitlines()[-1:]]
    with open(out, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row["scenario"] = name
    return rows, []


def read_old():
    if not LEDGER.exists():
        return {}
    with open(LEDGER, encoding="utf-8", newline="") as f:
        return {(r["scenario"], r["id"]): r for r in csv.DictReader(f)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    old = read_old()
    rows, skipped = [], []
    with tempfile.TemporaryDirectory() as td:
        for n in scenarios():
            got, missing = build_one(n, td)
            if got is None:
                skipped.append((n, missing))
                continue
            rows.extend(got)

    if skipped:
        for n, why in skipped:
            print("[skip] %-18s %s" % (n, why), file=sys.stderr)
        if len(skipped) == len(scenarios()):
            print("[error] 没有任何场景可用；先跑 make proto-all 再生成账本",
                  file=sys.stderr)
            return 1

    changed = []
    for r in rows:
        key = (r["scenario"], r["id"])
        o = old.get(key)
        if o is None:
            changed.append((key, "新增", "", r["cgra_cycles"] + "/" + r["cpu_cycles"]))
            continue
        for col in ("op", "impl", "cgra_cycles", "cpu_cycles",
                    "cpu_over_cgra", "verdict"):
            if o.get(col) != r[col]:
                changed.append((key, col, o.get(col), r[col]))

    print("场景 %d 个，账本 %d 行" % (len(scenarios()) - len(skipped), len(rows)))
    if changed:
        print("相对旧账本变化 %d 处：" % len(changed))
        for (sc, sid), col, before, after in changed[:20]:
            print("  %-18s %-4s %-14s %s -> %s" % (sc, sid, col, before, after))
        if len(changed) > 20:
            print("  ... 另有 %d 处" % (len(changed) - 20))
    else:
        print("与旧账本一致")

    totals = [r for r in rows if r["id"] == "TOTAL"]
    print("整链周期比（CPU/CGRA）：")
    for r in totals:
        print("  %-18s %8s x  %s" % (r["scenario"], r["cpu_over_cgra"], r["verdict"]))

    if a.dry_run:
        print("[dry-run] 未写 %s" % LEDGER)
        return 0 if not changed else 0

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print("[info] wrote %s" % LEDGER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
