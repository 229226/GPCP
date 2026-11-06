#!/usr/bin/env python3
"""开发工时台账 —— 按领域记录每个任务花了多久、产出是什么。

单一事实来源是 docs/dev-log.csv（append-only，进 git，可 diff 可审计）。
本工具只负责**录入规范化**与**事后审计**，不发明数据。

    python3 tools/devlog.py start model "64QAM 放开 Qm=6"
    python3 tools/devlog.py stop --commit edb44d7 --evidence "120 符号、3.27M 周期"
    python3 tools/devlog.py add --domain sw --kind feature --minutes 25 \
            --task "PUSCH IDFT 定点化" --commit abf6cd9 --confidence measured
    python3 tools/devlog.py report            # 按领域/类型/每天汇总
    python3 tools/devlog.py report --week     # 按 ISO 周汇总
    python3 tools/devlog.py check             # 枚举、时长自洽、提交覆盖
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "docs", "dev-log.csv")
PENDING = os.path.join(ROOT, "build", "devlog-pending.json")

FIELDS = ["date", "start", "end", "minutes", "domain", "kind",
          "task", "commit", "confidence", "evidence"]

DOMAINS = {
    "arch":      "架构 / 方案设计与评估（不落代码的推演、选型、取舍）",
    "rtl":       "RTL 设计（Verilog / SystemVerilog）",
    "microcode": "CGRA 微码与查表程序（*_prog.h、*_luts.h）",
    "model":     "协议参考模型（tools/nr_*.py 黄金模型）",
    "sw":        "裸机 C / 驱动 / SoC 软件",
    "verif":     "验证与测试（checker、回归档、testbench）",
    "synth":     "综合 / 时序 / 面积",
    "pd":        "物理设计（布局布线、DRC / LVS）",
    "infra":     "工具链与仿真基础设施（Makefile、bench、脚本）",
    "doc":       "文档",
    "wait":      "纯等待（后台回归 / 夜间长跑，期间未做别的任务）",
}

KINDS = {
    "feature":    "新功能 / 新覆盖",
    "fix":        "修缺陷（含回归修复）",
    "infra":      "基础设施与流程",
    "experiment": "探索性实验（含负结果，无提交也要记）",
    "doc":        "文档",
    "wait":       "纯等待（与 domain=wait 配对使用）",
}

# 各 AI 工具的入口文档都必须内联同一份规则块（字节一致），由 check 机械校验。
ENTRY_DOCS = ["AGENTS.md", "README.md", "CLAUDE.md", "GEMINI.md",
              ".cursorrules", ".github/copilot-instructions.md"]
BLOCK_BEGIN = "<!-- devlog-rule:v1 -->"
BLOCK_END = "<!-- /devlog-rule -->"

CONFIDENCE = {
    "measured": "有明确起止（start/stop 或已记录的时钟）",
    "estimate": "事后按区间与内容推断，误差约 ±25%",
    "backfill": "追溯补齐的历史数据，仅用于基线，不参与精度统计",
}


def die(msg):
    sys.stderr.write("devlog: %s\n" % msg)
    sys.exit(2)


def today():
    return dt.date.today().isoformat()


def now_hm():
    return dt.datetime.now().strftime("%H:%M")


def read_rows():
    if not os.path.exists(LOG):
        return []
    with open(LOG, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return [dict(row) for row in r]


def write_rows(rows):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    rows.sort(key=lambda r: (r["date"], r["start"] or "99:99"))
    with open(LOG, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in FIELDS})


def to_min(hm):
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def from_min(m):
    return "%02d:%02d" % (m // 60 % 24, m % 60)


def append(row, quiet=False):
    rows = read_rows()
    rows.append(row)
    write_rows(rows)
    if not quiet:
        print("  + %s  %3s min  %-9s %-9s %s"
              % (row["date"], row["minutes"], row["domain"], row["kind"],
                 row["task"][:52]))


# ---------------------------------------------------------------- commands

def cmd_start(a):
    os.makedirs(os.path.dirname(PENDING), exist_ok=True)
    if os.path.exists(PENDING) and not a.force:
        die("已有未结束的计时（%s），先 stop 或加 --force"
            % json.load(open(PENDING, encoding="utf-8"))["task"])
    if a.domain not in DOMAINS:
        die("domain 必须是 %s 之一" % "/".join(sorted(DOMAINS)))
    if a.kind not in KINDS:
        die("kind 必须是 %s 之一" % "/".join(sorted(KINDS)))
    rec = {"domain": a.domain, "kind": a.kind, "task": a.task,
           "date": a.date or today(), "start": now_hm()}
    json.dump(rec, open(PENDING, "w", encoding="utf-8"))
    print("started %s / %s / %s at %s" % (rec["domain"], rec["kind"],
                                          rec["task"], rec["start"]))


def cmd_stop(a):
    if not os.path.exists(PENDING):
        die("没有进行中的计时；用 add 直接补录")
    rec = json.load(open(PENDING, encoding="utf-8"))
    end = a.end or now_hm()
    mins = to_min(end) - to_min(rec["start"])
    if mins < 0:                       # 跨零点
        mins += 24 * 60
    mins = max(1, mins)                # 不足 1 min 记 1：0 会被 check 判非法
    rec.update({"end": end, "minutes": mins,
                "commit": a.commit or "", "evidence": a.evidence or "",
                "confidence": "measured"})
    append(rec)
    os.remove(PENDING)
    if mins > 180:
        print("  注意：单条 %.1f h，若中途穿插了别的任务请拆条重记"
              % (mins / 60.0))


def cmd_add(a):
    if a.domain not in DOMAINS:
        die("domain 必须是 %s 之一" % "/".join(sorted(DOMAINS)))
    if a.kind not in KINDS:
        die("kind 必须是 %s 之一" % "/".join(sorted(KINDS)))
    if a.confidence not in CONFIDENCE:
        die("confidence 必须是 %s 之一" % "/".join(sorted(CONFIDENCE)))
    if not a.minutes or a.minutes <= 0:
        die("--minutes 必须为正整数")
    start = a.start or ""
    end = a.end or ""
    if bool(start) != bool(end):
        die("--start 与 --end 要同时给或同时省略")
    if start:
        span = to_min(end) - to_min(start)
        if span < 0:
            span += 24 * 60
        if abs(span - a.minutes) > 5:
            die("--start/--end 跨 %d min，与 --minutes %d 差超过 5 min"
                % (span, a.minutes))
    append({"date": a.date or today(), "start": start, "end": end,
            "minutes": a.minutes, "domain": a.domain, "kind": a.kind,
            "task": a.task, "commit": a.commit or "",
            "confidence": a.confidence, "evidence": a.evidence or ""})


def rule_block(path):
    """Return the normalised shared rule block, or None if absent."""
    if not os.path.exists(path):
        return None
    text = open(path, encoding="utf-8").read()
    m = re.search(re.escape(BLOCK_BEGIN) + r"(.*?)" + re.escape(BLOCK_END),
                  text, re.S)
    if not m:
        return None
    return "\n".join(ln.rstrip() for ln in m.group(1).strip().splitlines())


def check_entry_docs(errs, warns):
    """每个 AI 入口文档都必须内联同一份规则块，否则该工具读不到规则。"""
    blocks = {}
    for rel in ENTRY_DOCS:
        fp = os.path.join(ROOT, rel)
        b = rule_block(fp)
        if b is None:
            if os.path.exists(fp):
                errs.append("%s 存在但缺规则块（%s ... %s）"
                            % (rel, BLOCK_BEGIN, BLOCK_END))
            else:
                warns.append("入口文档缺失：%s（该工具读不到台账规则）" % rel)
        else:
            blocks[rel] = b
    if not blocks:
        return
    ref_key = "AGENTS.md" if "AGENTS.md" in blocks else sorted(blocks)[0]
    ref = blocks[ref_key]
    for rel, b in sorted(blocks.items()):
        if b != ref:
            errs.append("%s 的规则块与 %s 不一致（改了口径没同步？）"
                        % (rel, ref_key))


def cmd_check(a):
    rows = read_rows()
    errs, warns = [], []

    if not rows:
        errs.append("docs/dev-log.csv 为空或不存在")
    for i, r in enumerate(rows, 2):
        tag = "第 %d 行" % i
        if r["domain"] not in DOMAINS:
            errs.append("%s: 未知 domain %r" % (tag, r["domain"]))
        if r["kind"] not in KINDS:
            errs.append("%s: 未知 kind %r" % (tag, r["kind"]))
        if r["confidence"] not in CONFIDENCE:
            errs.append("%s: 未知 confidence %r" % (tag, r["confidence"]))
        try:
            m = int(r["minutes"])
        except ValueError:
            errs.append("%s: minutes 不是整数 %r" % (tag, r["minutes"]))
            continue
        if not 0 < m <= 24 * 60:
            errs.append("%s: minutes=%d 超出 (0, 1440]" % (tag, m))
        if r["domain"] != "wait" and not r["commit"] and r["kind"] != "experiment":
            warns.append("%s: %s/%s 没有 commit 也没有标 experiment，"
                         "无法核对产出" % (tag, r["domain"], r["kind"]))
        if not r["evidence"] and r["domain"] != "wait":
            warns.append("%s: 缺 evidence（周期数/门数/PASS 行）" % tag)

    # 每日总时长不能超过 24 h（并行拆分只能减少，不会增加）
    per_day = {}
    for r in rows:
        per_day[r["date"]] = per_day.get(r["date"], 0) + int(r["minutes"] or 0)
    for d, m in sorted(per_day.items()):
        if m > 24 * 60:
            errs.append("%s: 合计 %d min 超过 24 h" % (d, m))

    # 提交覆盖：最近 N 个提交都应有记录（文档提交除外）
    if a.commits > 0:
        try:
            out = subprocess.check_output(
                ["git", "-C", ROOT, "log", "--pretty=%h|%s", "-%d" % a.commits],
                text=True)
        except (OSError, subprocess.CalledProcessError):
            warns.append("无法读取 git log，跳过提交覆盖检查")
            out = ""
        covered = set()
        for r in rows:
            for c in re.split(r"[;,]", r["commit"]):
                if c.strip():
                    covered.add(c.strip())
        for line in out.strip().splitlines():
            h, _, subj = line.partition("|")
            if h in covered:
                continue
            if re.match(r"^(docs?|devlog)\b", subj, re.I):
                continue
            warns.append("提交 %s 没有工时记录：%s" % (h, subj[:46]))

    check_entry_docs(errs, warns)

    for w in warns:
        print("WARN  %s" % w)
    for e in errs:
        print("ERROR %s" % e)
    print("== devlog check: %d 行, %d error, %d warn =="
          % (len(rows), len(errs), len(warns)))
    return 1 if errs else 0


def _w(s):
    """显示宽度：CJK 全角算两格，否则表格会歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s, width):
    return s + " " * max(0, width - _w(s))


def _table(title, pairs, total):
    """pairs: [(key, (count, minutes), label), ...]"""
    print("\n%s" % title)
    print("  %s%s%5s %8s %7s %6s"
          % (_pad("", 40), "", "条", "小时", "均值", "占比"))
    n = m_sum = 0
    for key, (cnt, mins), label in pairs:
        n += cnt
        m_sum += mins
        print("  %s%5d %8.1f %7.0f %5.1f%%"
              % (_pad(("%s %s" % (key, label)).strip(), 40), cnt,
                 mins / 60.0, mins / float(cnt), 100.0 * mins / total))
    print("  %s%5d %8.1f %7.0f"
          % (_pad("合计", 40), n, m_sum / 60.0, m_sum / float(n)))


def cmd_report(a):
    rows = read_rows()
    if a.since:
        rows = [r for r in rows if r["date"] >= a.since]
    if not rows:
        print("（无记录）")
        return 0
    total = sum(int(r["minutes"]) for r in rows)

    def agg(key):
        d = {}
        for r in rows:
            k = r[key]
            c, m = d.get(k, (0, 0))
            d[k] = (c + 1, m + int(r["minutes"]))
        return d

    dom = agg("domain")
    order = sorted(dom.items(), key=lambda kv: -kv[1][1])
    _table("按领域（写什么，占多少时间）",
           [(k, v, DOMAINS.get(k, "").split("（")[0].split(" /")[0])
            for k, v in order], total)

    kind = agg("kind")
    _table("按类型（为什么花时间）",
           [(k, v, "") for k, v in sorted(kind.items(),
                                          key=lambda kv: -kv[1][1])], total)

    day = agg("date")
    print("\n按天")
    print("  %s%5s %8s" % (_pad("", 12), "条", "小时"))
    for d in sorted(day):
        print("  %s%5d %8.1f" % (_pad(d, 12), day[d][0], day[d][1] / 60.0))

    if a.week:
        wk = {}
        for r in rows:
            y, w, _ = dt.date.fromisoformat(r["date"]).isocalendar()
            k = "%d-W%02d" % (y, w)
            c, m = wk.get(k, (0, 0))
            wk[k] = (c + 1, m + int(r["minutes"]))
        print("\n按 ISO 周")
        for k in sorted(wk):
            print("  %-10s %3d 条  %6.1f h" % (k, wk[k][0], wk[k][1] / 60.0))

    meas = [r for r in rows if r["confidence"] == "measured"]
    if meas:
        mt = sum(int(r["minutes"]) for r in meas)
        print("\n精度：measured %d 条占 %.0f%% 时长；estimate/backfill 其余"
              % (len(meas), 100.0 * mt / total))
    print("\n合计 %.1f h / %d 条" % (total / 60.0, len(rows)))
    return 0


def cmd_domains(a):
    print("领域（domain）—— 写什么：")
    for k, v in DOMAINS.items():
        print("  %-10s %s" % (k, v))
    print("\n类型（kind）—— 为什么：")
    for k, v in KINDS.items():
        print("  %-10s %s" % (k, v))
    print("\n置信度：")
    for k, v in CONFIDENCE.items():
        print("  %-10s %s" % (k, v))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="开始一段计时")
    s.add_argument("domain")
    s.add_argument("task")
    s.add_argument("--kind", default="feature")
    s.add_argument("--date")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("stop", help="结束当前计时并落账")
    s.add_argument("--commit")
    s.add_argument("--evidence")
    s.add_argument("--end")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("add", help="直接补录一条")
    s.add_argument("--domain", required=True)
    s.add_argument("--kind", required=True)
    s.add_argument("--minutes", type=int, required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--commit")
    s.add_argument("--evidence")
    s.add_argument("--confidence", default="measured")
    s.add_argument("--date")
    s.add_argument("--start")
    s.add_argument("--end")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("check", help="校验数据与提交覆盖")
    s.add_argument("--commits", type=int, default=15)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("report", help="按领域/类型/天/周汇总")
    s.add_argument("--since")
    s.add_argument("--week", action="store_true")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("domains", help="列出领域与类型定义")
    s.set_defaults(func=cmd_domains)

    a = ap.parse_args()
    sys.exit(a.func(a) or 0)


if __name__ == "__main__":
    main()
