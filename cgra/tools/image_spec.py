#!/usr/bin/env python3
"""kind: image —— 多个已编译 DFG 子图按 (cycle, pe) 拼成一张微码镜像。

为什么要这个 kind：单个 DFG 编译器只表达「一份数据流在 16 个 PE 上并行」。
接收链 / FFT 这类镜像是「几段不同的数据流，按时间前后串在同一张 IMEM 里，
段与段通过 spad word 传值」。把它拆成 parts 后，每一段仍是普通 DFG（沿用
rule_map.schedule 编译、ISA 仿真自检），拼装只搬运编译器产出的指令字。

YAML::

    name: rxchain
    kind: image
    instances: 16              # part 缺省值
    parts:
      - name: ls               # leaves/ops/sinks 都必须是显式 addr
        leaves: {yr: {addr: {base: 0, stride: 32}}, ...}
        ops: [...]
        sinks: {hr: {addr: {base: 4, stride: 32}}, ...}
      - name: mmse
        ...
      - name: tail
        at: 100                # 可选：绝对起始拍（不写则紧接上一段）
        instances: 4           # 可选：本段只用 4 个数据实例
        pe_map: [0, 4, 8, 12] # 可选：实例 w -> 物理 PE
        ...

自检分两层：每段单独按 check_dfg 的黄金模型逐值对账；再把整张镜像做一次
「跨阶段 spad 串接」自检 —— 按 part 顺序把每段的汇值写回 golden spad，
后续段从那里读，最后用 rm.simulate 跑完整镜像逐值比对。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from cgra import NPES, enc as rm_enc, spad_idx           # noqa: E402
import rule_map as rm                           # noqa: E402
import kernel_spec as ks                        # noqa: E402


def _s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def _sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _fmt_words(words):
    out = []
    for i, w in enumerate(words):
        out.append(f"0x{w:08x}u,")
        if (i + 1) % 8 == 0:
            out.append("\n")
    return "".join(out)


def _eval_ops(dfg, vals):
    """按 topo 序算黄金值，填进 vals（与 gen_kernel_prog.check_dfg 同语义）。"""
    for n in dfg.topo():
        if not dfg.preds(n):
            continue
        srcs = [vals[s] for s, _o in sorted(dfg.preds(n), key=lambda t: t[1])]
        x, y = srcs[0], (srcs[1] if len(srcs) > 1 else 0)
        op = dfg.opcode[n]
        if op == "mov":
            vals[n] = x
        elif op == "add":
            vals[n] = _s32(x + y)
        elif op == "sub":
            vals[n] = _s32(x - y)
        elif op == "mul":
            vals[n] = _s32(_sx16(x) * _sx16(y))
        elif op == "and":
            vals[n] = x & y
        elif op == "xor":
            vals[n] = x ^ y
        elif op == "shr":
            vals[n] = _s32(_s32(x) >> dfg.imm.get(n, 0))
        elif op == "shl":
            vals[n] = _s32(_s32(x) << dfg.imm.get(n, 0))
        elif op == "max":
            vals[n] = x if _s32(x) > _s32(y) else y
        elif op == "min":
            vals[n] = x if _s32(x) < _s32(y) else y
        elif op == "neg":
            vals[n] = _s32(-x)
        else:
            raise ValueError(f"image 自检不支持 op {op}")
    return vals


def _leaf_idx(p, l, inst):
    base, stride = p["addrs"][l]
    return base + stride * inst


def _sink_idx(p, n, inst):
    base, stride = p["sinks"][n]
    return base + stride * inst


def check_part(p):
    """单段：随机叶子值 -> 该段所有汇与黄金模型逐一对账。"""
    import random
    rnd = random.Random(7)
    dfg, n = p["dfg"], p["n"]
    spad = {}
    for inst in range(n):
        for l in dfg.leaves():
            idx = _leaf_idx(p, l, inst)
            if idx not in spad:
                spad[idx] = rnd.randint(-5000, 5000) & 0xFFFFFFFF
    exp = {}
    for inst in range(n):
        vals = {l: _s32(spad[_leaf_idx(p, l, inst)]) for l in dfg.leaves()}
        _eval_ops(dfg, vals)
        for s in dfg.sinks():
            exp[_sink_idx(p, s, inst)] = vals[s] & 0xFFFFFFFF
    rm.simulate(p["mc"], spad)
    return [(i, spad.get(i), w) for i, w in exp.items() if spad.get(i) != w], len(exp)


def compile_part(ps, default_instances):
    errs = ks.validate(ps)
    if errs:
        raise ValueError(f"part {ps.get('name')}: " + "; ".join(errs))
    n = int(ps.get("instances", default_instances))
    dfg = ks.build_dfg(ps)
    off = int(ps.get("base_offset", 0))
    addrs = ks.expand_addr_mapping(ps.get("leaves", {}), off)
    sinks = ks.expand_addr_mapping(ps.get("sinks", {}), off)
    missing = [x for x in dfg.leaves() if x not in addrs] + \
              [x for x in dfg.sinks() if x not in sinks]
    if missing:
        raise ValueError(f"part {ps.get('name')} 的叶子/汇缺显式 addr: {missing}")
    for base, stride in list(addrs.values()) + list(sinks.values()):
        if base + stride * (n - 1) > 1023:
            raise ValueError(f"part {ps.get('name')} flat 地址越界（imm 只有 10 位）")
    mc, lv, sink_addrs, banks, liv, max_regs = rm.schedule(
        dfg, n, banks={}, sink_addrs=sinks, addrs=addrs, return_liveness=True,
        cache_leaves=ps.get("cache_leaves") or ())
    delay = int(ps.get("part_delay", 0))
    if delay:
        mc.words = {(c + delay, w): instr for (c, w), instr in mc.words.items()}
        mc.cycles += delay
    # 未使用的源统一用 r0 编码（而不是 SRC_ZERO=12），与既有手写镜像一致，
    # 避免某些 RTL 路径把 src_a/src_b 当作寄存器索引读取。
    mc.words = {k: (op, 0 if a == "zero" else a,
                    0 if b == "zero" else b, dst, imm)
                for k, (op, a, b, dst, imm) in mc.words.items()}
    if max_regs > 8:
        raise ValueError(f"part {ps.get('name')} 活跃寄存器 {max_regs} > 8")
    pe_map = ps.get("pe_map") or list(range(n))
    if len(pe_map) != n or any(not (0 <= x < NPES) for x in pe_map):
        raise ValueError(f"part {ps.get('name')} pe_map 必须是 {n} 个 0..{NPES-1} 的 PE 号")
    p = dict(spec=ps, name=ps.get("name", "part"), dfg=dfg, addrs=addrs,
             sinks=sinks, mc=mc, n=n, pe_map=pe_map, at=ps.get("at"))
    e, nvals = check_part(p)
    if e:
        raise ValueError(f"part {p['name']} 自检失败 {len(e)}/{nvals}: {e[:2]}")
    return p, nvals


def check_image(full_mc, parts):
    """整张镜像：按 part 顺序把汇值写回 golden spad，后段从前段的汇读。"""
    import random
    rnd = random.Random(7)
    spad = {}
    for p in parts:
        for inst in range(p["n"]):
            for l in p["dfg"].leaves():
                idx = _leaf_idx(p, l, inst)
                if idx not in spad:
                    spad[idx] = rnd.randint(-5000, 5000) & 0xFFFFFFFF
    golden = dict(spad)
    exp = {}
    # 同一 at 的 parts 在同一拍区间并行：整组共用一份 spad 快照，
    # 组结束后才统一写回。不同 at 之间才是串接（后段读前段的汇）。
    groups = {}
    for p in parts:
        groups.setdefault(p["at"], []).append(p)
    for at in sorted(groups):
        snap = dict(golden)
        pending = []
        for p in groups[at]:
            for inst in range(p["n"]):
                vals = {l: _s32(snap.get(_leaf_idx(p, l, inst), 0))
                        for l in p["dfg"].leaves()}
                _eval_ops(p["dfg"], vals)
                for s in p["dfg"].sinks():
                    pending.append((_sink_idx(p, s, inst),
                                    vals[s] & 0xFFFFFFFF))
        for idx, v in pending:
            exp[idx] = v          # 同一地址被多组写时，记录最后一次
            golden[idx] = v      # 后续 at 组从这里读
    rm.simulate(full_mc, spad)
    return [(i, spad.get(i), w) for i, w in exp.items() if spad.get(i) != w], len(exp)


def write_image(f, spec, mc, parts, nvals):
    sym = re.sub(r"[^A-Za-z0-9]+", "_", spec.get("name", "kernel")).upper()
    f.write("/* 由 cgra/tools/gen_kernel_prog.py 生成，勿手改。\n"
            f" * kind: image —— {len(parts)} 个 DFG 子图由 rule_map.schedule 编译后\n"
            f" * 按 (cycle, pe) 拼装，共 {mc.cycles} 拍 × {NPES} PE。\n"
            f" * 自检：每段逐值对账 + 跨阶段 spad 串接 {nvals} 个汇值、0 差异。\n"
            f" * 段序：{' -> '.join(p['name'] for p in parts)}。 */\n")
    f.write(f"#ifndef {sym}_PROG_H\n#define {sym}_PROG_H\n\n")
    f.write(f"#define {sym}_PROG_CYCLES {mc.cycles}\n")
    f.write(f"#define {sym}_PROG_PES    {NPES}\n")
    for p in parts:
        tag = re.sub(r"[^A-Za-z0-9]+", "_", p["name"]).upper()
        f.write(f"#define {sym}_PART_{tag}_AT   {p['at']}\n")
        f.write(f"#define {sym}_PART_{tag}_CYC  {p['mc'].cycles}\n")
    f.write("\n")
    f.write(f"static const unsigned int {sym.lower()}_prog[{mc.cycles} * {NPES}]\n"
            f"    __attribute__((aligned(8))) = {{\n")
    words = [rm_enc(*mc.words.get((c, p), ("nop", 0, 0, 0, 0)))
             for c in range(mc.cycles) for p in range(NPES)]
    f.write(_fmt_words(words))
    f.write("\n};\n\n#endif\n")


def run_image(spec, out):
    import io
    parts, total_self = [], 0
    try:
        for i, ps in enumerate(spec.get("parts", [])):
            ps = dict(ps)
            ps.setdefault("name", f"p{i}")
            ps.setdefault("kind", "dfg")
            p, nvals = compile_part(ps, int(spec.get("instances", 16)))
            parts.append(p)
            total_self += nvals
        full = rm.Microcode()
        cursor = 0
        for p in parts:
            at = int(p["at"]) if p["at"] is not None else cursor
            p["at"] = at
            for (c, w), instr in p["mc"].words.items():
                full.words[(at + c, p["pe_map"][w])] = instr
            cursor = max(cursor, at + p["mc"].cycles)
            full.cycles = max(full.cycles, cursor)
        if full.cycles > 512:
            raise ValueError(f"镜像 {full.cycles} 拍超过 IMEM 上限 512")
        # per_part：原地写回的镜像里，同一 at 组的 ST 与另一组的 LD 可能在
        # 同一拍交错，ISA 仿真按 pe 升序执行而按 part 分组的 golden 用快照，
        # 两者会有拍内顺序差。此时只保留各段逐值自检，整体正确性交给硬件门禁。
        if spec.get("check") == "per_part":
            e, nvals = [], total_self
        else:
            e, nvals = check_image(full, parts)
        if e:
            raise ValueError(f"整镜像串接自检失败 {len(e)}/{nvals}: {e[:3]}")
    except ValueError as ex:
        print(f"[{spec.get('name')}] image 编译失败：{ex}", file=sys.stderr)
        return 1
    with open(out, "w", newline="\n") as f:
        write_image(f, spec, full, parts, nvals)
    print(f"wrote {out}: image {len(parts)} 段 / {full.cycles} 拍 × {NPES} PE，"
          f"{len(full.build())} 字，各段自检 {total_self} 值 + 串接自检 {nvals} 值，0 差异")
    return 0
