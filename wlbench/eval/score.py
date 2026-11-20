#!/usr/bin/env python3
"""wlbench 评分器：给定后端频率与逐时隙周期数，判定协议实时性并打分。

判定口径：
  budget_cycles = clock_hz * slot_ms / 1000
  on_time       = cycles[i] <= budget_cycles
  on_time_rate  = on_time / slots
  required_hz   = max(cycles) * 1000 / slot_ms      # 100% 按时所需最低频率
  score         = 100 * on_time_rate（正确性失败时直接 0）

输入：
  --cycles FILE   每行一个时隙的周期数；只有一个值时按 --slots 均摊
  --clock-hz F    可以是逗号分隔的多个频率
  --correct 1|0   可选；缺省表示只做时间判定
"""
import argparse
import json
import math
import re
import sys


def parse_cycles(path):
    vals = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for tok in re.findall(r"-?\d+", line.replace(",", " ")):
                vals.append(int(tok))
    return vals


def parse_freqs(hz_arg, mhz_arg):
    out = []
    if hz_arg:
        for tok in hz_arg.replace(" ", ",").split(","):
            if tok:
                out.append(int(float(tok)))
    if mhz_arg:
        for tok in mhz_arg.replace(" ", ",").split(","):
            if tok:
                out.append(int(float(tok) * 1e6))
    return out


def read_correct(path, direct):
    if direct is not None:
        return bool(direct)
    if path:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("correct"))
    return None


def pct_index(n, frac):
    if n <= 0:
        return 0
    idx = int(frac * (n - 1))
    if idx < 0:
        idx = 0
    if idx > n - 1:
        idx = n - 1
    return idx


def evaluate(cycles, slots, clocks, slot_ms, correct, target):
    rows = []
    for clock in clocks:
        budget = int(clock * slot_ms / 1000.0 + 0.5)
        if budget < 1:
            budget = 1
        on_time = sum(1 for c in cycles if c <= budget)
        rate = on_time / slots
        mx = max(cycles)
        total = sum(cycles)
        mean = total / slots
        ordered = sorted(cycles)
        p95 = ordered[pct_index(slots, 0.95)]
        need_idx = min(slots - 1, max(0, int(math.ceil(target * slots)) - 1))
        req_target = ordered[need_idx]
        overrun = sum(max(0, c - budget) for c in cycles)

        if correct is False:
            score = 0.0
        else:
            score = round(100.0 * rate, 2)

        if correct is False:
            verdict = "FAIL"
        elif rate >= target:
            verdict = "PASS" if correct is True else "TIMING_ONLY"
        else:
            verdict = "LATE"

        rows.append({
            "clock_hz": clock,
            "clock_mhz": clock / 1e6,
            "budget_cycles": budget,
            "on_time_slots": on_time,
            "on_time_rate": rate,
            "total_cycles": total,
            "mean_cycles": mean,
            "p95_cycles": p95,
            "worst_cycles": mx,
            "worst_over_budget": (mx / budget) if budget > 0 else 0.0,
            "overrun_cycles": overrun,
            "wall_ms_total": total / clock * 1000.0,
            "required_clock_hz": int(math.ceil(mx * 1000.0 / slot_ms)),
            "required_clock_p95_hz": int(math.ceil(p95 * 1000.0 / slot_ms)),
            "required_clock_target_hz": int(math.ceil(req_target * 1000.0 / slot_ms)),
            "score": score,
            "verdict": verdict,
        })
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wlbench-score")
    ap.add_argument("--cycles", required=True, help="逐时隙周期数文件")
    ap.add_argument("--slots", type=int, help="时隙数；只有一个周期值时必填")
    ap.add_argument("--clock-hz", help="后端频率（Hz），逗号分隔可给多个")
    ap.add_argument("--clock-mhz", help="后端频率（MHz），逗号分隔可给多个")
    ap.add_argument("--slot-ms", type=float, default=1.0, help="每时隙预算（ms）")
    ap.add_argument("--target", type=float, default=1.0, help="目标按时率，默认 1.0")
    ap.add_argument("--correct", type=int, choices=[0, 1], help="正确性判定结果")
    ap.add_argument("--correct-json", help="testbench 输出的 JSON（取 correct 字段）")
    ap.add_argument("--json", help="把完整评分写到 JSON")
    a = ap.parse_args(argv)

    if a.slot_ms <= 0.0:
        raise SystemExit("--slot-ms 必须为正")
    if not (0.0 <= a.target <= 1.0):
        raise SystemExit("--target 必须在 [0,1]")

    clocks = parse_freqs(a.clock_hz, a.clock_mhz)
    if not clocks:
        raise SystemExit("必须给 --clock-hz 或 --clock-mhz")

    raw = parse_cycles(a.cycles)
    if not raw:
        raise SystemExit("周期文件为空")
    if any(c < 0 for c in raw):
        raise SystemExit("周期数不能为负")

    if a.slots:
        slots = a.slots
    else:
        if len(raw) == 1:
            raise SystemExit("只有一个周期值时必须给 --slots")
        slots = len(raw)
    if slots <= 0:
        raise SystemExit("--slots 必须为正")

    if len(raw) == 1:
        total = raw[0]
        cycles = [total // slots + (1 if i < total % slots else 0)
                  for i in range(slots)]
    else:
        if len(raw) < slots:
            raise SystemExit("周期数不足：%d < slots %d" % (len(raw), slots))
        cycles = raw[:slots]

    correct = read_correct(a.correct_json, a.correct)
    rows = evaluate(cycles, slots, clocks, a.slot_ms, correct, a.target)

    print("wlbench score · %d slots · %.3f ms/slot · correct=%s · target=%.2f"
          % (slots, a.slot_ms, "unknown" if correct is None else str(correct),
             a.target))
    print("%10s %12s %8s %10s %10s %10s %10s %12s %12s %8s %s" %
          ("clock_MHz", "budget_cyc", "on_time", "rate", "worst", "mean",
           "p95", "need_MHz", "need_p95_MHz", "score", "verdict"))
    for r in rows:
        print("%10.2f %12d %8d %9.2f%% %10d %10.1f %10d %12.2f %12.2f %8.2f %s" %
              (r["clock_mhz"], r["budget_cycles"], r["on_time_slots"],
               r["on_time_rate"] * 100.0, r["worst_cycles"], r["mean_cycles"],
               r["p95_cycles"], r["required_clock_hz"] / 1e6,
               r["required_clock_p95_hz"] / 1e6, r["score"], r["verdict"]))

    best = rows[0]
    print("required clock (100%% on-time): %.2f MHz   p95: %.2f MHz" %
          (best["required_clock_hz"] / 1e6, best["required_clock_p95_hz"] / 1e6))

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({
                "schema": "wlbench-score/1",
                "slots": slots,
                "slot_ms": a.slot_ms,
                "correct": correct,
                "target_on_time": a.target,
                "clocks": rows,
            }, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return 0 if any(r["verdict"] in ("PASS", "TIMING_ONLY") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
