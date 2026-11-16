#!/usr/bin/env python3
# --- tools path bootstrap (subdirs import root modules) ---
import os as _os, sys as _sys
_T = _os.path.dirname(_os.path.abspath(__file__))
_TOOLS = _T if _os.path.basename(_T) == "tools" else _os.path.dirname(_T)
for _p in (_TOOLS, _os.path.join(_TOOLS, "models"), _os.path.join(_TOOLS, "checks"),
           _os.path.join(_os.path.dirname(_TOOLS), "cgra", "tools")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
_ROOT = _os.path.dirname(_TOOLS)
# --- end bootstrap ---
"""CGRA 利用率：阵列分到活之后，到底有多少时间在真算。

和覆盖率（docs/performance/README.md）是两个问题：
  - 覆盖率回答"多少工作量交给了阵列"（分子是工作量）；
  - 利用率回答"阵列接活之后忙不忙"（分子是时间）。

四个口径，分母各不相同，引用时必须写清是哪一个：

  1. 时间利用率  U_time = array_busy / 总周期
     阵列真正在执行指令的拍数占比。低 → 瓶颈在搬运/标量编排，不在算力。

  2. 有效槽率    U_slot = (inst_issued - inst_nop) / inst_issued
     阵列忙的时候，指令槽里有多少不是 NOP。低 → 微码气泡多（调度/映射问题）。

  3. PE 平均活跃 U_pe_run = Σ pe_active[0..15] / (16 * array_busy)
     阵列在跑的时候，16 个 PE 平均有几个在动。等于 U_slot（同一批计数的两种读法），
     用来交叉校验。

  4. 总算力利用率 U_pe_all = Σ pe_active[0..15] / (16 * 总周期)
     **最严格的"CGRA 利用率"**：把整段时间 × 全部 PE 当分母，只有真算的部分算数。
     U_pe_all = U_time * U_pe_run。

配套的互斥状态（每拍只归一类，见 docs/protocols/README.md）：
  阵列执行 / DMA 传输 / CPU 执行，三者相加 ≈ 总周期。

用法：
    python3 tools/cgra_utilization.py
    python3 tools/cgra_utilization.py --json
    python3 tools/cgra_utilization.py --scenario nr_pdsch --detail
"""
import argparse
import csv
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "proto"))
from proto_eval import parse_perf, read_dump  # noqa: E402

BUILD = os.path.join(ROOT, "sim", "soc", "build")
N_PE = 16

# perf full[] 的字段索引（docs/protocols/README.md）
I_CYC, I_BUSY, I_RUNS, I_ISSUED, I_NOP = 0, 1, 2, 3, 4
I_MAC, I_LD, I_ST, I_BANK, I_WORD = 5, 7, 8, 12, 13
I_DMA, I_CPU = 54, 56
PE_ACTIVE = range(22, 38)


def scenarios():
    out = []
    for p in sorted(os.listdir(os.path.dirname(BUILD))):
        pass
    d = os.path.join(ROOT, "proto", "scenarios")
    for f in sorted(os.listdir(d)):
        if f.endswith(".py") and not f.startswith("_"):
            out.append(f[:-3])
    return out


def one(name):
    p = os.path.join(BUILD, "proto_%s_dump.txt" % name)
    if not os.path.exists(p):
        return None, "缺 dump"
    perf = parse_perf(read_dump(p))
    if not perf or not perf.get("full"):
        return None, "无性能计数块"
    g = perf["full"]
    if len(g) <= I_CPU:
        return None, "计数块过短"
    cyc, busy = g[I_CYC], g[I_BUSY]
    if cyc <= 0:
        return None, "周期为 0"
    pe = [g[i] for i in PE_ACTIVE]
    pe_sum = sum(pe)
    dma, cpu = g[I_DMA], g[I_CPU]
    issued, nop = g[I_ISSUED], g[I_NOP]
    stage_peak = max(pe) if pe else 0
    stage_min = min(pe) if pe else 0
    return {
        "scenario": name,
        "cycles": cyc,
        "busy": busy,
        "runs": g[I_RUNS],
        "dma": dma,
        "cpu": cpu,
        "issued": issued,
        "nop": nop,
        "mac": g[I_MAC],
        "ld": g[I_LD],
        "st": g[I_ST],
        "bank_conf": g[I_BANK],
        "pe_sum": pe_sum,
        "pe": pe,
        "pe_max": stage_peak,
        "pe_min": stage_min,
        "u_time": busy / float(cyc),
        "u_slot": ((issued - nop) / float(issued)) if issued else 0.0,
        "u_pe_run": (pe_sum / float(N_PE * busy)) if busy else 0.0,
        "u_pe_all": pe_sum / float(N_PE * cyc),
        "sh_dma": dma / float(cyc),
        "sh_cpu": cpu / float(cyc),
        "mac_share": (g[I_MAC] / float(issued)) if issued else 0.0,
        "conflict": (g[I_BANK] / float(busy)) if busy else 0.0,
        "per_run": (busy / float(g[I_RUNS])) if g[I_RUNS] else 0.0,
    }, ""


def breakdown(name, busy):
    """把整链时间拆成：阵列执行 / 阵列阶段内的编排 / 纯标量阶段 / 收尾。"""
    lp = os.path.join(ROOT, "build", "proto", name, "plan-base.json")
    lg = os.path.join(ROOT, "build", "proto", "compare_all.csv")
    if not (os.path.exists(lp) and os.path.exists(lg)):
        return None
    plan = json.load(open(lp, encoding="utf-8"))
    where = {s["id"]: s.get("where", "control-core") for s in plan["stages"]}
    cyc = {}
    for r in csv.DictReader(open(lg, encoding="utf-8")):
        if r["scenario"] == name and r["id"] != "TOTAL":
            cyc[int(r["id"])] = int(r["cgra_cycles"])
    arr_stage = sum(cyc.get(k, 0) for k, v in where.items() if v == "array")
    cpu_stage = sum(cyc.get(k, 0) for k, v in where.items() if v != "array")
    return {"array_exec": busy,
            "array_orch": max(0, arr_stage - busy),
            "scalar_stage": cpu_stage,
            "array_stage": arr_stage}


def aggregate(rows):
    cyc = sum(r["cycles"] for r in rows)
    busy = sum(r["busy"] for r in rows)
    pe = sum(r["pe_sum"] for r in rows)
    issued = sum(r["issued"] for r in rows)
    nop = sum(r["nop"] for r in rows)
    return {
        "scenarios": len(rows),
        "cycles": cyc,
        "busy": busy,
        "u_time": busy / float(cyc) if cyc else 0.0,
        "u_slot": ((issued - nop) / float(issued)) if issued else 0.0,
        "u_pe_run": (pe / float(N_PE * busy)) if busy else 0.0,
        "u_pe_all": pe / float(N_PE * cyc) if cyc else 0.0,
        "sh_dma": sum(r["dma"] for r in rows) / float(cyc) if cyc else 0.0,
        "sh_cpu": sum(r["cpu"] for r in rows) / float(cyc) if cyc else 0.0,
        "runs": sum(r["runs"] for r in rows),
    }


def bar(v, width=18):
    n = int(round(v * width))
    return "#" * n + "." * (width - n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--scenario")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()

    names = [a.scenario] if a.scenario else scenarios()
    rows, skipped = [], []
    for n in names:
        r, why = one(n)
        (rows.append(r) if r else skipped.append((n, why)))
    if not rows:
        sys.exit("[FAIL] 没有可用的性能计数块")

    if a.json:
        print(json.dumps({"usage": rows, "total": aggregate(rows),
                          "skipped": skipped}, indent=1, ensure_ascii=False))
        return 0

    if a.detail:
        for r in rows:
            print("=== %s" % r["scenario"])
            print("  总周期 %d，阵列忙 %d（%.1f%%），启动 %d 次，每次平均 %.0f 拍"
                  % (r["cycles"], r["busy"], r["u_time"] * 100, r["runs"], r["per_run"]))
            print("  互斥状态：阵列 %.1f%% / DMA %.1f%% / CPU %.1f%%"
                  % (r["u_time"] * 100, r["sh_dma"] * 100, r["sh_cpu"] * 100))
            print("  指令槽：有效 %.1f%%（NOP %d/%d），MAC %.1f%%，bank 冲突 %.1f%%"
                  % (r["u_slot"] * 100, r["nop"], r["issued"],
                     r["mac_share"] * 100, r["conflict"] * 100))
            print("  PE 活跃：min %d / max %d（阵列运行时均值 %.1f%%）"
                  % (r["pe_min"], r["pe_max"], r["u_pe_run"] * 100))
            print("  总算力利用率：%.1f%%" % (r["u_pe_all"] * 100))
        print()
        return 0

    print("场景              时间利用率  有效槽率  PE活跃(跑时)  总算力利用率   DMA占比  CPU占比  "
          "启动  每次拍")
    print("-" * 104)
    for r in sorted(rows, key=lambda x: -x["u_pe_all"]):
        print("%-17s %6.1f%% %9.1f%% %9.1f%% %11.1f%% %9.1f%% %7.1f%% %6d %7.0f"
              % (r["scenario"], r["u_time"] * 100, r["u_slot"] * 100,
                 r["u_pe_run"] * 100, r["u_pe_all"] * 100, r["sh_dma"] * 100,
                 r["sh_cpu"] * 100, r["runs"], r["per_run"]))
    t = aggregate(rows)
    print("-" * 104)
    print("%-17s %6.1f%% %9.1f%% %9.1f%% %11.1f%% %9.1f%% %7.1f%% %6d"
          % ("合计(按周期加权)", t["u_time"] * 100, t["u_slot"] * 100,
             t["u_pe_run"] * 100, t["u_pe_all"] * 100, t["sh_dma"] * 100,
             t["sh_cpu"] * 100, t["runs"]))
    print()
    print("时间去向：%s  阵列 %.1f%%" % (bar(t["u_time"]), t["u_time"] * 100))
    print("           %s  DMA  %.1f%%" % (bar(t["sh_dma"]), t["sh_dma"] * 100))
    print("           %s  CPU  %.1f%%" % (bar(t["sh_cpu"]), t["sh_cpu"] * 100))
    print("\n总算力利用率 = 时间利用率 × PE 活跃率 = %.1f%% × %.1f%% = %.1f%%"
          % (t["u_time"] * 100, t["u_pe_run"] * 100, t["u_pe_all"] * 100))

    bd = [breakdown(r["scenario"], r["busy"]) for r in rows]
    bd = [x for x in bd if x]
    if bd:
        cyc = float(t["cycles"])
        a1 = sum(x["array_exec"] for x in bd)
        a2 = sum(x["array_orch"] for x in bd)
        a3 = sum(x["scalar_stage"] for x in bd)
        rest = t["cycles"] - sum(x["array_stage"] + x["scalar_stage"] for x in bd)
        print("\n整链时间去向（按阶段落点拆）：")
        print("  阵列真正执行        %9d  %5.1f%%" % (a1, 100 * a1 / cyc))
        print("  阵列阶段内的编排    %9d  %5.1f%%   （DMA + CPU 喂/收，阵列闲着）"
              % (a2, 100 * a2 / cyc))
        print("  纯标量阶段          %9d  %5.1f%%   （没上阵列的工作）"
              % (a3, 100 * a3 / cyc))
        print("  链外收尾/循环        %9d  %5.1f%%" % (rest, 100 * rest / cyc))
    if skipped:
        print("\n未计入：%s" % ", ".join("%s(%s)" % s for s in skipped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
