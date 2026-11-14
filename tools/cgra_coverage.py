#!/usr/bin/env python3
"""CGRA 覆盖率：阵列到底接管了多少运算量，以及接管得值不值。

三个口径故意放在一起——单独看任何一个都会误判：

  1. 阶段覆盖率   = 阵列上的阶段数 / 总阶段数
     最直观也最容易骗人：nr_mimo 是 2/5 = 40%，但那两个阶段占 99% 的标量工作量。

  2. 工作量覆盖率 = Σ(阵列阶段的 CPU 周期) / Σ(全部阶段的 CPU 周期)
     **主指标**。以"同一算法在 CVA6 上要跑多少周期"作为工作量权重，回答
     "如果全交给 CPU，其中多大比例被阵列接走了"。

  3. 有效覆盖率   = Σ max(0, CPU周期 - CGRA周期)_阵列阶段 / Σ CPU周期
     只统计"真的变快了"的那部分。覆盖率高、有效覆盖率低，说明有阶段
     **上了阵列反而更慢**（典型：lte_cell_search 的短相关 0.24×）。

配套两个诊断量：
  - 覆盖段实际加速 = ΣCPU_阵列 / ΣCGRA_阵列；
  - 未覆盖残余 = CPU-only 阶段按工作量排序（下一步该优化谁）。

口径说明：
  - 分母用"逐阶段周期之和"，分子只算阵列阶段；整链 TOTAL 与阶段和之差是
    循环/收尾开销（`overhead`），单独报告，不摊进覆盖率。
  - 权重是 **CPU 周期**，不是指令数/MAC 数——这些 kernel 的标量周期与运算量
    近似成正比，但控制密集阶段会被高估。要严格按运算量算，需要每个 kernel
    的 OP 计数表（见文末说明），本工具暂不臆造。

用法：
    python3 tools/cgra_coverage.py
    python3 tools/cgra_coverage.py --json
    python3 tools/cgra_coverage.py --scenario nr_mimo --detail
"""
import argparse
import csv
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROTO = os.path.join(ROOT, "build", "proto")
LEDGER = os.path.join(PROTO, "compare_all.csv")


def load_ledger():
    if not os.path.exists(LEDGER):
        sys.exit("[FAIL] 缺少 %s：先 make proto-all 并用 tools/gen_proto_ledger.py"
                 % LEDGER)
    rows = list(csv.DictReader(open(LEDGER, encoding="utf-8")))
    per, totals = defaultdict(list), {}
    for r in rows:
        if r["id"] == "TOTAL":
            totals[r["scenario"]] = r
        else:
            per[r["scenario"]].append(r)
    return per, totals


def load_where(name):
    p = os.path.join(PROTO, name, "plan-base.json")
    if not os.path.exists(p):
        return None
    plan = json.load(open(p, encoding="utf-8"))
    return {s["id"]: s.get("where", "control-core") for s in plan["stages"]}


def scenario_stats(name, rows, total):
    where = load_where(name)
    if where is None:
        return None
    cpu = sum(int(r["cpu_cycles"]) for r in rows)
    cgra = sum(int(r["cgra_cycles"]) for r in rows)
    arr = [r for r in rows if where.get(int(r["id"])) == "array"]
    arr_cpu = sum(int(r["cpu_cycles"]) for r in arr)
    arr_cgra = sum(int(r["cgra_cycles"]) for r in arr)
    gain = sum(max(0, int(r["cpu_cycles"]) - int(r["cgra_cycles"])) for r in arr)
    # 负收益：上了阵列反而更慢的那部分工作量（g > c）
    loss = sum(int(r["cgra_cycles"]) - int(r["cpu_cycles"]) for r in arr
               if int(r["cgra_cycles"]) > int(r["cpu_cycles"]))
    # 剩余代价：阵列阶段自己也要花的时间（min(c,g) 的和）
    residual = sum(min(int(r["cpu_cycles"]), int(r["cgra_cycles"])) for r in arr)
    # 标为 CPU、但 CGRA 路径明显更快的阶段：说明该阶段内部用了阵列（例如
    # equalise_demap 里藏了一次 Bluestein），或者两条路径的 C 实现不同。
    # 这部分收益不在"声明覆盖率"里，单独统计，避免把账算错。
    hidden = [r for r in rows
              if where.get(int(r["id"])) != "array"
              and int(r["cgra_cycles"]) * 1.2 < int(r["cpu_cycles"])]
    hidden_gain = sum(int(r["cpu_cycles"]) - int(r["cgra_cycles"]) for r in hidden)
    cpu_end = int(total["cpu_cycles"]) if total else cpu
    cgra_end = int(total["cgra_cycles"]) if total else cgra
    resid = sorted((r for r in rows if where.get(int(r["id"])) != "array"),
                   key=lambda r: -int(r["cpu_cycles"]))
    if not arr:
        verdict = "无阵列实现"
    elif arr_cgra and arr_cpu / float(arr_cgra) < 1.0:
        verdict = "覆盖但更慢"
    elif arr_cpu / float(cpu) < 0.5:
        verdict = "覆盖不足"
    else:
        verdict = "有效"
    return {
        "scenario": name,
        "stages": len(rows),
        "array_stages": len(arr),
        "stage_cov": len(arr) / float(len(rows)) if rows else 0.0,
        "cpu_stage_sum": cpu,
        "cgra_stage_sum": cgra,
        "cpu_total": cpu_end,
        "cgra_total": cgra_end,
        "overhead_cycles": cpu_end - cpu,
        "speedup": (cpu_end / float(cgra_end)) if cgra_end else 0.0,
        "work_cov": (arr_cpu / float(cpu)) if cpu else 0.0,
        "gain_cov": (gain / float(cpu)) if cpu else 0.0,
        "array_speedup": (arr_cpu / float(arr_cgra)) if arr_cgra else 0.0,
        "lost_cycles": loss,
        "residual_cost": (residual / float(cpu)) if cpu else 0.0,
        "neg_cov": ((loss / float(cpu)) if cpu else 0.0),
        "hidden_ops": [r["op"] for r in hidden],
        "hidden_gain": (hidden_gain / float(cpu)) if cpu else 0.0,
        "verdict": verdict,
        "residual": [{"id": int(r["id"]), "op": r["op"],
                      "cpu": int(r["cpu_cycles"])} for r in resid[:3]],
        "stages_detail": [{"id": int(r["id"]), "op": r["op"], "impl": r["impl"],
                           "where": where.get(int(r["id"]), "?"),
                           "cpu": int(r["cpu_cycles"]),
                           "cgra": int(r["cgra_cycles"])} for r in rows],
    }


def aggregate(stats):
    cpu = sum(s["cpu_stage_sum"] for s in stats)
    arr_cpu = sum(s["work_cov"] * s["cpu_stage_sum"] for s in stats)
    gain = sum(s["gain_cov"] * s["cpu_stage_sum"] for s in stats)
    hidden = sum(s["hidden_gain"] * s["cpu_stage_sum"] for s in stats)
    residual = sum(s["residual_cost"] * s["cpu_stage_sum"] for s in stats)
    neg = sum(s["neg_cov"] * s["cpu_stage_sum"] for s in stats)
    arr_cgra = sum(s["array_speedup"] and
                   (s["work_cov"] * s["cpu_stage_sum"]) / s["array_speedup"] or 0
                   for s in stats)
    cpu_end = sum(s["cpu_total"] for s in stats)
    cgra_end = sum(s["cgra_total"] for s in stats)
    return {
        "scenarios": len(stats),
        "cpu_stage_sum": cpu,
        "cpu_total": cpu_end,
        "cgra_total": cgra_end,
        "speedup": cpu_end / float(cgra_end) if cgra_end else 0.0,
        "work_cov": arr_cpu / float(cpu) if cpu else 0.0,
        "gain_cov": gain / float(cpu) if cpu else 0.0,
        "hidden_gain": hidden / float(cpu) if cpu else 0.0,
        "residual_cost": residual / float(cpu) if cpu else 0.0,
        "neg_cov": neg / float(cpu) if cpu else 0.0,
        "array_speedup": arr_cpu / float(arr_cgra) if arr_cgra else 0.0,
    }


def op_table(per):
    acc = defaultdict(lambda: {"n": 0, "n_arr": 0, "cpu": 0, "arr_cpu": 0,
                               "arr_cgra": 0})
    for name, rows in per.items():
        where = load_where(name)
        if where is None:
            continue
        for r in rows:
            a = acc[r["op"]]
            a["n"] += 1
            a["cpu"] += int(r["cpu_cycles"])
            if where.get(int(r["id"])) == "array":
                a["n_arr"] += 1
                a["arr_cpu"] += int(r["cpu_cycles"])
                a["arr_cgra"] += int(r["cgra_cycles"])
    out = []
    for op, a in acc.items():
        out.append({
            "op": op, "n": a["n"], "n_array": a["n_arr"], "cpu": a["cpu"],
            "covered": a["arr_cpu"] / float(a["cpu"]) if a["cpu"] else 0.0,
            "array_speedup": (a["arr_cpu"] / float(a["arr_cgra"]))
            if a["arr_cgra"] else 0.0,
        })
    return sorted(out, key=lambda x: -x["cpu"])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    per, totals = load_ledger()
    names = [a.scenario] if a.scenario else sorted(per)
    stats = []
    for n in names:
        s = scenario_stats(n, per.get(n, []), totals.get(n))
        if s is None:
            print("[skip] %s：没有 plan-base.json（未在 GPCP 侧构建）" % n,
                  file=sys.stderr)
            continue
        stats.append(s)
    if not stats:
        sys.exit("[FAIL] 没有可统计的场景")

    if a.json:
        print(json.dumps({"scenarios": stats, "total": aggregate(stats),
                          "ops": op_table(per)}, indent=1, ensure_ascii=False))
        return 0

    print("%-18s %5s %4s %7s %9s %9s %8s %10s %s" % (
        "场景", "阶段", "阵列", "阶段覆盖", "工作量覆盖", "有效覆盖",
        "覆盖段加速", "端到端", "判定"))
    print("-" * 96)
    for s in stats:
        print("%-18s %5d %4d %6.0f%% %8.1f%% %8.1f%% %9.2fx %9.2fx  %s" % (
            s["scenario"], s["stages"], s["array_stages"], s["stage_cov"] * 100,
            s["work_cov"] * 100, s["gain_cov"] * 100, s["array_speedup"],
            s["speedup"], s["verdict"]))
    t = aggregate(stats)
    print("-" * 96)
    print("%-18s %5s %4s %7s %8.1f%% %8.1f%% %9.2fx %9.2fx" % (
        "合计（工作量加权）", "", "", "", t["work_cov"] * 100,
        t["gain_cov"] * 100, t["array_speedup"], t["speedup"]))
    gap = (t["work_cov"] - t["gain_cov"]) * 100
    print("\n分解：覆盖 %.1f%% = 增益 %.1f%% + 剩余代价 %.1f%% − 负收益 %.1f%%"
          % (t["work_cov"] * 100, t["gain_cov"] * 100,
             t["residual_cost"] * 100, t["neg_cov"] * 100))
    print("另有 %.1f%% 的收益来自「标为 CPU 但更快」的阶段（内部用了阵列或实现不同），"
          "不计入上面的覆盖率" % (t["hidden_gain"] * 100))
    print("未被覆盖的工作量 ≈ %.1f%%" % (100 - t["work_cov"] * 100))

    print("\n按算子（跨场景，按 CPU 工作量排序）：")
    print("%-18s %4s %6s %10s %10s" % ("算子", "样本", "上阵列", "工作量", "覆盖段加速"))
    for o in op_table(per):
        print("%-18s %4d %6d %9.1f%% %9s" % (
            o["op"], o["n"], o["n_array"], o["covered"] * 100,
            "%.2fx" % o["array_speedup"] if o["n_array"] else "-"))

    if a.detail and len(stats) == 1:
        s = stats[0]
        print("\n%s 逐阶段（链开销 %d 周期，未摊入覆盖率）：" % (
            s["scenario"], s["overhead_cycles"]))
        print("%-4s %-18s %-14s %-13s %10s %10s" % ("id", "op", "impl", "落点", "CPU", "CGRA"))
        for d in s["stages_detail"]:
            print("%-4d %-18s %-14s %-13s %10d %10d" % (
                d["id"], d["op"], d["impl"] or "-", d["where"], d["cpu"], d["cgra"]))
        print("\n未覆盖残余（CPU-only，按工作量）：")
        for r in s["residual"]:
            print("  %-18s %10d 周期  %.1f%%" % (r["op"], r["cpu"],
                                                 r["cpu"] * 100.0 / s["cpu_stage_sum"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
