#!/usr/bin/env python3
"""bundle -> plan.h：运行器唯一的编译期输入。

只做渲染。输入是 *bundle* 的 ir.json / stimulus.bin，加上
backend/gpcp/lower.py 从 bundle IR + 目标几何算出的 plan；
不再读 build/proto/<场景>/plan-base.json（那是旧场景流水线的产物）。

plan.h 里出现硬件数字是允许的——它是生成物，来源是 cgra/target.yaml。
"""
import hashlib
import struct

_WHERE = {"control-core": "PROTO_WHERE_CPU",
          "array": "PROTO_WHERE_ARRAY",
          "dma": "PROTO_WHERE_DMA"}


def _cstr(s):
    return '"%s"' % (s or "")


def params_hash(ir_bytes, stim):
    """IR + 激励的指纹；运行器写进结果区 [8]，评估器用它挡旧二进制。

    入参必须是 bundle 里那份原始 ir.json 字节（不是重新序列化的 dict），
    否则哈希与运行器写入的值对不上。
    """
    return int(hashlib.sha256(ir_bytes + stim).hexdigest()[:8], 16)


def plan_h(scenario, ir, ir_bytes, stim, plan, target):
    budget = ir.get("budget") or {}
    slot_ms = float(budget.get("slot_ms", 0.0))
    clock_hz = int(target.get("clock_hz", 0))
    budget_cycles = (int(round(slot_ms * clock_hz / 1000.0))
                     if slot_ms and clock_hz else 0)
    stim_magic = struct.unpack("<I", stim[:4])[0] if len(stim) >= 4 else 0

    lines = [
        "/* 生成物：proto/emit.py 从 ir.json + plan-base.json 生成，勿手改。",
        " * 场景 %s（%s/%s），目标 %s（%dx%d = %d PE，%d bank x %d 字）。"
        % (scenario, ir.get("rat"), ir.get("direction"), target.get("source", "?"),
           target["rows"], target["cols"], target["n_pes"], target["banks"],
           target["words_per_bank"]),
        " * 改场景 / 换目标后重跑 `make proto PROTO=%s`。" % scenario,
        " */",
        "#ifndef PROTO_PLAN_H",
        "#define PROTO_PLAN_H",
        "",
        '#define PROTO_SCENARIO     "%s"' % scenario,
        "#define PROTO_PARAMS_HASH  0x%08xul" % params_hash(ir_bytes, stim),
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
        "    uint8_t     id;            /* 语义图节点号（docs/protocols/README.md 的顺序） */",
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
    return "\n".join(lines)
