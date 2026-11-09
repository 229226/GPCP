#!/usr/bin/env python3
"""docs/20 §6 第 6 步：判据 + 按时率 + 阶段占比 + 差距表 -> report.md。

    # 单跑协议运行器（模型判据 + 预算 + 阶段表）
    python3 proto/proto_eval.py --scenario nr_pdcch --dump build/soc/proto_nr_pdcch_dump.txt

    # 阶段 0 的等价门：再和人写基准逐值对账（周期 ±1%）
    python3 proto/proto_eval.py --scenario nr_pdcch --dump ... --ref build/soc/pdcch_loop_dump.txt

    python3 proto/proto_eval.py --scenario nr_pdcch --ref-gate   # 打印参照门名（Makefile 用）

判据分三层，缺一层都不算过：
  1. 模型层：golden.json 的锚点与比特（由 tools/nr_pdcch_tx.py 独立算出）；
  2. 平台层：errors == 0、params_hash 一致（旧二进制不能服务新激励）、预算内；
  3. 等价层（可选）：与手写 benchmark 的每个锚点逐值相同、周期差 ≤ 1%。
"""
import argparse
import importlib.util
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import load as LOAD                     # noqa: E402

MARKER = 0x50524F54          # 'PROT'
PAYLOAD_HDR = 10             # [10..] 每阶段 (id, cycles, runs)，其后是锚点块
STAGE_WORDS = 3
CYCLES_TOL = 0.01


def _model(scenario):
    path = os.path.join(_HERE, "models", scenario + ".py")
    spec = importlib.util.spec_from_file_location("proto_model_" + scenario, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_dump(path):
    words = []
    for line in open(path):
        p = line.split()
        if len(p) >= 3 and p[0] == "word" and p[2].startswith("0x"):
            w = int(p[2], 16)
            words.append(w & 0xFFFFFFFF)
            words.append((w >> 32) & 0xFFFFFFFF)
    return words


class Report:
    def __init__(self):
        self.rows = []
        self.ok = True

    def add(self, name, want, got, ok, note=""):
        self.rows.append((name, str(want), str(got), "OK" if ok else "FAIL", note))
        self.ok &= bool(ok)
        return ok

    def eq(self, name, got, want, note=""):
        return self.add(name, want, got, got == want, note)

    def window(self, name, got, lo, hi, note=""):
        return self.add(name, "[%d, %d]" % (lo, hi), got, lo <= got <= hi, note)

    def bits(self, name, got, want, note=""):
        bad = sum(1 for a, b in zip(got, want) if a != b) + abs(len(got) - len(want))
        return self.add(name, "%d bit" % len(want), "%d/%d 不同" % (bad, len(want)),
                        bad == 0, note)


def write_report(path, scenario, plan, ir, rep, stages, ref_rows, cycles, budget, dma,
                 timeline=None):
    tgt = plan["target"]
    arrival = (ir.get("traffic") or {}).get("arrival", "single")
    enforced = arrival not in ("single", "none", None, "")
    on_time = (budget > 0 and cycles <= budget)
    lines = [
        "# 协议基准报告 — %s（%s/%s）" % (scenario, ir.get("rat"), ir.get("direction")),
        "",
        "> 生成物：`proto/proto_eval.py`（docs/20 §6）。不要手改，重跑 `make proto` 覆盖。",
        "",
        "- 目标：`%s`（%dx%d = %d PE，%d bank × %d 字，IMEM %d 拍，%d MHz）"
        % (tgt.get("source", "?"), tgt["rows"], tgt["cols"], tgt["n_pes"],
           tgt["banks"], tgt["words_per_bank"], tgt["imem_cycles"],
           tgt.get("clock_hz", 0) // 1000000),
        "- IR 摘要：`%s`" % plan.get("ir_sha", "?"),
        "- 参数摘要：`%s`" % plan.get("params_hash", "?"),
        "",
        "## 判据",
        "",
        "| 项 | 期望 | 实测 | 结论 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for name, want, got, verdict, note in rep.rows:
        lines.append("| %s | %s | %s | %s | %s |" % (name, want, got, verdict, note))
    lines += [
        "",
        "## 按时率",
        "",
        "- 每时隙预算：**%d 周期**（%s ms × %d Hz）"
        % (budget, ir.get("budget", {}).get("slot_ms", "?"), tgt.get("clock_hz", 0)),
        "- 本次：**%d 周期**（占预算 %.1f%%，%s）→ %s%s"
        % (cycles, 100.0 * cycles / budget if budget else 0.0,
           "超 %.1f×" % (cycles / float(budget)) if budget and cycles > budget
           else "在预算内",
           "按时" if on_time else "**超时**",
           "" if enforced else "（单次到达：阶段 0 只报不判，负载模型见 docs/20 §5）"),
        "",
    ]
    if timeline:
        lines += [
            "### 负载时间轴（docs/20 §5）",
            "",
            "- 到达模型：`%s`，每格 %d 个作业，共 **%d 格**"
            % ((ir.get("traffic") or {}).get("arrival", "?"),
               timeline["jobs_per_slot"][0], timeline["slots"]),
            "- 每作业代价：**%d 周期**（硬件实测的整链）" % timeline["cost_per_job"],
            "- **按时率 %.1f%%**（超时 %d 格），最差积压 **%d 周期 = %.1f 格**"
            % (100 * timeline["on_time_rate"], timeline["late_slots"],
               timeline["worst_backlog"],
               timeline["worst_backlog"] / float(budget or 1)),
            "- 逐格记录：`timeline.csv`（%d 行）" % timeline["slots"],
            "",
            "| 格 | 作业 | 代价 | 完成时刻 | 截止 | 积压 | 按时 |",
            "|---|---|---|---|---|---|---|",
        ]
        rows = timeline.get("rows") or []
        for r in rows[:3] + rows[3:4] + rows[-2:]:
            lines.append("| %d | %d | %d | %d | %d | %d | %s |"
                         % (r["slot"], r["jobs"], r["cost"], r["finish"],
                            r["deadline"], r["backlog"], "是" if r["on_time"] else "否"))
        lines += ["", "> 单服务器、非抢占：上一格没做完的继续占用，后面的格排队。"
                  "跨格状态（HARQ 软合并）留到阶段 2。"]
    lines += [
        "",
        "## 阶段占比",
        "",
        "| # | 阶段 | 计划落点 | 实现 | 计划阵列运行 | 实测周期 | 占比 | 实测阵列运行 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    total = float(cycles) or 1.0
    for i, st in enumerate(stages):
        lines.append("| %d | %s | %s | %s | %d | %d | %.1f%% | %d |"
                     % (st["id"], st["op"], st["where"], st["impl"] or "-",
                        st["planned"], st["cycles"], 100.0 * st["cycles"] / total,
                        st["runs"]))
    lines.append("| | **合计** | | | **%d** | **%d** | 100%% | **%d** |"
                 % (sum(s["planned"] for s in stages),
                    sum(s["cycles"] for s in stages), sum(s["runs"] for s in stages)))
    gaps = [s for s in stages
            if s["planned"] != s["runs"]
            or (s["where"] == "control-core" and s["runs"] > 0)]
    lines += [
        "",
        "## 差距表（计划 vs 实测）",
        "",
    ]
    if gaps:
        lines += ["| # | 阶段 | 计划落点 | 计划运行 | 实测运行 | 判断 |",
                  "|---|---|---|---|---|---|"]
        for s in gaps:
            why = []
            if s["planned"] != s["runs"]:
                if s["planned"]:
                    why.append("运行数估偏 %.1f×" % (s["runs"] / float(s["planned"])))
                else:
                    why.append("计划里没有阵列工作，实测 %d 次" % s["runs"])
            if s["where"] == "control-core" and s["runs"] > 0:
                why.append("落点判断偏：标量阶段里还有阵列工作")
            lines.append("| %d | %s | %s | %d | %d | %s（阶段 4 自动改善） |"
                         % (s["id"], s["op"], s["where"], s["planned"], s["runs"],
                            "；".join(why)))
        lines += ["", "> 这些是 `proto/lower.py` 的**估算**与真实执行的差，不是 bug；"
                  "docs/20 §7 把它们列为阶段 4 的自动搜索目标。"]
    else:
        lines.append("（无差异：每个阶段的计划运行数与实测一致，且没有"
                     "「标量落点里跑阵列」的情况。）")
    if ref_rows:
        lines += ["", "## 与手写基准的对账（等价门）", "",
                  "| 项 | 手写 | 协议运行器 | 结论 |", "|---|---|---|---|"]
        for name, refv, gotv, verdict in ref_rows:
            lines.append("| %s | %s | %s | %s |" % (name, refv, gotv, verdict))
        lines += ["", "> 周期判据是 ±%.0f%%（docs/20 §7）；其余锚点必须逐值相同。"
                  % (CYCLES_TOL * 100)]
    lines += ["",
              "## 记账证据（给 tools/devlog.py 用）",
              "",
              "`%s：%d 周期 / %d 次阵列运行 / %d 次 DMA / 按时率 %s`"
              % (scenario, cycles, sum(s["runs"] for s in stages), dma,
                 "100%" if on_time else "0%"),
              ""]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--dump", help="协议运行器的结果 dump")
    ap.add_argument("--ref", help="手写 benchmark 的 dump（等价门）；auto = 用场景模型声明的路径")
    ap.add_argument("--ref-gate", action="store_true", help="只打印参照门的 make 目标名")
    ap.add_argument("--branch", default="base", help="用哪个 plan-<branch>.json")
    a = ap.parse_args(argv)

    model = _model(a.scenario)
    if a.ref_gate:
        print(getattr(model, "REF_GATE", ""))
        return 0
    if not a.dump:
        print("usage: proto_eval.py --scenario S --dump D [--ref R]", file=sys.stderr)
        return 2
    if a.ref == "auto":
        rel = getattr(model, "REF_DUMP", "")
        if not rel:
            print("[FAIL] 场景 %s 没有声明 REF_DUMP" % a.scenario, file=sys.stderr)
            return 2
        a.ref = os.path.join(_ROOT, rel)

    pdir = os.path.join(_ROOT, "build", "proto", a.scenario)
    with open(os.path.join(pdir, "plan-%s.json" % a.branch), encoding="utf-8") as f:
        plan = json.load(f)
    with open(os.path.join(pdir, "golden.json"), encoding="utf-8") as f:
        gold = json.load(f)
    with open(os.path.join(pdir, "ir.json"), encoding="utf-8") as f:
        ir = json.load(f)

    n = len(plan["stages"])
    stage0 = PAYLOAD_HDR                 # 每阶段 (id, cycles, runs)
    base = PAYLOAD_HDR + STAGE_WORDS * n  # 锚点块起点
    # 锚点块的布局由场景模型声明（SPEC），这里只按声明取数——加场景不用改本文件。
    spec = getattr(model, "SPEC", None)
    if spec is None:
        print("[FAIL] proto/models/%s.py 没有声明 SPEC" % a.scenario)
        return 1
    need = 0
    for _, off, _k in spec["scalars"]:
        need = max(need, off + 1)
    for _, off, key, _note in spec["bits"]:
        need = max(need, off + len(gold["bits"][key]))
    w = read_dump(a.dump)
    if len(w) < base + need:
        print("  dump too short: %d words（至少要 %d）" % (len(w), base + need))
        return 1

    rep = Report()
    rep.add("marker", "0x%08x" % MARKER, "0x%08x" % w[0], w[0] == MARKER)
    rep.eq("errors", w[1], 0, "运行器自报的错误数")
    rep.add("完成标志", "0x600d", "0x%04x" % w[9], w[9] == 0x600D)
    rep.eq("params_hash", "0x%08x" % w[8], gold["params_hash"],
           "旧二进制不能服务新激励")

    # ---- 阶段表：顺序、周期合计、每阶段阵列运行数 ----
    stages = []
    total_stage_cycles = 0
    for i, st in enumerate(plan["stages"]):
        sid, cyc, runs = w[stage0 + STAGE_WORDS * i:][:3]
        stages.append({"id": st["id"], "op": st["op"], "where": st["where"],
                       "impl": st.get("impl") or "",
                       "planned": int(st.get("array_runs", 0)),
                       "cycles": cyc, "runs": runs})
        total_stage_cycles += cyc
        if sid != st["id"]:
            rep.add("阶段 %d 的编号" % i, st["id"], sid, False, "阶段表被改坏了")
    cycles = w[2]
    rep.add("阶段周期合计 ≤ 总周期", "≤ %d" % cycles, total_stage_cycles,
            total_stage_cycles <= cycles, "差值 = 循环与收尾开销")

    # ---- 模型锚点（布局由 SPEC 声明）----
    anchors = {}
    for name, off, kind in spec["scalars"]:
        got = w[base + off]
        anchors[name] = got
        if kind == "window":
            lo, hi = gold["windows"][name]
            rep.window(name, got, lo, hi, "定点增益窗口（符号才是硬门）")
            continue
        want = gold["anchors"][name]
        tol = int(gold.get("tol", {}).get(name, 0))
        ok = abs(got - want) <= tol
        want_s = "0x%08x" % want if kind == "hex" else want
        got_s = "0x%08x" % got if kind == "hex" else got
        rep.add(name, want_s, got_s, ok, ("±%d" % tol) if tol else "")

    bits_seen = {}
    for name, off, key, note in spec["bits"]:
        want = gold["bits"][key]
        got_b = w[base + off:base + off + len(want)]
        bits_seen[key] = got_b
        rep.bits(name, got_b, want, note)

    # ---- 平台锚点：阵列运行 / DMA / 忙拍 ----
    rep.eq("阵列运行总数", w[3], sum(s["runs"] for s in stages))
    rep.add("DMA 次数 > 0", "> 0", w[4], w[4] > 0)
    rep.add("阵列忙拍 ≤ 总周期", "≤ %d" % cycles, w[5], 0 < w[5] <= cycles)

    # ---- 按时率 / 负载时间轴（docs/20 §5）----
    # 单次到达：预算只报不判（阶段 0 的等价门不掺负载）。
    # 连续业务：每格的代价 = 硬件实测的整链周期，时间轴与队列在 proto/load.py 里推。
    slot_ms = float(ir.get("budget", {}).get("slot_ms", 0) or 0)
    clock = int(plan["target"].get("clock_hz", 0))
    budget = int(round(slot_ms * clock / 1000.0)) if slot_ms and clock else 0
    arrival = (ir.get("traffic") or {}).get("arrival", "single")
    threshold = ir.get("budget", {}).get("on_time")
    timeline = None
    counts = LOAD.profile(ir.get("traffic"), int((ir.get("traffic") or {}).get("slots", 40)))
    if budget and counts:
        rows, late = LOAD.simulate(cycles, counts, budget)
        timeline = LOAD.dump(pdir, rows, late, cycles, budget, len(rows))
        rate = timeline["on_time_rate"]
        if threshold is None:
            rep.add("按时率（未设门槛，只报）", "—", "%.1f%%" % (100 * rate), True,
                    "每格 %d 作业 × %d 周期 vs 预算 %d 周期"
                    % (counts[0], cycles, budget))
        else:
            rep.add("按时率 ≥ %.0f%%" % (100 * float(threshold)), "%d%%" % int(100 * float(threshold)),
                    "%.1f%%" % (100 * rate), rate >= float(threshold),
                    "40 格时间轴见 timeline.csv")
        rep.eq("overrun 标志", w[7], 0 if rate >= 1.0 else 1)
    elif budget:
        rep.add("每时隙预算（单次到达，只报不判）", "≤ %d" % budget, cycles,
                True, "%.3f ms，%s" % (slot_ms, "超 %.1f×" % (cycles / float(budget))
                                     if cycles > budget else "在预算内"))

    # ---- 等价门：与手写 benchmark 逐值对账 ----
    ref_rows = []
    if a.ref:
        if not os.path.exists(a.ref):
            print("[FAIL] 缺少参照 dump %s（先跑 %s）"
                  % (a.ref, getattr(model, "REF_GATE", "参照门")))
            return 1
        r = read_dump(a.ref)
        for name, idx in spec["ref_scalars"]:
            g = anchors[name]
            ok = (r[idx] == g)
            ref_rows.append((name, r[idx], g, "OK" if ok else "FAIL"))
            rep.add("参照 %s" % name, r[idx], g, ok)
        for name, off, key in spec["ref_bits"]:
            r0 = r[off:off + len(gold["bits"][key])]
            g0 = bits_seen[key]
            bad = sum(1 for x, y in zip(r0, g0) if x != y) + abs(len(r0) - len(g0))
            ref_rows.append((name, "%d bit" % len(r0), "%d/%d 不同" % (bad, len(r0)),
                             "OK" if bad == 0 else "FAIL"))
            rep.add("参照 %s" % name, "逐位一致", "%d/%d 不同" % (bad, len(r0)),
                    bad == 0)
        d = abs(cycles - r[2]) / float(r[2] or 1)
        ok = d <= CYCLES_TOL
        ref_rows.append(("周期", r[2], cycles, "OK（Δ %.2f%%）" % (100 * d) if ok
                         else "FAIL（Δ %.2f%%）" % (100 * d)))
        rep.add("参照周期（±1%）", "±%.0f%%" % (CYCLES_TOL * 100), "Δ %.2f%%"
                % (100 * d), ok)
        rep.eq("参照阵列运行数", r[3], w[3])

    report = os.path.join(pdir, "report.md")
    plan = dict(plan)
    plan["params_hash"] = gold.get("params_hash", "?")
    write_report(report, a.scenario, plan, ir, rep, stages, ref_rows, cycles, budget,
                 w[4], timeline)

    print("阶段占比：")
    for s in stages:
        print("  %-2d %-17s %-10s %6d 周期  %5.1f%%  %4d 次阵列运行（计划 %d）"
              % (s["id"], s["op"], s["where"], s["cycles"],
                 100.0 * s["cycles"] / (cycles or 1), s["runs"], s["planned"]))
    print("总周期 %d（预算 %d）%s；阵列 %d 次 / DMA %d 次 / 忙 %d 拍"
          % (cycles, budget, "，按时" if (not budget or cycles <= budget) else "，超时",
             w[3], w[4], w[5]))
    if a.ref:
        print("与参照基准：周期差 %.2f%%，锚点逐值对账见报告"
              % (100.0 * abs(cycles - read_dump(a.ref)[2]) / (read_dump(a.ref)[2] or 1)))
    print("报告：%s" % os.path.relpath(report, _ROOT))

    if not rep.ok:
        fail = [r for r in rep.rows if r[3] != "OK"]
        for name, want, gotv, _, note in fail:
            print("  [FAIL] %s：期望 %s，实测 %s %s" % (name, want, gotv, note))
        print("[FAIL] 协议基准 %s" % a.scenario)
        return 1
    print("[ OK ] 协议基准 %s：判据全过%s"
          % (a.scenario, "，与手写基准等价" if a.ref else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
