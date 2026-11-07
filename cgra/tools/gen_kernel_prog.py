#!/usr/bin/env python3
"""统一微码生成入口（开发计划书 L1）。

    python3 cgra/tools/gen_kernel_prog.py --spec cgra/kernels/bn_24.yaml --out /tmp/bn.h
    python3 cgra/tools/gen_kernel_prog.py --all            # 扫 cgra/kernels/*.yaml
    python3 cgra/tools/gen_kernel_prog.py --from-cgra kernel_zc_corr   # 兼容 path-A

调度分发（见 docs/18）：
  kind: bn  -> rule_map.schedule_bn_split + check_bn_split  （与仓库 pdsch_bn_prog.h 逐字节一致）
  kind: cn  -> rule_map.schedule_cn        + check_cn       （与仓库 pdsch_cn_prog.h 逐字节一致）
  kind: dfg -> kernel_spec.build_dfg + rule_map.schedule + check_dfg（新 kernel 走这里）

铁律：不改 RTL；生成文件带「勿手改」头；自检不过 exit 1；复用 cgra.enc/OPS。
"""
import argparse
import glob
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import cgra as cgra_mod                       # noqa: E402
from cgra import NPES, enc, spad_idx         # noqa: E402
import rule_map as rm                         # noqa: E402
import kernel_spec as ks                      # noqa: E402
import bank_alloc as ba                       # noqa: E402  L2-A：区间图着色 bank 分配器


# ---------------------------------------------------------------- 字格式化
def _fmt_words(words):
    out = []
    for i, w in enumerate(words):
        out.append(f"0x{w:08x}u,")
        if (i + 1) % 8 == 0:
            out.append("\n")
    return "".join(out)


def _s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def _sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# ---------------------------------------------------------------- 头文件写入
def _write_bn(f, mc, nvals, rows):
    f.write("/* 由 cgra/tools/gen_bn_prog.py 生成，勿手改。\n"
            f" * 规则映射器（link 版）：每行 4 个 PE，{rows} 行 = {rows // 4} pass，"
            f"共 {mc.cycles} 拍 × {NPES} PE。\n"
            f" * 布局：bank k = 第 k 条消息（24 槽）、bank 24 = LLR、bank 25 = 硬判决，\n"
            f" *       word = 提升行号（0..{rows - 1}）。\n"
            f" * 自检：{nvals} 个 spad 值、0 差异（模型）；台架见 run-pdsch-bn1。 */\n")
    f.write("#ifndef PDSCH_BN_PROG_H\n#define PDSCH_BN_PROG_H\n\n")
    f.write(f"#define BN_PROG_CYCLES {mc.cycles}\n")
    f.write(f"#define BN_PROG_PES    {NPES}\n\n")
    f.write(f"static const unsigned int bn_prog[{mc.cycles} * {NPES}]\n"
            f"    __attribute__((aligned(8))) = {{\n")
    f.write(_fmt_words(mc.build()))
    f.write("\n};\n\n#endif\n")


def _write_cn(f, mc, nvals):
    f.write("/* 由 cgra/tools/gen_cn_prog.py 生成，勿手改。\n"
            f" * 规则映射器：每行 1 个 PE、20 个提升行一次跑完（16 + 4），"
            f"共 {mc.cycles} 拍 × {NPES} PE。\n"
            f" * 布局：bank 0..9 = 10 条边（不足由 bank {rm.CN_SENTINEL_BANK} 的 +32767 补齐）、\n"
            f" *       bank 10/11/12 = min1/min2/sign，word = 提升行号。\n"
            f" * 自检：{nvals} 个 spad 值、0 差异（模型）；台架见 run-pdsch-bg2。 */\n")
    f.write("#ifndef PDSCH_CN_PROG_H\n#define PDSCH_CN_PROG_H\n\n")
    f.write(f"#define CN_PROG_CYCLES {mc.cycles}\n")
    f.write(f"#define CN_PROG_PES    {NPES}\n\n")
    f.write(f"static const unsigned int cn_prog[{mc.cycles} * {NPES}]\n"
            f"    __attribute__((aligned(8))) = {{\n")
    f.write(_fmt_words(mc.build()))
    f.write("\n};\n\n#endif\n")


def _sym(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).upper()


def _write_dfg(f, spec, mc, nvals, nops, nleaves, nsinks, instances,
               leaf_banks=None, sink_banks=None):
    sym = _sym(spec.get("name", "kernel"))
    leaf_banks = leaf_banks or {}
    sink_banks = sink_banks or {}
    f.write("/* 由 cgra/tools/gen_kernel_prog.py 生成，勿手改。\n"
            f" * 通用 DFG 调度（rule_map.schedule）：{nops} 算子 / {nleaves} 叶子 / {nsinks} 汇，\n"
            f" * {instances} 个实例各占 1 PE，共 {mc.cycles} 拍 × {NPES} PE。\n"
            f" * 自检：{nvals} 个汇值、0 差异（模型）。\n"
            f" * 布局：每实例 1 个 spad word（word=实例号），叶子/汇 bank 见下方 BANK_*。 */\n")
    f.write(f"#ifndef {sym}_PROG_H\n#define {sym}_PROG_H\n\n")
    f.write(f"#define {sym}_PROG_CYCLES {mc.cycles}\n")
    f.write(f"#define {sym}_PROG_PES    {NPES}\n")
    # 叶子/汇 → spad bank，供 C 基准按 bank 布局组织 DMA 缓冲
    for n in sorted(leaf_banks, key=lambda k: leaf_banks[k]):
        f.write(f"#define {sym}_BANK_{_sym(n)} {leaf_banks[n]}\n")
    for n in sorted(sink_banks, key=lambda k: sink_banks[k]):
        f.write(f"#define {sym}_BANK_{_sym(n)} {sink_banks[n]}\n")
    all_banks = list(leaf_banks.values()) + list(sink_banks.values())
    if all_banks:
        f.write(f"#define {sym}_BANK_LO {min(all_banks)}\n")
        f.write(f"#define {sym}_BANK_HI {max(all_banks)}\n")
    f.write("\n")
    f.write(f"static const unsigned int {sym.lower()}_prog[{mc.cycles} * {NPES}]\n"
            f"    __attribute__((aligned(8))) = {{\n")
    f.write(_fmt_words(mc.build()))
    f.write("\n};\n\n#endif\n")


# ---------------------------------------------------------------- 通用 DFG 自检
def check_dfg(mc, dfg, banks, sink_addrs, instances):
    """随机叶子值 → 所有汇写回值与黄金模型逐一对账（镜像 rule_map.main 的通用模式）。"""
    rnd = random.Random(7)
    spad, exp = {}, {}
    for inst in range(min(instances, 16)):
        vals = {}
        for l in dfg.leaves():
            v = rnd.randint(-5000, 5000)
            vals[l] = _s32(v)
            spad[spad_idx(banks[l], inst)] = v & 0xFFFFFFFF
        for n in dfg.topo():
            if not dfg.preds(n):
                continue
            op = dfg.opcode[n]
            srcs = [vals[s] for s, _o in sorted(dfg.preds(n), key=lambda t: t[1])]
            x, y = srcs[0], (srcs[1] if len(srcs) > 1 else 0)
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
                _imm = dfg.imm.get(n, 0)        # 与 rule_map.schedule 一致：用节点 imm
                vals[n] = _s32(_s32(x) >> _imm)
            elif op == "shl":
                _imm = dfg.imm.get(n, 0)
                vals[n] = _s32(x << _imm)
            elif op == "max":
                vals[n] = x if _s32(x) > _s32(y) else y
            elif op == "min":
                vals[n] = x if _s32(x) < _s32(y) else y
            else:
                raise SystemExit(f"通用自检不支持 op {op}")
            if not dfg.succs(n):
                exp[spad_idx(sink_addrs[n], inst)] = vals[n] & 0xFFFFFFFF
    rm.simulate(mc, spad)
    errs = [(i, spad.get(i), w) for i, w in exp.items() if spad.get(i) != w]
    return errs, len(exp)


# ---------------------------------------------------------------- 单 spec 生成
def run_spec(spec_path, out=None, rows=None):
    spec = ks.load_spec(spec_path)
    if rows is not None and spec.get("kind") == "bn":
        spec["rows"] = rows
    errs = ks.validate(spec)
    if errs:
        print(f"[{spec_path}] 校验失败：", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1

    kind = spec.get("kind", "dfg")
    out = out or ks.resolve_out(spec, spec_path, ROOT)

    if kind == "bn":
        rows = spec.get("rows", 20)
        mc = rm.schedule_bn_split(rows=rows)
        e, nvals = rm.check_bn_split(mc, rows=rows)
        if e:
            print(f"[{spec_path}] 自检失败：{len(e)}/{nvals} 个值不同，拒绝生成", file=sys.stderr)
            return 1
        with open(out, "w", newline="\n") as f:
            _write_bn(f, mc, nvals, rows)
        print(f"wrote {out}: {mc.cycles} 拍 × {NPES} PE，{len(mc.build())} 字，"
              f"自检 {nvals} 值 0 差异")
        return 0

    if kind == "cn":
        mc = rm.schedule_cn()
        e, nvals = rm.check_cn(mc)
        if e:
            print(f"[{spec_path}] 自检失败：{len(e)}/{nvals} 个值不同，拒绝生成", file=sys.stderr)
            return 1
        with open(out, "w", newline="\n") as f:
            _write_cn(f, mc, nvals)
        print(f"wrote {out}: {mc.cycles} 拍 × {NPES} PE，{len(mc.build())} 字，"
              f"自检 {nvals} 值 0 差异")
        return 0

    # dfg
    dfg = ks.build_dfg(spec)
    instances = spec.get("instances", 16)
    # ---- L2-A：两阶段 bank 分配（淘汰 leaf_banks/sink_bank 硬编码的 6 上限）----
    plan_leaves, plan_sinks = _plan_assign(dfg)
    mc0, _lv, _sa, _b, liv, max_regs = rm.schedule(
        dfg, instances, banks=plan_leaves, sink_addrs=plan_sinks,
        return_liveness=True)
    end = mc0.cycles - 1
    # 叶子常驻 [0, end]（输入单次写入）；汇常驻 [st, end]（写回后由校验器读）。
    # 二者在着色意义下两两重叠 -> 强制分配不同 bank，匹配 spad 单次初始化模型。
    liv_alloc = {}
    for n in dfg.leaves():
        liv_alloc[n] = (0, end)
    for n in dfg.sinks():
        liv_alloc[n] = (liv.get(n, (0, end))[0], end)
    # hints = YAML 里显式写的 bank（强制绑定，复现固定布局）
    hints = {}
    for fld in ("leaves", "sinks"):
        for name, bank in ks.expand_mapping(spec.get(fld, {})).items():
            if bank is not None:
                hints[name] = bank
    alloc = ba.alloc_banks(liv_alloc, hints=hints)
    leaf_banks_final = {n: b for n, b in alloc.items() if n in set(dfg.leaves())}
    sink_banks_final = {n: b for n, b in alloc.items() if n in set(dfg.sinks())}
    # ---- 第二阶段：用分配好的 bank 发射 ----
    mc, lv, sink_addrs, banks = rm.schedule(
        dfg, instances, banks=leaf_banks_final, sink_addrs=sink_banks_final)
    # ---- L2-D：资源断言（铁律：不过就 exit 1）----
    _assert_resources(mc, leaf_banks_final, sink_banks_final, max_regs)
    e, nvals = check_dfg(mc, dfg, banks, sink_addrs, instances)
    if e:
        print(f"[{spec_path}] 自检失败：{len(e)}/{nvals} 个值不同，拒绝生成", file=sys.stderr)
        return 1
    with open(out, "w", newline="\n") as f:
        # 算子数 = 总节点 - 叶子（叶子由消费者现场 LD，不算独立发射的算子）
        _write_dfg(f, spec, mc, nvals, len(dfg.order) - len(dfg.leaves()),
                   len(dfg.leaves()), len(dfg.sinks()), instances,
                   leaf_banks=leaf_banks_final, sink_banks=sink_banks_final)
    print(f"wrote {out}: {mc.cycles} 拍 × {NPES} PE，{len(mc.build())} 字，"
          f"自检 {nvals} 值 0 差异")
    return 0


# ---------------------------------------------------------------- L2 辅助：规划期 bank / 资源断言
def _plan_assign(dfg):
    """规划期临时 bank 分配：叶子与汇各给唯一 bank（不触发 6 上限），仅用于算生命区间。"""
    leaves = {n: i for i, n in enumerate(dfg.leaves())}
    sinks = {n: 100 + i for i, n in enumerate(dfg.sinks())}
    return leaves, sinks


def _assert_resources(mc, leaf_banks, sink_banks, max_regs):
    """L2-D：生成前硬断言，不过就 SystemExit。"""
    if mc.cycles > 512:
        raise SystemExit(f"微码 {mc.cycles} 拍超过 IMEM 上限 512（≈8192 字 / 16 PE）")
    maxbank = 0
    for b in list(leaf_banks.values()) + list(sink_banks.values()):
        maxbank = max(maxbank, b)
    if maxbank > 31:
        raise SystemExit(f"bank {maxbank} 越界（spad_idx 只保留 5 位，必须 <=31）")
    if max_regs > 8:
        raise SystemExit(f"单 PE 活跃寄存器 {max_regs} 超过 8 个（r0..r7）")
    return


def run_from_cgra(func, out=None):
    """兼容 path-A：调用 cgra.py 里的 kernel_* 函数，写通用头（L1 不强制迁移 prach）。"""
    if not hasattr(cgra_mod, func):
        print(f"cgra 没有函数 {func}", file=sys.stderr)
        return 1
    k = getattr(cgra_mod, func)()
    words = getattr(k, "words", None)
    cycles = getattr(k, "cycles", None)
    if words is None or cycles is None or len(words) != cycles * NPES:
        print(f"{func} 不是预期的 Kernel（words/cycles 不匹配），跳过", file=sys.stderr)
        return 1
    out = out or os.path.join(ROOT, "sw", "soc", f"{func}_prog.h")
    sym = _sym(func)
    with open(out, "w", newline="\n") as f:
        f.write(f"/* 由 cgra/tools/gen_kernel_prog.py --from-cgra {func} 生成，勿手改。 */\n")
        f.write(f"#ifndef {sym}_PROG_H\n#define {sym}_PROG_H\n\n")
        f.write(f"#define {sym}_PROG_CYCLES {cycles}\n")
        f.write(f"#define {sym}_PROG_PES    {NPES}\n\n")
        f.write(f"static const unsigned int {sym.lower()}_prog[{cycles} * {NPES}]\n"
                f"    __attribute__((aligned(8))) = {{\n")
        f.write(_fmt_words(words))
        f.write("\n};\n\n#endif\n")
    print(f"wrote {out}: {cycles} 拍 × {NPES} PE，{len(words)} 字（path-A 兼容）")
    return 0


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", help="单个 YAML 配置")
    ap.add_argument("--out", help="输出头文件路径（覆盖 spec 里的 out）")
    ap.add_argument("--all", action="store_true", help="扫 cgra/kernels/*.yaml")
    ap.add_argument("--from-cgra", metavar="FUNC",
                    help="兼容 path-A：调用 cgra.<FUNC>() 生成")
    a = ap.parse_args()

    if a.from_cgra:
        return run_from_cgra(a.from_cgra, a.out)

    if a.all:
        # 规范布局：<repo>/cgra/kernels/*.yaml；扁平 checkout：与本脚本同目录的 *.yaml
        specs = sorted(glob.glob(os.path.join(ROOT, "cgra", "kernels", "*.yaml")))
        if not specs:
            specs = sorted(glob.glob(os.path.join(HERE, "*.yaml")))
        if not specs:
            print("cgra/kernels/ 与本脚本目录下都没有 YAML", file=sys.stderr)
            return 1
        rc = 0
        for s in specs:
            r = run_spec(s)
            rc = rc or r
        return rc

    if a.spec:
        return run_spec(a.spec, a.out)

    ap.error("需要 --spec / --all / --from-cgra")
    return 2


if __name__ == "__main__":
    sys.exit(main())
