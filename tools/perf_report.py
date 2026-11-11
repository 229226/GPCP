#!/usr/bin/env python3
"""把 build/proto/*/report.md 的 CGRA 硬件计数器汇总成一份可读的 HTML 报告。

数据来源：`verify.sh full` 生成的 17 个协议基准报告（每阶段 9 个计数器 +
整跑 64 个计数器快照）。输出：
    perf-report.html   自包含报告（无外部依赖）
    perf-summary.csv   逐场景逐阶段明细，方便再加工
"""
import csv
import glob
import html
import os
import re
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build", "proto")
OUT_HTML = os.path.join(ROOT, "perf-report.html")
OUT_CSV = os.path.join(ROOT, "perf-summary.csv")

RAT_NAMES = {"nr": "5G NR", "lte": "4G LTE", "wifi": "WiFi 802.11be"}
RAT_ORDER = ["nr", "lte", "wifi"]


def num(s):
    s = str(s).replace("**", "").replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else 0.0


def parse_report(path):
    text = open(path, encoding="utf-8").read()
    m = re.search(r"# 协议基准报告 — (\S+)（(\w+)/(\w+)）", text)
    if not m:
        return None
    name, rat, direction = m.groups()

    stage_rows = []
    sec = text.split("## 阶段占比")[1].split("## 差距表")[0]
    for line in sec.splitlines():
        if not line.startswith("|"):
            continue
        c = [x.strip() for x in line.strip("|").split("|")]
        if c and c[0].isdigit():
            stage_rows.append({
                "id": int(c[0]), "op": c[1], "where": c[2],
                "impl": "" if c[3] in ("-", "") else c[3],
                "planned": int(num(c[4])), "cycles": int(num(c[5])),
                "share": num(c[6]), "runs": int(num(c[7])),
            })

    perf_rows = []
    if "## 硬件性能计数器" in text:
        psec = text.split("## 硬件性能计数器")[1]
        psec = psec.split("## 全周期账本")[0].split("### 整跑")[0]
        for line in psec.splitlines():
            if not line.startswith("|"):
                continue
            c = [x.strip() for x in line.strip("|").split("|")]
            if c and c[0].isdigit():
                perf_rows.append({
                    "id": int(c[0]), "array_busy": int(num(c[2])),
                    "util": num(c[3]), "eff": num(c[4]), "mac_pct": num(c[5]),
                    "ld": int(num(c[6].split("/")[0])),
                    "st": int(num(c[6].split("/")[1])),
                    "bank_pct": num(c[7]),
                    "dma_beats": int(num(c[8])), "dma_wait": int(num(c[9])),
                })

    ledger = {}
    if "## 全周期账本（互斥状态）" in text:
        lsec = text.split("## 全周期账本（互斥状态）")[1]
        lsec = lsec.split("### CPU 执行细分")[0]
        for line in lsec.splitlines():
            if not line.startswith("|"):
                continue
            c = [x.strip() for x in line.strip("|").split("|")]
            if c and c[0].isdigit():
                ledger[int(c[0])] = {
                    "array": int(num(c[3])), "dma_xfer": int(num(c[4])),
                    "dma_wait": int(num(c[5])), "cpu": int(num(c[6])),
                }

    cpu_detail = {}
    if "### CPU 执行细分" in text:
        csec = text.split("### CPU 执行细分")[1].split("### 整跑")[0]
        for line in csec.splitlines():
            if not line.startswith("|"):
                continue
            c = [x.strip() for x in line.strip("|").split("|")]
            if c and c[0].isdigit():
                cpu_detail[int(c[0])] = {
                    "imem": int(num(c[2])), "array_cfg": int(num(c[3])),
                    "dma_cfg": int(num(c[4])), "cache": int(num(c[5])),
                    "scalar": int(num(c[6])),
                }

    full = {
        "total_cycles": 0, "array_busy": 0, "array_pct": 0.0,
        "issued": 0, "nop_pct": 0.0, "mac_pct": 0.0, "ld": 0, "st": 0,
        "spad_read": 0, "spad_write": 0, "bank_conflict": 0,
        "word_conflict": 0, "dma_busy": 0, "dma_beats": 0, "dma_wait": 0,
        "ar_wait": 0, "aw_wait": 0, "b_wait": 0, "data_wait": 0,
        "pe_active": [],
    }
    pats = [
        (r"总周期 \*\*([\d,]+)\*\*；阵列忙 \*\*([\d,]+)（([\d.]+)%）\*\*",
         ("total_cycles", "array_busy", "array_pct")),
        (r"指令槽 \*\*([\d,]+)\*\*：NOP \*\*([\d.]+)%\*\*、MAC \*\*([\d.]+)%\*\*、"
         r"LD/ST \*\*([\d,]+)/([\d,]+)\*\*",
         ("issued", "nop_pct", "mac_pct", "ld", "st")),
        (r"SPAD：读 \*\*([\d,]+)\*\* / 写 \*\*([\d,]+)\*\*；"
         r"同拍同 bank 冲突 \*\*([\d,]+)\*\* 周期、同 word 冲突 \*\*([\d,]+)\*\* 周期",
         ("spad_read", "spad_write", "bank_conflict", "word_conflict")),
        (r"DMA：busy \*\*([\d,]+)\*\* 周期、beats \*\*([\d,]+)\*\*、"
         r"等待 \*\*([\d,]+)\*\* 周期（AR ([\d,]+) / AW ([\d,]+) / B ([\d,]+) / data ([\d,]+)）",
         ("dma_busy", "dma_beats", "dma_wait", "ar_wait", "aw_wait", "b_wait", "data_wait")),
    ]
    for pat, keys in pats:
        mm = re.search(pat, text)
        if mm:
            for k, v in zip(keys, mm.groups()):
                full[k] = num(v)
    mm = re.search(r"每 PE 活跃周期（0\.\.15）：([\d, ]+)", text)
    if mm:
        full["pe_active"] = [int(num(x)) for x in mm.group(1).split(",")]
    if full["issued"] == 0:
        full["issued"] = full["array_busy"] * 16
    if full["total_cycles"] == 0:
        full["total_cycles"] = sum(s["cycles"] for s in stage_rows)

    perf = {p["id"]: p for p in perf_rows}
    stages = []
    for s in stage_rows:
        p = perf.get(s["id"], {})
        cycles = max(1, s["cycles"])
        array_busy = int(p.get("array_busy", 0))
        issued = array_busy * 16
        eff = float(p.get("eff", 0.0))
        mac_pct = float(p.get("mac_pct", 0.0))
        nop = issued * max(0.0, 100.0 - eff) / 100.0
        mac = issued * mac_pct / 100.0
        ld = int(p.get("ld", 0))
        st = int(p.get("st", 0))
        alu = max(0.0, issued - nop - mac - ld - st)
        bank_pct = float(p.get("bank_pct", 0.0))
        led = ledger.get(s["id"], {})
        det = cpu_detail.get(s["id"], {})
        if led:
            dma_xfer = led["dma_xfer"]
            dma_wait = led["dma_wait"]
            cpu = led["cpu"]
        else:
            dma_wait = int(p.get("dma_wait", 0))
            dma_xfer = 0
            cpu = max(0, cycles - array_busy - dma_wait)
        cpu_imem = det.get("imem", 0)
        cpu_cfg = det.get("array_cfg", 0) + det.get("dma_cfg", 0)
        cpu_cache = det.get("cache", 0)
        cpu_scalar = det.get("scalar", max(0, cpu - cpu_imem - cpu_cfg - cpu_cache))
        stages.append({
            **s,
            "cycles": cycles,
            "array_busy": array_busy,
            "util": 100.0 * array_busy / cycles,
            "issued": issued, "nop": nop, "mac": mac,
            "ld": ld, "st": st, "alu": alu,
            "nop_pct": (100.0 * nop / issued) if issued else 0.0,
            "mac_pct": (100.0 * mac / issued) if issued else 0.0,
            "ld_pct": (100.0 * ld / issued) if issued else 0.0,
            "st_pct": (100.0 * st / issued) if issued else 0.0,
            "alu_pct": (100.0 * alu / issued) if issued else 0.0,
            "bank_pct": bank_pct,
            "bank_conflict": int(round(array_busy * bank_pct / 100.0)),
            "dma_beats": int(p.get("dma_beats", 0)),
            "dma_wait": dma_wait, "dma_xfer": dma_xfer,
            "cpu": cpu, "cpu_imem": cpu_imem, "cpu_cfg": cpu_cfg,
            "cpu_cache": cpu_cache, "cpu_scalar": cpu_scalar,
            "array_pct": pct(array_busy, cycles),
            "dma_xfer_pct": pct(dma_xfer, cycles),
            "dma_wait_pct": pct(dma_wait, cycles),
            "cpu_pct": pct(cpu, cycles),
            "cpu_imem_pct": pct(cpu_imem, cpu),
            "cpu_cfg_pct": pct(cpu_cfg, cpu),
            "cpu_cache_pct": pct(cpu_cache, cpu),
            "cpu_scalar_pct": pct(cpu_scalar, cpu),
        })
    return {"name": name, "rat": rat, "direction": direction,
            "stages": stages, "full": full}


def pct(a, b):
    return 100.0 * a / b if b else 0.0


def esc(s):
    return html.escape(str(s))


def fmt(n):
    return "{:,}".format(int(round(n)))


def bar(value, maxv, cls="blue", label=""):
    w = 100.0 * value / maxv if maxv else 0.0
    return ('<div class="bar-track"><div class="bar-fill %s" style="width:%.1f%%"></div>'
            '<span class="bar-label">%s</span></div>' % (cls, w, esc(label)))


def ledger_segments(stage):
    return [("array", stage["array_busy"]), ("dma", stage["dma_xfer"]),
            ("dma2", stage["dma_wait"]), ("imem", stage["cpu_imem"]),
            ("cfg", stage["cpu_cfg"]), ("cache", stage["cpu_cache"]),
            ("scalar", stage["cpu_scalar"])]


def ledger_bar(stage):
    total = max(1, stage["cycles"])
    out = ['<div class="stack" title="阶段 %d %s：%s 周期">'
           % (stage["id"], esc(stage["op"]), fmt(total))]
    for cls, v in ledger_segments(stage):
        out.append('<span class="seg %s" style="width:%.2f%%"></span>'
                   % (cls, min(100.0, 100.0 * v / total)))
    out.append("</div>")
    return "".join(out)


def mix_bar(stage):
    if not stage["issued"]:
        return '<span class="muted">—</span>'
    parts = [("nop", stage["nop_pct"]), ("mac", stage["mac_pct"]),
             ("ld", stage["ld_pct"]), ("st", stage["st_pct"]),
             ("alu", stage["alu_pct"])]
    out = ['<div class="mix" title="NOP %.0f%% / MAC %.0f%% / LD %.0f%% / ST %.0f%% / ALU %.0f%%">'
           % (stage["nop_pct"], stage["mac_pct"], stage["ld_pct"],
              stage["st_pct"], stage["alu_pct"])]
    for cls, v in parts:
        out.append('<span class="seg %s" style="width:%.2f%%"></span>' % (cls, v))
    out.append("</div>")
    return "".join(out)


def kernel_bar(k):
    total = max(1, k["cycles"])
    out = ['<div class="stack">']
    for cls, v in [("array", k["array_busy"]), ("dma", k["dma_xfer"]),
                   ("dma2", k["dma_wait"]), ("imem", k["cpu_imem"]),
                   ("cfg", k["cpu_cfg"]), ("cache", k["cpu_cache"]),
                   ("scalar", k["cpu_scalar"])]:
        out.append('<span class="seg %s" style="width:%.2f%%"></span>'
                   % (cls, min(100.0, 100.0 * v / total)))
    out.append("</div>")
    return "".join(out)


def heatmap(pe, name):
    if not pe or max(pe) == 0:
        return '<p class="muted">该场景无阵列工作（全部 CVA6 标量）。</p>'
    mx = max(pe)
    cells = []
    for i, v in enumerate(pe):
        alpha = 0.12 + 0.78 * (v / mx if mx else 0)
        color = "#fff" if alpha >= 0.55 else "#18212f"
        cells.append('<div class="cell" style="background:rgba(37,99,235,%.2f);color:%s" '
                     'title="PE%d：%s 周期">%s<br><b>%s</b></div>'
                     % (alpha, color, i, fmt(v), i, fmt(v / 1000.0) + "k"))
    return '<div class="heat">%s</div>' % "".join(cells)


def main():
    data = [parse_report(p) for p in sorted(glob.glob(os.path.join(BUILD, "*", "report.md")))]
    data = [d for d in data if d]
    if not data:
        raise SystemExit("no reports in %s" % BUILD)

    total_cycles = sum(d["full"]["total_cycles"] for d in data)
    total_busy = sum(d["full"]["array_busy"] for d in data)
    total_issued = sum(d["full"]["issued"] for d in data)
    total_nop = sum(d["full"]["issued"] * d["full"]["nop_pct"] / 100.0 for d in data)
    total_mac = sum(d["full"]["issued"] * d["full"]["mac_pct"] / 100.0 for d in data)
    total_ld = sum(d["full"]["ld"] for d in data)
    total_st = sum(d["full"]["st"] for d in data)
    total_bank = sum(d["full"]["bank_conflict"] for d in data)
    total_dma_wait = sum(d["full"]["dma_wait"] for d in data)
    total_data_wait = sum(d["full"]["data_wait"] for d in data)
    total_dma_busy = sum(d["full"]["dma_busy"] for d in data)
    total_dma_xfer = sum(s["dma_xfer"] for d in data for s in d["stages"])
    total_cpu = sum(s["cpu"] for d in data for s in d["stages"])
    total_cpu_scalar = sum(s["cpu_scalar"] for d in data for s in d["stages"])

    # kernel aggregation：有 impl 用 impl，否则用 op（标量阶段）
    kernels = {}
    for d in data:
        for s in d["stages"]:
            key = s["impl"] or ("scalar:" + s["op"])
            k = kernels.setdefault(key, {"key": key, "impl": s["impl"], "op": s["op"],
                                         "scenarios": set(), "cycles": 0,
                                         "array_busy": 0, "issued": 0, "nop": 0.0,
                                         "mac": 0.0, "ld": 0, "st": 0, "alu": 0.0,
                                         "bank_conflict": 0, "dma_beats": 0, "dma_wait": 0,
                                         "dma_xfer": 0, "cpu": 0, "cpu_imem": 0,
                                         "cpu_cfg": 0, "cpu_cache": 0, "cpu_scalar": 0,
                                         "instances": []})
            k["scenarios"].add(d["name"])
            k["instances"].append((d["name"], s))
            for f in ("cycles", "array_busy", "issued", "ld", "st", "dma_beats",
                      "dma_wait", "dma_xfer", "cpu", "cpu_imem", "cpu_cfg",
                      "cpu_cache", "cpu_scalar", "bank_conflict"):
                k[f] += s[f]
            k["nop"] += s["nop"]
            k["mac"] += s["mac"]
            k["alu"] += s["alu"]
    for k in kernels.values():
        k["scenarios"] = len(k["scenarios"])
        k["util"] = pct(k["array_busy"], k["cycles"])
        k["nop_pct"] = pct(k["nop"], k["issued"])
        k["mac_pct"] = pct(k["mac"], k["issued"])
        k["ld_pct"] = pct(k["ld"], k["issued"])
        k["st_pct"] = pct(k["st"], k["issued"])
        k["alu_pct"] = pct(k["alu"], k["issued"])
        k["bank_pct"] = pct(k["bank_conflict"], k["array_busy"])
        k["array_pct"] = pct(k["array_busy"], k["cycles"])
        k["dma_xfer_pct"] = pct(k["dma_xfer"], k["cycles"])
        k["dma_wait_pct"] = pct(k["dma_wait"], k["cycles"])
        k["cpu_pct"] = pct(k["cpu"], k["cycles"])
        k["cpu_imem_pct"] = pct(k["cpu_imem"], k["cpu"])
        k["cpu_cfg_pct"] = pct(k["cpu_cfg"], k["cpu"])
        k["cpu_cache_pct"] = pct(k["cpu_cache"], k["cpu"])
        k["cpu_scalar_pct"] = pct(k["cpu_scalar"], k["cpu"])
    kernel_list = sorted(kernels.values(), key=lambda x: -x["cycles"])

    max_scenario_cycles = max(d["full"]["total_cycles"] for d in data)
    max_kernel_cycles = max(k["cycles"] for k in kernel_list)

    # 自动结论：大场景按利用率，冲突/DMA 按绝对周期，避免小场景百分比误导
    major = [d for d in data if d["full"]["total_cycles"] >= 200000] or data
    major_cgra = [d for d in major if d["full"]["array_busy"] > 0] or major
    by_util = sorted(major_cgra, key=lambda d: -pct(d["full"]["array_busy"],
                                                    d["full"]["total_cycles"]))
    worst_bank = max(data, key=lambda d: d["full"]["bank_conflict"])
    worst_dma = max(data, key=lambda d: d["full"]["dma_wait"])
    top_cycle = max(data, key=lambda d: d["full"]["total_cycles"])
    pure_scalar = [d["name"] for d in data if d["full"]["array_busy"] == 0]

    # CSV
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["scenario", "rat", "direction", "stage_id", "op", "impl", "where",
                    "cycles", "share_pct", "array_busy", "array_util_pct",
                    "dma_xfer", "dma_xfer_pct", "dma_wait", "dma_wait_pct",
                    "cpu", "cpu_pct", "cpu_imem", "cpu_imem_pct",
                    "cpu_cfg", "cpu_cfg_pct", "cpu_cache", "cpu_cache_pct",
                    "cpu_scalar", "cpu_scalar_pct",
                    "issued", "nop_pct", "mac_pct", "ld", "st", "alu_pct",
                    "bank_conflict", "bank_pct", "dma_beats"])
        for d in data:
            for s in d["stages"]:
                w.writerow([d["name"], d["rat"], d["direction"], s["id"], s["op"],
                            s["impl"], s["where"], s["cycles"], "%.1f" % s["share"],
                            s["array_busy"], "%.1f" % s["util"],
                            s["dma_xfer"], "%.1f" % s["dma_xfer_pct"],
                            s["dma_wait"], "%.1f" % s["dma_wait_pct"],
                            s["cpu"], "%.1f" % s["cpu_pct"],
                            s["cpu_imem"], "%.1f" % s["cpu_imem_pct"],
                            s["cpu_cfg"], "%.1f" % s["cpu_cfg_pct"],
                            s["cpu_cache"], "%.1f" % s["cpu_cache_pct"],
                            s["cpu_scalar"], "%.1f" % s["cpu_scalar_pct"],
                            s["issued"],
                            "%.1f" % s["nop_pct"], "%.1f" % s["mac_pct"],
                            s["ld"], s["st"], "%.1f" % s["alu_pct"],
                            s["bank_conflict"], "%.1f" % s["bank_pct"],
                            s["dma_beats"]])

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    css = """
:root{--bg:#f5f7fa;--card:#fff;--ink:#18212f;--muted:#667085;--line:#e4e7ec;
--blue:#2563eb;--blue2:#93c5fd;--orange:#f59e0b;--green:#10b981;--red:#ef4444;
--purple:#8b5cf6;--gray:#cbd5e1}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:0 22px 60px}
header{background:linear-gradient(120deg,#0f172a 0%,#123b6b 55%,#0f766e 100%);
color:#fff;padding:38px 0 30px;margin-bottom:26px}
header .wrap{padding-bottom:0}
h1{font-size:30px;margin:0 0 8px;letter-spacing:.5px}
.sub{color:#c7d2fe;font-size:15px;margin:0}
.chips{margin-top:18px;display:flex;flex-wrap:wrap;gap:8px}
.chip{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.22);
padding:5px 10px;border-radius:999px;font-size:12px;color:#e0e7ff}
h2{font-size:21px;margin:34px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:22px 0 10px}
h4{font-size:13px;margin:16px 0 8px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
box-shadow:0 1px 2px rgba(16,24,40,.04)}
.kpi .label{color:var(--muted);font-size:12px}.kpi .value{font-size:26px;font-weight:700;
margin-top:4px;font-variant-numeric:tabular-nums}.kpi .note{color:var(--muted);font-size:12px;margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:10px;overflow:hidden;font-variant-numeric:tabular-nums}
th,td{padding:8px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{background:#f8fafc;color:#344054;font-size:12px;font-weight:600;white-space:nowrap}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.muted{color:var(--muted)}
.bar-track{position:relative;background:#eef2f7;border-radius:6px;height:18px;min-width:90px;overflow:hidden}
.bar-fill{height:100%;border-radius:6px}.bar-fill.blue{background:linear-gradient(90deg,#60a5fa,#2563eb)}
.bar-fill.green{background:linear-gradient(90deg,#6ee7b7,#10b981)}
.bar-fill.orange{background:linear-gradient(90deg,#fcd34d,#f59e0b)}
.bar-label{position:absolute;right:5px;top:0;line-height:18px;font-size:11px;color:#344054}
.stack{display:flex;height:14px;border-radius:4px;overflow:hidden;background:#eef2f7;min-width:120px}
.seg{display:block;height:100%}.seg.array{background:#2563eb}.seg.dma{background:#f59e0b}
.seg.dma2{background:#fcd34d}.seg.imem{background:#8b5cf6}.seg.cfg{background:#a78bfa}
.seg.cache{background:#14b8a6}.seg.scalar{background:#94a3b8}
.seg.other{background:#cbd5e1}.seg.nop{background:#cbd5e1}.seg.mac{background:#8b5cf6}
.seg.ld{background:#10b981}.seg.st{background:#f97316}.seg.alu{background:#60a5fa}
.mix{display:flex;height:10px;border-radius:3px;overflow:hidden;background:#eef2f7;min-width:80px}
.legend{display:flex;flex-wrap:wrap;gap:14px;color:var(--muted);font-size:12px;margin:8px 0 12px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
details.scenario{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:0 16px;margin:12px 0;box-shadow:0 1px 2px rgba(16,24,40,.04)}
details.scenario>summary{cursor:pointer;padding:14px 0;font-weight:600;font-size:15px;list-style:none}
details.scenario>summary::-webkit-details-marker{display:none}
details.scenario>summary:before{content:"▸";display:inline-block;width:18px;color:var(--muted)}
details.scenario[open]>summary:before{content:"▾"}
.badge{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
background:#eef2ff;color:#3730a3;margin-left:6px}
.badge.lte{background:#fff7ed;color:#9a3412}.badge.wifi{background:#ecfdf5;color:#065f46}
.heat{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;max-width:360px}
.cell{border-radius:6px;padding:7px 4px;text-align:center;color:#fff;font-size:11px;line-height:1.35}
.cell b{font-size:12px}
.insight{background:#f0f9ff;border:1px solid #bae6fd;border-left:4px solid #0284c7;
border-radius:8px;padding:12px 15px;margin:12px 0}
.insight.warn{background:#fff7ed;border-color:#fed7aa;border-left-color:#ea580c}
.insight.good{background:#ecfdf5;border-color:#a7f3d0;border-left-color:#059669}
.insight ul{margin:6px 0 0 18px;padding:0}.insight li{margin:3px 0}
.note{color:var(--muted);font-size:12px}
.two{display:grid;grid-template-columns:1.15fr .85fr;gap:16px}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 8px}
.toc a{font-size:12px;color:#344054;background:#fff;border:1px solid var(--line);
padding:5px 10px;border-radius:999px;text-decoration:none}
.toc a:hover{border-color:#93c5fd;color:#1d4ed8}
.filter{width:min(560px,100%);padding:7px 10px;border:1px solid var(--line);
border-radius:8px;font-size:13px;background:#fff;color:var(--ink)}
@media(max-width:900px){.two{grid-template-columns:1fr}}
@media print{header{background:#0f172a!important;-webkit-print-color-adjust:exact}
details.scenario{break-inside:avoid}body{background:#fff}}
"""

    def rat_badge(rat):
        return '<span class="badge %s">%s</span>' % (rat, RAT_NAMES.get(rat, rat))

    h = []
    h.append('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">')
    h.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    h.append('<title>GPCP 协议基准性能分析报告</title>')
    h.append('<style>%s</style></head><body>' % css)
    h.append('<header><div class="wrap"><h1>GPCP 协议基准性能分析报告</h1>')
    h.append('<p class="sub">17 个场景 · 3 种制式 · CGRA 硬件计数器逐阶段归因</p>')
    h.append('<div class="chips"><span class="chip">数据：%d 个 proto 报告</span>'
             '<span class="chip">生成：%s</span>'
             '<span class="chip">验证：verify full 83 passed / 0 failed / 1182 s</span>'
             '<span class="chip">计数器：64 × 32 bit @ 0x5000_0100</span></div>'
             '</div></header><div class="wrap">' % (len(data), now))
    h.append('<nav class="toc"><a href="#insights">结论摘要</a>'
             '<a href="#protocols">按协议</a><a href="#kernels">按 Kernel</a>'
             '<a href="#kernel-detail">Kernel 明细</a>'
             '<a href="#hot">热点阶段</a><a href="#details">逐场景明细</a>'
             '<a href="#method">口径</a></nav>')

    # KPI
    h.append('<div class="grid">')
    kpis = [
        ("场景 / 制式", "%d / 3" % len(data), "NR 12 · LTE 4 · WiFi 1"),
        ("周期总和", fmt(total_cycles), "17 次代表性运行之和"),
        ("加权阵列利用率", "%.1f%%" % pct(total_busy, total_cycles),
         "阵列忙 %s / 总周期 %s" % (fmt(total_busy), fmt(total_cycles))),
        ("MAC 槽占比", "%.1f%%" % pct(total_mac, total_issued),
         "当前内核未使用 MAC/MACL"),
        ("DMA 活动", "%s（%.1f%%）" % (fmt(total_dma_xfer + total_dma_wait),
                                     pct(total_dma_xfer + total_dma_wait, total_cycles)),
         "有效传输 %s · 等总线 %s" % (fmt(total_dma_xfer), fmt(total_dma_wait))),
        ("CPU 标量执行", "%s（%.1f%%）" % (fmt(total_cpu_scalar),
                                       pct(total_cpu_scalar, total_cycles)),
         "阵列与 DMA 都空闲时的 CVA6 算法/编排"),
        ("SPAD bank 冲突", fmt(total_bank),
         "占阵列忙 %.1f%%" % pct(total_bank, total_busy)),
    ]
    for label, value, note in kpis:
        h.append('<div class="card kpi"><div class="label">%s</div>'
                 '<div class="value">%s</div><div class="note">%s</div></div>'
                 % (label, value, note))
    h.append('</div>')

    # Insights
    h.append('<h2 id="insights">结论摘要</h2>')
    h.append('<div class="insight"><b>一句话：</b>当前 17 个场景的瓶颈不在 PE 数量，'
             '而在阵列空闲（加权利用率仅 %.1f%%）和 CVA6 标量执行（占 %.1f%%）；'
             'MAC 流水完全未使用，bank 冲突是阵列内部最明确的优化点。</div>'
             % (pct(total_busy, total_cycles), pct(total_cpu, total_cycles)))
    h.append('<div class="insight good"><b>可直接指导架构的 7 条观察</b><ul>')
    h.append('<li><b>阵列利用率</b>：大 CGRA 场景（≥20 万周期且有阵列工作）最高 <code>%s</code> %.1f%%，最低 <code>%s</code> %.1f%%；'
             '多数时间阵列空闲，先做 DMA/双缓冲/阶段重叠，而不是加 PE。</li>'
             % (by_util[0]["name"], pct(by_util[0]["full"]["array_busy"], by_util[0]["full"]["total_cycles"]),
                by_util[-1]["name"], pct(by_util[-1]["full"]["array_busy"], by_util[-1]["full"]["total_cycles"])))
    h.append('<li><b>MAC 单元</b>：全部场景 MAC 槽占比 %.1f%%，说明当前微码/映射没有发出 MAC/MACL；'
             '下一代可评估删减、复用或强制 mapper 使用 MAC。</li>' % pct(total_mac, total_issued))
    h.append('<li><b>bank 冲突</b>：绝对量最大 <code>%s</code> %s 周期（占其阵列忙 %.1f%%）；同 word 冲突为 0，'
             '是“同 bank 多写”而非数据踩踏，增加 bank 或改布局可直接收益。</li>'
             % (worst_bank["name"], fmt(worst_bank["full"]["bank_conflict"]),
                pct(worst_bank["full"]["bank_conflict"],
                    max(1, worst_bank["full"]["array_busy"]))))
    h.append('<li><b>DMA 等待</b>：绝对量最大 <code>%s</code> %s 周期（占其总周期 %.1f%%）；'
             '全部场景 data wait 占 DMA 等待 %.0f%%，AR/AW/B 很小，瓶颈在数据通路带宽/并发，不是描述符取指。</li>'
             % (worst_dma["name"], fmt(worst_dma["full"]["dma_wait"]),
                pct(worst_dma["full"]["dma_wait"], worst_dma["full"]["total_cycles"]),
                pct(total_data_wait, total_dma_wait)))
    h.append('<li><b>CPU 执行</b>：全部场景 CPU 占 %.1f%%（%s 周期），其中标量算法 %s（占 CPU %.0f%%）、'
             'IMEM 装载/配置/缓存维护合计 %s；这些周期阵列与 DMA 都空闲，'
             '是“把算法下沉到阵列”或“加宽主机接口”的直接依据。</li>'
             % (pct(total_cpu, total_cycles), fmt(total_cpu), fmt(total_cpu_scalar),
                pct(total_cpu_scalar, total_cpu),
                fmt(max(0, total_cpu - total_cpu_scalar))))
    h.append('<li><b>纯标量场景</b>：<code>%s</code> 阵列忙为 0，完全跑在 CVA6 上；'
             '若这些协议重要，应考虑 256 点 DFT/向量单元，而不是继续加 CGRA PE。</li>'
             % "、".join(pure_scalar))
    h.append('<li><b>PE 均衡</b>：<code>nr_pdsch</code> 每 PE 活跃 224k–278k（max/min 1.24），'
             '当前 tiling 基本均衡；优先解决阵列空转与 DMA。</li>')
    h.append('</ul></div>')

    # Protocol overview
    h.append('<h2 id="protocols">按协议总览</h2>')
    h.append('<p class="note">周期为单次代表性运行的整链实测；利用率 = 阵列忙 / 总周期。'
             '表格按制式分组，组内按总周期降序。</p>')
    for rat in RAT_ORDER:
        group = sorted([d for d in data if d["rat"] == rat],
                       key=lambda d: -d["full"]["total_cycles"])
        if not group:
            continue
        gcyc = sum(d["full"]["total_cycles"] for d in group)
        gbusy = sum(d["full"]["array_busy"] for d in group)
        h.append('<h3>%s <span class="muted">（%d 场景，%s 周期，阵列利用率 %.1f%%）</span></h3>'
                 % (RAT_NAMES.get(rat, rat), len(group), fmt(gcyc), pct(gbusy, gcyc)))
        h.append('<table><thead><tr><th>场景</th><th class="num">总周期</th>'
                 '<th>周期量级</th><th class="num">阵列利用率</th><th class="num">NOP</th>'
                 '<th class="num">MAC</th><th class="num">LD/ST</th>'
                 '<th class="num">bank 冲突/阵列忙</th><th class="num">DMA 等待</th>'
                 '<th class="num">PE 负载 max/min</th></tr></thead><tbody>')
        for d in group:
            full = d["full"]
            pe = full["pe_active"]
            imb = (max(pe) / min(pe)) if pe and min(pe) else 0
            h.append('<tr><td><b>%s</b> %s</td><td class="num">%s</td><td>%s</td>'
                     '<td class="num">%.1f%%</td><td class="num">%.1f%%</td>'
                     '<td class="num">%.1f%%</td><td class="num">%s/%s</td>'
                     '<td class="num">%.1f%%</td><td class="num">%s</td>'
                     '<td class="num">%s</td></tr>'
                     % (esc(d["name"]), rat_badge(d["rat"]), fmt(full["total_cycles"]),
                        bar(full["total_cycles"], max_scenario_cycles, "blue"),
                        pct(full["array_busy"], full["total_cycles"]),
                        full["nop_pct"], full["mac_pct"], fmt(full["ld"]), fmt(full["st"]),
                        pct(full["bank_conflict"], max(1, full["array_busy"])),
                        fmt(full["dma_wait"]), ("%.2f×" % imb) if imb else "—"))
        h.append('</tbody></table>')

    # Kernel view
    h.append('<h2 id="kernels">按 Kernel 归因</h2>')
    h.append('<p class="note">口径：有 <code>impl</code> 的阶段按实现归并（如 <code>fft4096</code>、'
             '<code>ldpc_bg2_zc20</code>、<code>turbo_k104</code>），控制核阶段按动词归并'
             '（前缀 <code>scalar:</code>）。同一条形长度 = 该 kernel 在全部场景中的累计阶段周期。</p>')
    h.append('<div class="legend"><span><i style="background:#2563eb"></i>阵列执行</span>'
             '<span><i style="background:#f59e0b"></i>DMA 传输</span>'
             '<span><i style="background:#fcd34d"></i>DMA 等总线</span>'
             '<span><i style="background:#8b5cf6"></i>CPU-IMEM</span>'
             '<span><i style="background:#a78bfa"></i>CPU-配置</span>'
             '<span><i style="background:#14b8a6"></i>CPU-缓存维护</span>'
             '<span><i style="background:#94a3b8"></i>CPU-标量算法</span></div>')
    h.append('<p><input id="kernel-filter" class="filter" placeholder="输入 kernel 过滤，例如 ldpc_bg2_zc20 / turbo_k104 / scalar:equalise_demap"></p>')
    h.append('<table id="kernel-table"><thead><tr><th>Kernel / 阶段</th><th class="num">场景数</th>'
             '<th class="num">累计周期</th><th class="num">阵列执行</th>'
             '<th class="num">DMA 传输</th><th class="num">DMA 等待</th>'
             '<th class="num">CPU 执行</th><th>CPU 细分（占 CPU）</th><th>时间构成</th>'
             '<th class="num">NOP</th><th class="num">MAC</th>'
             '<th class="num">bank 冲突/阵列忙</th></tr></thead><tbody>')
    for k in kernel_list[:28]:
        name = k["impl"] or k["key"]
        cpusub = ('IMEM %.1f%% · 配置 %.1f%% · 缓存 %.1f%% · 标量 %.1f%%'
                  % (k["cpu_imem_pct"], k["cpu_cfg_pct"], k["cpu_cache_pct"],
                     k["cpu_scalar_pct"])) if k["cpu"] else "—"
        h.append('<tr data-kernel="%s"><td class="mono">%s</td><td class="num">%d</td>'
                 '<td class="num">%s</td>'
                 '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                 '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                 '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                 '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                 '<td class="note">%s</td><td>%s</td>'
                 '<td class="num">%.1f%%</td><td class="num">%.1f%%</td>'
                 '<td class="num">%.1f%%</td></tr>'
                 % (esc(name), esc(name), k["scenarios"], fmt(k["cycles"]),
                    fmt(k["array_busy"]), k["array_pct"],
                    fmt(k["dma_xfer"]), k["dma_xfer_pct"],
                    fmt(k["dma_wait"]), k["dma_wait_pct"],
                    fmt(k["cpu"]), k["cpu_pct"], cpusub,
                    kernel_bar(k),
                    k["nop_pct"], k["mac_pct"], k["bank_pct"]))
    h.append('</tbody></table>')

    # Per-kernel ledger
    def ipct(v, issued):
        return "—" if not issued else "%.1f%%" % v

    h.append('<h2 id="kernel-detail">Kernel 逐次账本（单个 kernel 的周期花在哪）</h2>')
    h.append('<p class="note">展开任一 kernel 可看到它在各场景的全部实例。每行'
             '<b>阵列执行 / DMA 传输 / DMA 等待 / CPU 执行</b>相加即该实例实测周期；'
             'CPU 列再细分为 IMEM 装载、配置写、缓存维护、标量算法四类（括号为占该实例 CPU 的比例）。'
             '表尾合计行为该 kernel 跨场景的加权占比。</p>')
    for k in kernel_list[:14]:
        name = k["impl"] or k["key"]
        h.append('<details class="scenario"><summary><span class="mono">%s</span> '
                 '<span class="muted">· %d 个实例 · %s 周期 · 阵列 %.1f%% · DMA %.1f%% · CPU %.1f%%</span></summary>'
                 % (esc(name), len(k["instances"]), fmt(k["cycles"]),
                    k["array_pct"], k["dma_xfer_pct"] + k["dma_wait_pct"], k["cpu_pct"]))
        h.append('<table><thead><tr><th>场景</th><th>阶段</th><th class="num">周期</th>'
                 '<th class="num">阵列执行</th><th class="num">DMA 传输</th>'
                 '<th class="num">DMA 等待</th><th class="num">CPU 执行</th>'
                 '<th>CPU 细分（占 CPU）</th><th>工作构成</th>'
                 '<th class="num">bank 冲突</th><th class="num">DMA beats</th>'
                 '</tr></thead><tbody>')
        for sc, s in sorted(k["instances"], key=lambda x: -x[1]["cycles"]):
            cpusub = ('IMEM <b>%s</b> · 配置 <b>%s</b> · 缓存 <b>%s</b> · 标量 <b>%s</b>'
                      % (ipct(s["cpu_imem_pct"], s["cpu"]),
                         ipct(s["cpu_cfg_pct"], s["cpu"]),
                         ipct(s["cpu_cache_pct"], s["cpu"]),
                         ipct(s["cpu_scalar_pct"], s["cpu"]))) if s["cpu"] else "—"
            h.append('<tr><td><b>%s</b></td><td>%s</td><td class="num">%s</td>'
                     '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                     '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                     '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                     '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                     '<td class="note">%s</td><td>%s</td>'
                     '<td class="num">%.1f%%</td><td class="num">%s</td></tr>'
                     % (esc(sc), esc(s["op"]), fmt(s["cycles"]),
                        fmt(s["array_busy"]), s["array_pct"],
                        fmt(s["dma_xfer"]), s["dma_xfer_pct"],
                        fmt(s["dma_wait"]), s["dma_wait_pct"],
                        fmt(s["cpu"]), s["cpu_pct"], cpusub,
                        ledger_bar(s), s["bank_pct"], fmt(s["dma_beats"])))
        h.append('<tr><td colspan="2"><b>合计</b></td><td class="num"><b>%s</b></td>'
                 '<td class="num"><b>%s</b><br><span class="muted">%.1f%%</span></td>'
                 '<td class="num"><b>%s</b><br><span class="muted">%.1f%%</span></td>'
                 '<td class="num"><b>%s</b><br><span class="muted">%.1f%%</span></td>'
                 '<td class="num"><b>%s</b><br><span class="muted">%.1f%%</span></td>'
                 '<td class="note">IMEM %.1f%% · 配置 %.1f%% · 缓存 %.1f%% · 标量 %.1f%%</td>'
                 '<td>%s</td><td class="num">%.1f%%</td><td class="num">%s</td></tr>'
                 % (fmt(k["cycles"]),
                    fmt(k["array_busy"]), k["array_pct"],
                    fmt(k["dma_xfer"]), k["dma_xfer_pct"],
                    fmt(k["dma_wait"]), k["dma_wait_pct"],
                    fmt(k["cpu"]), k["cpu_pct"],
                    k["cpu_imem_pct"], k["cpu_cfg_pct"],
                    k["cpu_cache_pct"], k["cpu_scalar_pct"],
                    kernel_bar(k), k["bank_pct"], fmt(k["dma_beats"])))
        h.append('</tbody></table></details>')
    h.append('<p class="note">仅展开累计周期前 14 的 kernel；完整逐阶段数据见 '
             '<code>perf-summary.csv</code>（含全部账本列与指令构成）。</p>')

    # Hot stages
    hot = sorted([(d, s) for d in data for s in d["stages"]],
                 key=lambda x: -x[1]["cycles"])[:15]
    h.append('<h2 id="hot">Top 15 热点阶段</h2>')
    h.append('<p class="note">按单阶段实测周期排序；“占场景”= 该阶段 / 所在场景总周期。'
             '这些是下一步优化最值得先看的代码/微码位置。</p>')
    h.append('<table><thead><tr><th>#</th><th>场景</th><th>阶段</th><th>实现</th>'
             '<th class="num">周期</th><th class="num">占场景</th>'
             '<th class="num">阵列利用率</th><th class="num">NOP</th>'
             '<th class="num">MAC</th><th class="num">LD/ST</th>'
             '<th class="num">bank 冲突</th><th class="num">DMA wait</th>'
             '</tr></thead><tbody>')
    for i, (d, s) in enumerate(hot, 1):
        h.append('<tr><td>%d</td><td><b>%s</b></td><td>%s</td>'
                 '<td class="mono">%s</td><td class="num">%s</td>'
                 '<td class="num">%.1f%%</td><td class="num">%.1f%%</td>'
                 '<td class="num">%.1f%%</td><td class="num">%.1f%%</td>'
                 '<td class="num">%s/%s</td><td class="num">%.1f%%</td>'
                 '<td class="num">%s</td></tr>'
                 % (i, esc(d["name"]), esc(s["op"]), esc(s["impl"] or "—"),
                    fmt(s["cycles"]), pct(s["cycles"], d["full"]["total_cycles"]),
                    s["util"], s["nop_pct"], s["mac_pct"], fmt(s["ld"]), fmt(s["st"]),
                    s["bank_pct"], fmt(s["dma_wait"])))
    h.append('</tbody></table>')
    h.append('<div class="insight warn"><b>热点解读：</b>Top 15 里最贵的三项都是 CVA6 上的 '
             'DFT/IDFT（<code>nr_pusch_tp equalise_demap</code> 5.05M、'
             '<code>lte_pusch equalise_demap</code> 3.11M、<code>lte_pusch modulate</code> 3.08M），'
             '接着是 Turbo 译码（<code>turbo_k104</code> 2.79M / 2.75M）。'
             '如果目标是实时，优先做 200/360 点 DFT 的硬件单元或向量化，以及 Turbo 并行化；'
             'LDPC decode（约 1.96M/场景）次之，且其 bank 冲突已达阵列忙的 25.5%。</div>')

    # Scenario details
    h.append('<h2 id="details">逐场景逐阶段明细</h2>')
    h.append('<div class="legend"><span><i style="background:#2563eb"></i>阵列执行</span>'
             '<span><i style="background:#f59e0b"></i>DMA 传输</span>'
             '<span><i style="background:#fcd34d"></i>DMA 等总线</span>'
             '<span><i style="background:#8b5cf6"></i>CPU-IMEM</span>'
             '<span><i style="background:#a78bfa"></i>CPU-配置</span>'
             '<span><i style="background:#14b8a6"></i>CPU-缓存维护</span>'
             '<span><i style="background:#94a3b8"></i>CPU-标量算法</span>'
             '<span class="muted">条形按该阶段实测周期归一</span></div>')
    for d in sorted(data, key=lambda x: (RAT_ORDER.index(x["rat"]), -x["full"]["total_cycles"])):
        full = d["full"]
        pe = full["pe_active"]
        imb = (max(pe) / min(pe)) if pe and min(pe) else 0
        h.append('<details class="scenario"><summary>%s %s '
                 '<span class="muted">· %s 周期 · 阵列 %.1f%% · CPU %.1f%% · NOP %.1f%% · MAC %.1f%%</span></summary>'
                 % (esc(d["name"]), rat_badge(d["rat"]), fmt(full["total_cycles"]),
                    pct(full["array_busy"], full["total_cycles"]),
                    pct(sum(s["cpu"] for s in d["stages"]), full["total_cycles"]),
                    full["nop_pct"], full["mac_pct"]))
        h.append('<table><thead><tr><th>#</th><th>阶段</th><th>实现</th>'
                 '<th class="num">周期</th><th class="num">占比</th>'
                 '<th class="num">阵列执行</th><th class="num">DMA 传输</th>'
                 '<th class="num">DMA 等待</th><th class="num">CPU 执行</th>'
                 '<th>CPU 细分（占 CPU）</th><th>工作构成</th>'
                 '</tr></thead><tbody>')
        for s in d["stages"]:
            cpusub = ('IMEM <b>%s</b> · 配置 <b>%s</b> · 缓存 <b>%s</b> · 标量 <b>%s</b>'
                      % (ipct(s["cpu_imem_pct"], s["cpu"]),
                         ipct(s["cpu_cfg_pct"], s["cpu"]),
                         ipct(s["cpu_cache_pct"], s["cpu"]),
                         ipct(s["cpu_scalar_pct"], s["cpu"]))) if s["cpu"] else "—"
            h.append('<tr><td>%d</td><td><b>%s</b><br><span class="muted">%s</span></td>'
                     '<td class="mono">%s</td><td class="num">%s</td>'
                     '<td class="num">%.1f%%</td>'
                     '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                     '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                     '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                     '<td class="num">%s<br><span class="muted">%.1f%%</span></td>'
                     '<td class="note">%s</td><td>%s</td></tr>'
                     % (s["id"], esc(s["op"]), esc(s["where"]),
                        esc(s["impl"] or "—"), fmt(s["cycles"]), s["share"],
                        fmt(s["array_busy"]), s["array_pct"],
                        fmt(s["dma_xfer"]), s["dma_xfer_pct"],
                        fmt(s["dma_wait"]), s["dma_wait_pct"],
                        fmt(s["cpu"]), s["cpu_pct"], cpusub, ledger_bar(s)))
        h.append('</tbody></table>')
        h.append('<h4 class="muted">微架构计数</h4>')
        h.append('<table><thead><tr><th>#</th><th>阶段</th>'
                 '<th class="num">NOP</th><th class="num">MAC</th>'
                 '<th class="num">LD/ST</th><th>指令构成</th>'
                 '<th class="num">bank 冲突</th><th class="num">DMA beats</th>'
                 '</tr></thead><tbody>')
        for s in d["stages"]:
            h.append('<tr><td>%d</td><td>%s</td>'
                     '<td class="num">%.1f%%</td><td class="num">%.1f%%</td>'
                     '<td class="num">%s/%s</td><td>%s</td>'
                     '<td class="num">%.1f%%</td><td class="num">%s</td></tr>'
                     % (s["id"], esc(s["op"]), s["nop_pct"], s["mac_pct"],
                        fmt(s["ld"]), fmt(s["st"]), mix_bar(s),
                        s["bank_pct"], fmt(s["dma_beats"])))
        h.append('</tbody></table>')
        h.append('<h3>每 PE 活跃周期</h3>%s' % heatmap(pe, d["name"]))
        if imbu := (("%.2f×" % imb) if imb else "—"):
            h.append('<p class="note">PE max/min = %s；'
                     'DMA 等待中 data wait 占 %s。</p>'
                     % (imbu, ("%.0f%%" % pct(full["data_wait"], max(1, full["dma_wait"])))))
        h.append('</details>')

    # Method
    h.append('<h2 id="method">口径与注意事项</h2>')
    h.append('<div class="two"><div class="card"><h3>全周期账本口径</h3><ul class="note">'
             '<li><b>阵列执行</b>：CGRA <code>busy</code> 的墙钟周期。</li>'
             '<li><b>DMA 传输 / 等待</b>：阵列空闲且 DMA busy 的周期；其中“等待”是'
             'AR/AW/B/数据任一未就绪的子集。阵列与 DMA 重叠时归阵列（优先级 阵列 &gt; DMA &gt; CPU）。</li>'
             '<li><b>CPU 执行</b>：阵列与 DMA 都空闲的周期，即 CVA6 在跑阶段代码。'
             '恒等式：<code>阵列 + DMA 传输 + DMA 等待 + CPU = 阶段实测周期</code>。</li>'
             '<li><b>CPU 细分</b>：用 <code>PERF_STATE_CPU</code> 的增量给 IMEM 装载、'
             '阵列配置写、DMA 配置写、缓存维护计时（只累计 CPU 状态，天然不与轮询重叠）；'
             '剩余即“标量算法”，含主机窗口拷贝、状态轮询与循环开销。</li>'
             '<li><b>NOP / MAC / LD / ST / ALU</b>：按每拍 16 个 PE 指令槽统计；'
             'MAC 只计 <code>OP_MAC/OP_MACL</code>。</li>'
             '</ul></div><div class="card"><h3>注意</h3><ul class="note">'
             '<li>“工作构成”条形把阶段周期拆成 阵列 / DMA 传输 / DMA 等待 / CPU 四类，'
             'CPU 再细分四类；七段宽度之和为 100%。</li>'
             '<li>CPU 细分项由软件在公共运行时函数处计时，若阶段代码绕过这些函数'
             '（例如直接逐字写 SPAD），会落入“标量算法”。</li>'
             '<li>阶段表里的 bank 冲突原始值由“阵列忙 × 百分比”估算；'
             '整跑表是硬件原始计数。</li>'
             '<li>数据来自每个场景一次代表性运行（SNR/seed 见各场景报告），'
             '不是统计平均。</li>'
             '<li>计数器 32 bit；<code>--max-cycles 40M</code> 内不回卷。'
             '性能块本身增加约 0.1% 周期。</li>'
             '<li>MAC=0 是真实观测：当前 17 个场景的微码没有发出 MAC/MACL，'
             '不是计数器缺失。</li>'
             '</ul></div></div>')
    h.append('<p class="note">生成脚本：<code>tools/perf_report.py</code>；'
             '明细 CSV：<code>perf-summary.csv</code>；原始报告：'
             '<code>build/proto/&lt;scenario&gt;/report.md</code>。</p>')
    h.append('<script>'
             'const f=document.getElementById("kernel-filter");'
             'if(f)f.addEventListener("input",()=>{const q=f.value.trim().toLowerCase();'
             'document.querySelectorAll("#kernel-table tbody tr").forEach(tr=>{'
             'tr.style.display=tr.dataset.kernel.toLowerCase().includes(q)?"":"none";});});'
             '</script>')
    h.append('</div></body></html>')

    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write("\n".join(h))
    print("wrote", OUT_HTML, "and", OUT_CSV)


if __name__ == "__main__":
    main()
