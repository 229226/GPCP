#!/usr/bin/env python3
"""docs/protocols/README.md 第 6 步：判据 + 按时率 + 阶段占比 + 差距表 -> report.md。

    # 单跑协议运行器（模型判据 + 预算 + 阶段表）
    python3 proto/proto_eval.py --scenario nr_pdcch --dump build/soc/proto_nr_pdcch_dump.txt

    # 阶段 0 的等价门：再和人写基准逐值对账（周期 ±1%）
    python3 proto/proto_eval.py --scenario nr_pdcch --dump ... --ref build/soc/pdcch_loop_dump.txt

    python3 proto/proto_eval.py --scenario nr_pdcch --ref-gate   # 打印参照门名（Makefile 用）

判据分三层，缺一层都不算过：
  1. 模型层：golden.json 的锚点与比特（由 tools/models/nr_pdcch_tx.py 独立算出）；
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


def _sx(v):
    """32 位无符号 -> 有符号（dump 里存的是补码）。"""
    return v - 0x100000000 if v & 0x80000000 else v


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


PERF_STAGE_WORD = 1200
PERF_FULL_WORD = 1400
PERF_MAGIC = 0x50455246


def parse_perf(w):
    """解析 proto_runner 写在结果区 1200/1400 的 CGRA 性能计数块。"""
    if len(w) < PERF_STAGE_WORD + 3 or w[PERF_STAGE_WORD] != PERF_MAGIC:
        return None
    nst = w[PERF_STAGE_WORD + 1]
    nk = w[PERF_STAGE_WORD + 2]
    stages = []
    stride = nk + 1
    for i in range(nst):
        b = PERF_STAGE_WORD + 3 + i * stride
        if b + 1 + nk > len(w):
            break
        stages.append({"id": w[b], "values": w[b + 1:b + 1 + nk]})
    full = None
    if len(w) >= PERF_FULL_WORD + 2 and w[PERF_FULL_WORD] == PERF_MAGIC:
        n = w[PERF_FULL_WORD + 1]
        full = w[PERF_FULL_WORD + 2:PERF_FULL_WORD + 2 + n]
    return {"stages": stages, "full": full, "keys": nk}


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
                 timeline=None, perf=None):
    tgt = plan["target"]
    arrival = (ir.get("traffic") or {}).get("arrival", "single")
    enforced = arrival not in ("single", "none", None, "")
    on_time = (budget > 0 and cycles <= budget)
    lines = [
        "# 协议基准报告 — %s（%s/%s）" % (scenario, ir.get("rat"), ir.get("direction")),
        "",
        "> 生成物：`proto/proto_eval.py`（docs/protocols/README.md）。不要手改，重跑 `make proto` 覆盖。",
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
           "" if enforced else "（单次到达：阶段 0 只报不判，负载模型见 docs/protocols/README.md）"),
        "",
    ]
    if timeline:
        lines += [
            "### 负载时间轴（docs/protocols/README.md）",
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
                  "docs/protocols/README.md 把它们列为阶段 4 的自动搜索目标。"]
    else:
        lines.append("（无差异：每个阶段的计划运行数与实测一致，且没有"
                     "「标量落点里跑阵列」的情况。）")

    # 阶段 4：把 lower.py 的 tiling / 缓冲搜索摊开，便于审阅“为什么选它”。
    srch = plan.get("search") or {}
    srows = []
    for item in srch.get("wifi_dft") or []:
        cands = ["%s(est_ops=%s)" % (c["impl"], c.get("est_ops"))
                 for c in item.get("candidates") or []]
        srows.append(("WiFi DFT", item.get("stage"),
                      " / ".join(cands), item.get("chosen"),
                      "min est_ops；buffer=%s 字"
                      % ((item.get("candidates") or [{}])[0].get("buffer_words"))))
    for item in srch.get("fft") or []:
        cands = ["%s(x%.2f, runs/sym=%d)" % (c["impl"], c["over_provision"],
                                            c["runs_per_symbol"])
                 for c in item.get("candidates") or []]
        srows.append(("FFT", item.get("stage"), " / ".join(cands),
                      item.get("chosen"),
                      "%s跑数=%d（%d 符号）"
                      % ("隐藏 " if item.get("hidden") else "",
                         item.get("runs", 0), item.get("nsym", 1))))
    for item in srch.get("ldpc") or []:
        cands = ["%s(spad=%d, runs=%d)" % (c["impl"], c["spad_words"], c["array_runs"])
                 for c in item.get("candidates") or []]
        srows.append(("LDPC", item.get("stage"), " / ".join(cands),
                      item.get("chosen"),
                      "need_zc=%d；min spad" % item.get("need_zc", 0)))
    if srows:
        lines += ["", "## 自动搜索（tiling / 缓冲）", "",
                  "| 类别 | 阶段 | 候选 | 选中 | 依据 |",
                  "|---|---|---|---|---|"]
        for r in srows:
            lines.append("| %s | %s | %s | %s | %s |" % r)

    if perf:
        opmap = {s["id"]: s["op"] for s in stages}
        cycmap = {s["id"]: max(1, s["cycles"]) for s in stages}
        lines += ["", "## 硬件性能计数器（CGRA，阶段增量）", "",
                  "| # | 阶段 | 阵列忙 | 阵列利用率 | 有效指令 | MAC | LD/ST | bank 冲突 | DMA beats | DMA wait |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for r in perf.get("stages") or []:
            v = r["values"]
            cyc = cycmap.get(r["id"], 1)
            issued = max(1, v[1])
            util = 100.0 * v[0] / cyc
            eff = 100.0 * (v[1] - v[2]) / issued
            mac = 100.0 * v[3] / issued
            conf = 100.0 * v[6] / max(1, v[0])
            lines.append("| %d | %s | %d | %.1f%% | %.1f%% | %.1f%% | %d/%d | %.1f%% | %d | %d |"
                         % (r["id"], opmap.get(r["id"], "?"),
                            v[0], util, eff, mac, v[4], v[5], conf, v[7], v[8]))

        # 全周期账本：每拍只归一类，所有周期都有明确归属
        def pctv(a, b):
            return 100.0 * a / b if b else 0.0

        cpu_rows = []
        if perf.get("keys", 0) >= 16:
            lines += ["", "## 全周期账本（互斥状态）", "",
                      "口径：每拍只归一类，优先级 阵列 > DMA > CPU；"
                      "`阵列执行 + DMA 传输 + DMA 等待 + CPU 执行 = 阶段实测周期`。"
                      "DMA 等待是 DMA 状态下总线未给响应的子集。"
                      "CPU 为该阶段墙钟减去阵列与 DMA 后的残差，"
                      "已扣除计数器快照本身引入的约 0.14k 周期归属。", "",
                      "| # | 阶段 | 实测周期 | 阵列执行 | DMA 传输 | DMA 等待 | CPU 执行 |",
                      "|---|---|---|---|---|---|---|"]
            for r in perf["stages"]:
                v = r["values"]
                cyc = cycmap.get(r["id"], 1)
                arr = v[0]
                dma_all = v[9]
                if arr + dma_all > cyc:          # 快照跨阶段边界时夹紧，保证账本恒等
                    dma_all = max(0, cyc - arr)
                dma_wait = min(v[10], dma_all)
                dma_xfer = dma_all - dma_wait
                cpu = max(0, cyc - arr - dma_all)
                imem, acfg, dcfg, cache = v[12], v[13], v[14], v[15]
                sub = imem + acfg + dcfg + cache
                if sub > cpu and sub:
                    sc = float(cpu) / sub
                    imem, acfg, dcfg, cache = (int(imem * sc), int(acfg * sc),
                                               int(dcfg * sc), int(cache * sc))
                    sub = imem + acfg + dcfg + cache
                scalar = max(0, cpu - sub)
                lines.append("| %d | %s | %d | %d (%.1f%%) | %d (%.1f%%) | %d (%.1f%%) | %d (%.1f%%) |"
                             % (r["id"], opmap.get(r["id"], "?"), cyc,
                                arr, pctv(arr, cyc),
                                dma_xfer, pctv(dma_xfer, cyc),
                                dma_wait, pctv(dma_wait, cyc),
                                cpu, pctv(cpu, cyc)))
                cpu_rows.append((r["id"], imem, acfg, dcfg, cache, scalar, cpu, cyc))
            lines += ["", "### CPU 执行细分（只统计阵列与 DMA 都空闲的周期）", "",
                      "| # | 阶段 | IMEM 装载 | 阵列配置写 | DMA 配置写 | 缓存维护 | 标量算法（含主机窗口拷贝/轮询） | 合计 |",
                      "|---|---|---|---|---|---|---|---|"]
            for (sid, imem, acfg, dcfg, cache, scalar, cpu, cyc) in cpu_rows:
                lines.append("| %d | %s | %d (%.1f%%) | %d (%.1f%%) | %d (%.1f%%) | %d (%.1f%%) | %d (%.1f%%) | %d |"
                             % (sid, opmap.get(sid, "?"),
                                imem, pctv(imem, cyc), acfg, pctv(acfg, cyc),
                                dcfg, pctv(dcfg, cyc), cache, pctv(cache, cyc),
                                scalar, pctv(scalar, cyc), cpu))

        full = perf.get("full") or []
        if full:
            def g(i):
                return full[i] if i < len(full) else 0
            cyc = g(0) or 1
            issued = g(3) or 1
            dma_wait = g(16) + g(17) + g(18) + g(19)
            lines += ["", "### 整跑", "",
                      "- 总周期 **%d**；阵列忙 **%d（%.1f%%）**"
                      % (g(0), g(1), 100.0 * g(1) / cyc),
                      "- 互斥状态：阵列 **%d（%.1f%%）**、DMA **%d（%.1f%%）**、CPU **%d（%.1f%%）**"
                      % (g(1), 100.0 * g(1) / cyc, g(54), 100.0 * g(54) / cyc,
                         g(56), 100.0 * g(56) / cyc),
                      "- 指令槽 **%d**：NOP **%.1f%%**、MAC **%.1f%%**、LD/ST **%d/%d**"
                      % (g(3), 100.0 * g(4) / issued, 100.0 * g(5) / issued,
                         g(7), g(8)),
                      "- SPAD：读 **%d** / 写 **%d**；同拍同 bank 冲突 **%d** 周期、"
                      "同 word 冲突 **%d** 周期" % (g(10), g(11), g(12), g(13)),
                      "- DMA：busy **%d** 周期、beats **%d**、等待 **%d** 周期"
                      "（AR %d / AW %d / B %d / data %d）"
                      % (g(14), g(15), dma_wait, g(16), g(17), g(18), g(19)),
                      "- 每 PE 活跃周期（0..15）：%s"
                      % ", ".join(str(g(22 + p)) for p in range(16))]
            if perf.get("stages"):
                def worst(key):
                    return max(perf["stages"],
                               key=lambda r: r["values"][key])
                wb = worst(6)
                wd = worst(8)
                wm = worst(3)
                hints = []
                if max(r["values"][0] for r in perf["stages"]) == 0:
                    hints.append("- 本场景无阵列工作（全部在 CVA6 标量执行）")
                else:
                    hints.append("- 最大阵列利用率阶段：`%s`（%.1f%%）"
                                 % (opmap.get(max(perf["stages"],
                                                  key=lambda r: r["values"][0] / cycmap.get(r["id"], 1))["id"], "?"),
                                    100.0 * max(r["values"][0] / cycmap.get(r["id"], 1)
                                                for r in perf["stages"])))
                    hints.append("- bank 冲突最多阶段：`%s`（%d 周期）"
                                 % (opmap.get(wb["id"], "?"), wb["values"][6]))
                    hints.append("- DMA 等待最多阶段：`%s`（%d 周期）"
                                 % (opmap.get(wd["id"], "?"), wd["values"][8]))
                    hints.append("- MAC 槽：%s"
                                 % (("所有阶段为 0（当前内核未用 MAC 指令）"
                                     if wm["values"][3] == 0 else
                                     "最多 `%s`（%d 槽）"
                                     % (opmap.get(wm["id"], "?"), wm["values"][3]))))
                if cpu_rows:
                    cs = max(cpu_rows, key=lambda x: x[5])
                    cd = max(cpu_rows, key=lambda x: x[6])
                    hints.append("- CPU 标量最多阶段：`%s`（%d 周期，占其阶段 %.1f%%）"
                                 % (opmap.get(cs[0], "?"), cs[5],
                                    pctv(cs[5], cs[7])))
                    hints.append("- CPU 执行最多阶段：`%s`（%d 周期，占其阶段 %.1f%%）"
                                 % (opmap.get(cd[0], "?"), cd[6],
                                    pctv(cd[6], cd[7])))
                lines += ["", "### 架构提示（自动）", ""] + hints

    if ref_rows:
        lines += ["", "## 与手写基准的对账（等价门）", "",
                  "| 项 | 手写 | 协议运行器 | 结论 |", "|---|---|---|---|"]
        for name, refv, gotv, verdict in ref_rows:
            lines.append("| %s | %s | %s | %s |" % (name, refv, gotv, verdict))
        lines += ["", "> 周期判据是 ±%.0f%%（docs/protocols/README.md）；其余锚点必须逐值相同。"
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
    ap.add_argument("--cpu-only", action="store_true",
                    help="CPU 对照路径：不要求 DMA/阵列忙 > 0（docs/performance/README.md）")
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
    bits_len = {}
    for name, off, key, _note, *rest in spec["bits"]:
        n = len(gold["bits"][key]) if key else int(rest[0])
        bits_len[key or name] = (off, n)
        need = max(need, off + n)
    w = read_dump(a.dump)
    if len(w) < base + need:
        print("  dump too short: %d words（至少要 %d）" % (len(w), base + need))
        return 1

    perf = parse_perf(w)
    rep = Report()
    rep.add("性能计数器块", "0x%08x" % PERF_MAGIC,
            "0x%08x" % w[PERF_STAGE_WORD] if len(w) > PERF_STAGE_WORD else "缺失",
            perf is not None)
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
        # CPU 对照路径不跑阵列，硬件运行次数类锚点不适用（docs/performance/README.md）
        if getattr(a, "cpu_only", False) and name in ("mmse_runs",):
            rep.add(name, "不适用", got, True, "CPU 路径不跑阵列")
            continue
        if kind == "window":
            lo, hi = gold["windows"][name]
            rep.window(name, got, lo, hi, "定点增益窗口（符号才是硬门）")
            continue
        if kind == "nonzero":
            rep.add(name, "非零", got, got != 0)
            continue
        if kind == "raw":
            continue                 # 只记录，供与手写基准逐值对账
        want = gold["anchors"][name]
        if kind == "sign":
            got_s = _sx(got)
            ok = got_s != 0 and (got_s > 0) == (want > 0)
            rep.add(name, "正" if want > 0 else "负", got_s, ok, "只判符号")
            continue
        tol = int(gold.get("tol", {}).get(name, 0))
        ok = abs(got - want) <= tol
        want_s = "0x%08x" % want if kind == "hex" else want
        got_s = "0x%08x" % got if kind == "hex" else got
        rep.add(name, want_s, got_s, ok, ("±%d" % tol) if tol else "")

    bits_seen = {}
    for name, off, key, note, *rest in spec["bits"]:
        n = len(gold["bits"][key]) if key else int(rest[0])
        got_b = w[base + off:base + off + n]
        if key:
            bits_seen[key] = got_b
            rep.bits(name, got_b, gold["bits"][key], note)
        else:
            # 模型不给这一组（例如 PE 分部），只与手写基准逐值对账
            bits_seen[name] = got_b

    # ---- 平台锚点：阵列运行 / DMA / 忙拍 ----
    # 纯接收场景的 FFT 描述符不走 DMA API（ssb_fft.inc 自己编排），模型的
    # SPEC["platform"] 可以声明"本场景不统计"，此时只打印数值不判。
    plat = spec.get("platform", {})
    if getattr(a, "cpu_only", False):
        plat = dict(plat, dma=False, busy=False)
    rep.eq("阵列运行总数", w[3], sum(s["runs"] for s in stages))
    if plat.get("dma", True):
        rep.add("DMA 次数 > 0", "> 0", w[4], w[4] > 0)
    else:
        rep.add("DMA 次数", "本场景不统计", w[4], True, "FFT 内部描述符不计入")
    if plat.get("busy", True):
        rep.add("阵列忙拍 ≤ 总周期", "≤ %d" % cycles, w[5], 0 < w[5] <= cycles)
    else:
        rep.add("阵列忙拍", "本场景不统计", w[5], True)

    # ---- 按时率 / 负载时间轴（docs/protocols/README.md）----
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
        for ent in spec["ref_bits"]:
            name, off, key = ent[0], ent[1], ent[2]
            nbits = ent[3] if len(ent) > 3 else None
            n = nbits or bits_len[key][1]
            r0 = r[off:off + n]
            g0 = bits_seen[key or name]
            bad = sum(1 for x, y in zip(r0, g0) if x != y) + abs(len(r0) - len(g0))
            ref_rows.append((name, "%d bit" % len(r0), "%d/%d 不同" % (bad, len(r0)),
                             "OK" if bad == 0 else "FAIL"))
            rep.add("参照 %s" % name, "逐位一致", "%d/%d 不同" % (bad, len(r0)),
                    bad == 0)
        # 有些基准的结果区没有周期/运行数字段（PRACH/PUCCH 只报 runs/DMA），
        # 由模型的 ref_stats 声明对应下标；None 表示不比。
        rs = spec.get("ref_stats", {"cycles": 2, "runs": 3})
        if rs.get("cycles") is not None:
            d = abs(cycles - r[rs["cycles"]]) / float(r[rs["cycles"]] or 1)
            tol = float(getattr(model, "CYCLES_TOL", CYCLES_TOL))
            ok = d <= tol
            ref_rows.append(("周期", r[rs["cycles"]], cycles,
                             "OK（Δ %.2f%%）" % (100 * d) if ok
                             else "FAIL（Δ %.2f%%）" % (100 * d)))
            rep.add("参照周期（±%.0f%%）" % (tol * 100), "±%.0f%%" % (tol * 100),
                    "Δ %.2f%%" % (100 * d), ok)
        else:
            ref_rows.append(("周期", "该基准不统计", cycles, "—"))
        if rs.get("runs") is not None:
            rep.eq("参照阵列运行数", r[rs["runs"]], w[3])

    # CPU 对照路径（docs/performance/README.md）不能覆盖 CGRA 路径的 report.md：perf_report.py
    # 只读 report.md，一旦被覆盖，报告里这个场景就变成"全是 CPU"。
    report = os.path.join(pdir, "report_cpu.md" if getattr(a, "cpu_only", False)
                          else "report.md")
    plan = dict(plan)
    plan["params_hash"] = gold.get("params_hash", "?")
    write_report(report, a.scenario, plan, ir, rep, stages, ref_rows, cycles, budget,
                 w[4], timeline, perf)

    print("阶段占比：")
    for s in stages:
        print("  %-2d %-17s %-10s %6d 周期  %5.1f%%  %4d 次阵列运行（计划 %d）"
              % (s["id"], s["op"], s["where"], s["cycles"],
                 100.0 * s["cycles"] / (cycles or 1), s["runs"], s["planned"]))
    print("总周期 %d（预算 %d）%s；阵列 %d 次 / DMA %d 次 / 忙 %d 拍"
          % (cycles, budget, "，按时" if (not budget or cycles <= budget) else "，超时",
             w[3], w[4], w[5]))
    if a.ref:
        rs = spec.get("ref_stats", {"cycles": 2, "runs": 3})
        if rs.get("cycles") is not None:
            rref = read_dump(a.ref)
            print("与参照基准：周期差 %.2f%%，锚点逐值对账见报告"
                  % (100.0 * abs(cycles - rref[rs["cycles"]])
                     / (rref[rs["cycles"]] or 1)))
        else:
            print("与参照基准：该基准不统计周期，锚点逐值对账见报告")
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
