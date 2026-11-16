#!/usr/bin/env python3
"""docs/protocols/README.md 的负载模型：把一个时隙的实测代价摊成 40 格时间轴。

它只做"时间轴 + 单服务器队列"，不重新定义协议，也不猜硬件：
  * 每格来多少作业由场景的 `traffic(**kw)` 决定（arrival / jobs_per_slot / slots）；
  * 每个作业的代价是**硬件实测**的整链周期（proto_eval 传进来，不在这里估）；
  * 判据：第 k 格到达的工作是否在第 k+1 格的截止点（= (k+1)·预算）之前做完。

    python3 proto/load.py --scenario nr_pdsch --cost 4054949 --slots 40
    # 一般由 proto_eval.py 调用：它写完 report.md 的同时写 timeline.json / timeline.csv

口径（故意写死在这里，避免散落）：
  * 单服务器、非抢占：上一格没做完的作业继续占用，后面的格排队；
  * 不做跨格状态（HARQ 软合并会改变后续格的代价）——那是阶段 2 的事；
  * `single`（一次到达）场景不适用时间轴，返回空。
"""
import argparse
import json
import math
import os
import random
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def profile(traffic, slots):
    """每格的到达作业数。single/none 返回 []（不适用时间轴）。"""
    arrival = (traffic or {}).get("arrival", "single")
    per = int((traffic or {}).get("jobs_per_slot", 1))
    if arrival in ("single", "none", None, ""):
        return []
    if arrival == "periodic":
        return [per] * slots
    rng = random.Random(int((traffic or {}).get("seed", 1)))
    if arrival == "poisson":
        lam = float((traffic or {}).get("lambda", per))
        return [_poisson(rng, lam) for _ in range(slots)]
    if arrival == "on-off":
        on = float((traffic or {}).get("on", 0.5))
        lam = float((traffic or {}).get("lambda", per))
        return [_poisson(rng, lam) if rng.random() < on else 0
                for _ in range(slots)]
    raise SystemExit("load: 未知的 arrival=%r（single/periodic/poisson/on-off）"
                     % arrival)


def _poisson(rng, lam):
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        k += 1
        p *= rng.random()
        if p <= limit:
            return k - 1


def simulate(cost, counts, budget):
    """单服务器队列。返回 (逐格记录, 超时格数)。"""
    t = 0
    rows = []
    late = 0
    for k, n in enumerate(counts):
        deadline = (k + 1) * budget
        t += n * cost
        backlog = t - deadline
        ok = backlog <= 0
        if not ok:
            late += 1
        rows.append({"slot": k, "jobs": n, "cost": n * cost, "finish": t,
                     "deadline": deadline, "backlog": max(0, backlog),
                     "on_time": ok})
    return rows, late


def dump(outdir, rows, late, cost, budget, slots):
    """写 timeline.json + timeline.csv（逐格记录，docs/protocols/README.md）。"""
    os.makedirs(outdir, exist_ok=True)
    summary = {
        "slots": slots,
        "jobs_per_slot": [r["jobs"] for r in rows],
        "cost_per_job": cost,
        "budget_cycles": budget,
        "on_time_rate": (slots - late) / float(slots) if slots else 1.0,
        "late_slots": late,
        "worst_backlog": max((r["backlog"] for r in rows), default=0),
        "last_deadline": rows[-1]["deadline"] if rows else 0,
        "last_finish": rows[-1]["finish"] if rows else 0,
    }
    with open(os.path.join(outdir, "timeline.json"), "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "slots": rows}, f, indent=1,
                  ensure_ascii=False)
    with open(os.path.join(outdir, "timeline.csv"), "w", encoding="utf-8") as f:
        f.write("slot,jobs,cost,finish,deadline,backlog,on_time\n")
        for r in rows:
            f.write("%d,%d,%d,%d,%d,%d,%d\n"
                    % (r["slot"], r["jobs"], r["cost"], r["finish"],
                       r["deadline"], r["backlog"], 1 if r["on_time"] else 0))
    summary["rows"] = rows          # 只给报告渲染用，不进 timeline.json 的 summary
    return summary


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--cost", type=int, required=True, help="每作业的实测周期")
    ap.add_argument("--slots", type=int, default=40)
    a = ap.parse_args(argv)
    pdir = os.path.join(_ROOT, "build", "proto", a.scenario)
    with open(os.path.join(pdir, "ir.json"), encoding="utf-8") as f:
        ir = json.load(f)
    with open(os.path.join(pdir, "plan-base.json"), encoding="utf-8") as f:
        plan = json.load(f)
    budget = int(round(float(ir["budget"]["slot_ms"])
                       * int(plan["target"]["clock_hz"]) / 1000.0))
    counts = profile(ir.get("traffic"), a.slots)
    if not counts:
        print("load: 场景 %s 是单次到达，没有时间轴" % a.scenario)
        return 0
    rows, late = simulate(a.cost, counts, budget)
    s = dump(pdir, rows, late, a.cost, budget, a.slots)
    print("load: %d 格 / 每格 %d 作业 / 每作业 %d 周期 / 预算 %d 周期"
          % (s["slots"], counts[0], a.cost, budget))
    print("      按时率 %.1f%%（超时 %d 格，最差积压 %d 周期 = %.1f 格）"
          % (100 * s["on_time_rate"], late, s["worst_backlog"],
             s["worst_backlog"] / float(budget)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
