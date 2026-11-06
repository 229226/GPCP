#!/usr/bin/env python3
"""Morpher 调度 → GPCP 32 位微码（后端 v0）。

输入：
    schedule.csv   t,x,y,idx,op        （Morpher 映射结果，由 dumpSchedule 导出）
    *_DFG.xml                          （节点依赖：谁把值喂给谁的哪个操作数）

输出：
    prog.words     每个周期 16 个 32 位字（cycle-major，与 IMEM 布局一致）
    report.json    哪些节点编了、哪些编不了、为什么

v0 只做"能明确翻译"的部分，编不了的不猜，直接报告原因：
    no_op        我们的 ISA 里没有这个算子（SELECT/CMP/CMERGE/MOVC/...）
    no_addr      访存节点的地址不是 {bank,word} 立即数（需要地址折叠，未实现）
    no_route     生产者不在同 PE 的上一拍、也不在四邻的上一拍（需要多跳路由）
    reg_pressure 单 PE 活跃值超过 8 个寄存器

用法：
    python3 tools/morpher/cc.py schedule.csv array_add_PartPredDFG.xml --ii 4
"""

import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
from cgra import enc, NPES            # noqa: E402  复用唯一编码定义

# Morpher 算子名 → 我们的算子名（只列语义等价的）
OP_MAP = {"ADD": "add", "SUB": "sub", "MUL": "mul", "AND": "and", "XOR": "xor",
          "ARS": "shr", "LS": "shl", "LOAD": "ld", "STORE": "st",
          "MOV": "mov", "MAC": "mac", "MAX": "max", "MIN": "min"}

# 我们的操作数选择器
NEIGH = {(0, -1): "n", (1, 0): "e", (0, 1): "s", (-1, 0): "w"}


def load_schedule(path):
    nodes = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            nodes[int(r["idx"])] = {"t": int(r["t"]), "x": int(r["x"]),
                                    "y": int(r["y"]), "op": r["op"]}
    return nodes


def load_edges(path):
    """Producer -> [(consumer, operand_slot)]，从 XML 的 Outputs 反推。"""
    s = open(path, encoding="utf-8", errors="replace").read()
    preds = {}
    for m in re.finditer(r'<Node idx="(\d+)".*?</Node>', s, re.S):
        body = m.group(0)
        for out in re.finditer(r'<Output idx="(\d+)"[^>]*type="(\w+)"', body):
            cons, slot = int(out.group(1)), out.group(2)
            preds.setdefault(cons, []).append((int(m.group(1)), slot))
    return preds


def pe_of(x, y):
    return y * 4 + x          # 我们：pe = row*4 + col


def translate(nodes, preds, ii):
    words, report = {}, {"encoded": [], "skipped": {}}
    reg_live, reg_free = {}, {pe_of(n["x"], n["y"]): list(range(7, -1, -1))
                              for n in nodes.values()}
    out_words = [[0] * NPES for _ in range(ii)]

    def skip(idx, why):
        report["skipped"].setdefault(why, []).append(idx)

    for idx in sorted(nodes, key=lambda i: (nodes[i]["t"], i)):
        n = nodes[idx]
        t, x, y, op = n["t"], n["x"], n["y"], n["op"]
        pe = pe_of(x, y)
        ours = OP_MAP.get(op)
        if not ours:
            skip(idx, "no_op:%s" % op)
            continue
        if ours in ("ld", "st"):
            skip(idx, "no_addr")          # 地址折叠未实现
            continue

        a = b = "zero"
        ok = True
        for (src, slot) in preds.get(idx, [])[:2]:
            if src not in nodes:
                ok = False
                break
            s = nodes[src]
            dt = (t - s["t"]) % ii
            if dt != 1:
                skip(idx, "no_route")
                ok = False
                break
            if (s["x"], s["y"]) == (x, y):
                key = (pe, src)
                if key not in reg_live:
                    skip(idx, "no_route")
                    ok = False
                    break
                sel = "r%d" % reg_live[key]
            else:
                d = NEIGH.get((s["x"] - x, s["y"] - y))
                if d is None:
                    skip(idx, "no_route")
                    ok = False
                    break
                sel = d
            if slot in ("I1", "P") and a == "zero":
                a = sel
            else:
                b = sel
        if not ok:
            continue
        free = reg_free[pe]
        if not free:
            skip(idx, "reg_pressure")
            continue
        dst = free.pop()
        reg_live[(pe, idx)] = dst
        out_words[t][pe] = enc(ours, a, b, dst, 31 if ours == "shr" else 0)
        report["encoded"].append({"idx": idx, "t": t, "pe": pe, "op": ours,
                                  "a": a, "b": b, "dst": dst})
    return out_words, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("schedule")
    ap.add_argument("dfg")
    ap.add_argument("--ii", type=int, required=True)
    ap.add_argument("--out", default="prog.json")
    a = ap.parse_args()

    nodes = load_schedule(a.schedule)
    preds = load_edges(a.dfg)
    words, rep = translate(nodes, preds, a.ii)

    flat = [w for cyc in words for w in cyc]
    rep["summary"] = {
        "nodes_total": len(nodes),
        "encoded": len(rep["encoded"]),
        "skipped": sum(len(v) for v in rep["skipped"].values()),
        "by_reason": {k: len(v) for k, v in sorted(rep["skipped"].items())},
        "cycles": a.ii,
        "words": len(flat),
        "nonzero_words": sum(1 for w in flat if w),
    }
    json.dump({"words": flat, "report": rep}, open(a.out, "w"), indent=1)

    s = rep["summary"]
    print("nodes %d -> encoded %d, skipped %d" % (s["nodes_total"], s["encoded"], s["skipped"]))
    for k, v in s["by_reason"].items():
        print("   %-22s %d" % (k, v))
    print("cycles=%d words=%d nonzero=%d -> %s"
          % (s["cycles"], s["words"], s["nonzero_words"], a.out))


if __name__ == "__main__":
    main()
