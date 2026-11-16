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
"""协议级落点对比：同一场景的 "尽量 CGRA" 与 "全 CPU" 两次运行，逐阶段比周期。

输入：
    build/proto/<scenario>/plan-base.json
    sim/soc/build/proto_<scenario>_dump.txt         （run-proto-<scenario>）
    sim/soc/build/proto_<scenario>_cpu_dump.txt     （run-proto-cpu-<scenario>）

输出：
    build/proto/compare_<scenario>.csv
    stdout 表格（阶段 -> CPU 周期 / CGRA 周期 / 加速比 / 结论）

口径：两次运行使用同一份 plan.h / 激励 / 判据，阶段编号一一对应；只有阶段
内部算法路径不同（-DPROTO_CPU_ONLY）。因此逐阶段周期可直接相除。
"""
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from checklib import parse_dump, words32   # noqa: E402

STAGE_WORD = 10


def load_stages(scenario):
    p = ROOT / "build/proto" / scenario / "plan-base.json"
    if not p.exists():
        raise SystemExit("[FAIL] 缺少 %s（先跑 run-proto-%s）" % (p, scenario))
    return json.loads(p.read_text())["stages"]


def stage_cycles(dump, nst):
    st = parse_dump(dump)
    if st.get("timeout", 1):
        raise SystemExit("[FAIL] %s 超时" % dump)
    w = words32(st)
    if len(w) < 10 or w[0] != 0x50524F54:
        raise SystemExit("[FAIL] %s 结果区 magic 不对" % dump)
    out = {}
    for i in range(nst):
        sid = w[STAGE_WORD + 3 * i]
        out[sid] = w[STAGE_WORD + 3 * i + 1]
    return w[2], out


def verdict(cg, cpu):
    if cg <= 0:
        return "-"
    r = cpu / cg                     # >1 = CGRA 更快
    if r >= 1.2:
        return "CGRA"
    if r <= 1.0 / 1.2:
        return "CPU"
    return "混合"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--cgra", required=True)
    ap.add_argument("--cpu", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    stages = load_stages(a.scenario)
    cg_total, cg = stage_cycles(a.cgra, len(stages))
    cpu_total, cpu = stage_cycles(a.cpu, len(stages))

    rows = []
    print(f"场景 {a.scenario}：CGRA 路径 {cg_total} 周期，CPU 路径 {cpu_total} 周期，"
          f"整体 {cpu_total / cg_total:.2f}× CPU/CGRA")
    print(f"{'id':>2}  {'阶段':<18} {'CGRA':>10} {'CPU':>10} {'CPU/CGRA':>9}  结论")
    print("-" * 70)
    for st in stages:
        sid = st["id"]
        c = cg.get(sid, 0)
        p = cpu.get(sid, 0)
        r = (p / c) if c else 0.0
        v = verdict(c, p)
        rows.append({"id": sid, "op": st["op"], "impl": st.get("impl") or "",
                     "cgra_cycles": c, "cpu_cycles": p,
                     "cpu_over_cgra": "%.3f" % r if c else "",
                     "verdict": v})
        print(f"{sid:>2}  {st['op']:<18} {c:>10} {p:>10} "
              f"{('%.2f' % r) if c else '-':>9}  {v}")
    print("-" * 70)
    rows.append({"id": "TOTAL", "op": "整链", "impl": "",
                 "cgra_cycles": cg_total, "cpu_cycles": cpu_total,
                 "cpu_over_cgra": "%.3f" % (cpu_total / cg_total),
                 "verdict": verdict(cg_total, cpu_total)})
    print(f"{'':>2}  {'整链':<18} {cg_total:>10} {cpu_total:>10} "
          f"{cpu_total / cg_total:>9.2f}  {verdict(cg_total, cpu_total)}")

    out = Path(a.out) if a.out else ROOT / "build/proto" / ("compare_%s.csv" % a.scenario)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "op", "impl", "cgra_cycles",
                                          "cpu_cycles", "cpu_over_cgra", "verdict"])
        w.writeheader()
        w.writerows(rows)
    print(f"[info] wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
