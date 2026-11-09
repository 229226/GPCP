#!/usr/bin/env python3
"""docs/20 §6 第 4 步：落点计划 -> 运行器能 include 的 plan.h，并产出激励。

    python3 proto/emit.py --scenario nr_pdcch
    python3 proto/emit.py --all

输入（build/proto/<scenario>/ 下，由 compile.py 生成）：
    ir.json / plan-base.json
输出（同一目录）：
    plan.h        阶段表 / 参数摘要 / 每时隙预算（**运行器唯一的编译期输入**）
    stimulus.bin  激励（协议输入，不含期望符号）
    golden.json   判据锚点（由 Python 模型算，供 proto_eval.py）

约定：plan.h 里可以出现硬件数字（阵列规模、预算周期），因为它是**生成物**；
它们的来源只有 plan-*.json（即 cgra/target.yaml），本文不自己算硬件。
"""
import argparse
import hashlib
import importlib.util
import json
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

_WHERE = {"control-core": "PROTO_WHERE_CPU",
          "array": "PROTO_WHERE_ARRAY",
          "dma": "PROTO_WHERE_DMA"}


def load_model(scenario):
    """场景的物理层参照（激励 + 黄金 + 参照门）。缺了就直接报错，不许猜。"""
    path = os.path.join(_HERE, "models", scenario + ".py")
    if not os.path.exists(path):
        raise SystemExit(
            "[FAIL] 缺少 proto/models/%s.py：每个场景都要给出激励与黄金锚点"
            "（docs/20 §8 规则 1）" % scenario)
    spec = importlib.util.spec_from_file_location("proto_model_" + scenario, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for fn in ("stimulus_bytes", "golden"):
        if not hasattr(mod, fn):
            raise SystemExit("[FAIL] proto/models/%s.py 缺少 %s()" % (scenario, fn))
    return mod


def _cstr(s):
    return '"%s"' % (s or "")


def emit(scenario, outdir):
    ir_path = os.path.join(outdir, "ir.json")
    plan_path = os.path.join(outdir, "plan-base.json")
    for p in (ir_path, plan_path):
        if not os.path.exists(p):
            raise SystemExit("[FAIL] 缺少 %s（先跑 proto/compile.py --scenario %s）"
                             % (p, scenario))

    with open(ir_path, "rb") as f:
        ir_bytes = f.read()
    ir = json.loads(ir_bytes.decode("utf-8"))
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)

    model = load_model(scenario)
    stim = model.stimulus_bytes()
    gold = model.golden()

    # 参数摘要：IR + 激励的指纹。运行器把它写进结果区 [8]，评估器拿它对账
    # "旧二进制服务新激励"（AGENTS §5.4 的构建坑）。
    params_hash = int(hashlib.sha256(ir_bytes + stim).hexdigest()[:8], 16)
    gold["scenario"] = scenario
    gold["params_hash"] = "0x%08x" % params_hash
    gold["stimulus_words"] = len(stim) // 4

    tgt = plan["target"]
    budget = ir.get("budget") or {}
    slot_ms = float(budget.get("slot_ms", 0.0))
    clock_hz = int(tgt.get("clock_hz", 0))
    budget_cycles = int(round(slot_ms * clock_hz / 1000.0)) if slot_ms and clock_hz else 0
    stim_magic = struct.unpack("<I", stim[:4])[0] if len(stim) >= 4 else 0

    lines = [
        "/* 生成物：proto/emit.py 从 ir.json + plan-base.json 生成，勿手改。",
        " * 场景 %s（%s/%s），目标 %s（%dx%d = %d PE，%d bank x %d 字）。"
        % (scenario, ir.get("rat"), ir.get("direction"), tgt.get("source", "?"),
           tgt["rows"], tgt["cols"], tgt["n_pes"], tgt["banks"],
           tgt["words_per_bank"]),
        " * 改场景 / 换目标后重跑 `make proto PROTO=%s`。" % scenario,
        " */",
        "#ifndef PROTO_PLAN_H",
        "#define PROTO_PLAN_H",
        "",
        '#define PROTO_SCENARIO     "%s"' % scenario,
        "#define PROTO_PARAMS_HASH  0x%08xul" % params_hash,
        "#define PROTO_N_STAGES     %d" % len(plan["stages"]),
        "#define PROTO_CLOCK_HZ     %dul" % clock_hz,
        "#define PROTO_SLOT_MS      %.3f" % slot_ms,
        "#define PROTO_BUDGET_CYCLES %dul" % budget_cycles,
        "#define PROTO_STIM_MAGIC   0x%08xul" % stim_magic,
        "#define PROTO_STIM_WORDS   %d" % (len(stim) // 4),
        "",
        "typedef enum { PROTO_WHERE_CPU = 0, PROTO_WHERE_ARRAY = 1,",
        "               PROTO_WHERE_DMA = 2 } proto_where_t;",
        "",
        "typedef struct {",
        "    uint8_t     id;            /* 语义图节点号（docs/20 §2 的顺序） */",
        "    uint8_t     where;         /* 落点：CPU / 阵列 / DMA */",
        "    uint16_t    planned_runs;  /* 计划里的阵列运行次数（0 = 纯标量） */",
        "    const char *op;            /* 协议动词 */",
        "    const char *impl;          /* 编译器选的实现（诊断用） */",
        "} proto_stage_t;",
        "",
        "static const proto_stage_t proto_stages[PROTO_N_STAGES] = {",
    ]
    for st in plan["stages"]:
        where = _WHERE.get(st["where"], "PROTO_WHERE_CPU")
        lines.append("    { %2d, %-18s %3d, %-18s %s },"
                     % (st["id"], where + ",", int(st.get("array_runs", 0)),
                        _cstr(st["op"]) + ",", _cstr(st.get("impl") or "")))
    lines += ["};", "", "#endif /* PROTO_PLAN_H */", ""]

    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "plan.h"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(os.path.join(outdir, "stimulus.bin"), "wb") as f:
        f.write(stim)
    with open(os.path.join(outdir, "golden.json"), "w", encoding="utf-8") as f:
        json.dump(gold, f, indent=1, ensure_ascii=False)

    print("scenario   : %s" % scenario)
    print("params hash: 0x%08x  (%d 字节 IR + %d 字节激励)"
          % (params_hash, len(ir_bytes), len(stim)))
    print("plan.h     : %d 个阶段，预算 %d 周期（%.3f ms @ %d Hz）"
          % (len(plan["stages"]), budget_cycles, slot_ms, clock_hz))
    for st in plan["stages"]:
        print("  %-2d %-17s %-9s %s" % (st["id"], st["op"], st["where"],
                                        st.get("impl") or "-"))
    return 0


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out", default=os.path.join(_ROOT, "build", "proto"))
    a = ap.parse_args(argv)

    scdir = os.path.join(_HERE, "scenarios")
    if a.all:
        names = sorted(f[:-3] for f in os.listdir(scdir)
                       if f.endswith(".py") and not f.startswith("_"))
    elif a.scenario:
        names = [a.scenario]
    else:
        print(__doc__)
        return 2

    for n in names:
        print("== %s ==" % n)
        emit(n, os.path.join(a.out, n))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
