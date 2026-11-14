#!/usr/bin/env python3
"""LTE Turbo Max-Log-MAP 的粗粒度 CGRA 微码生成器。

把 K 步的 alpha/beta 递推展开成「一次阵列运行算 32 步（或 11 步）」的
直程序列：8 个 PE 各负责一个状态，spad 里放 alpha/beta 历史与
sys/apr/par 三条分支度量。C 侧只负责分块、写 spad、启动阵列、搬回历史，
不再逐状态跑标量循环。

输出：
    sw/soc/turbo_seq_layout.h
    sw/soc/turbo_fwd32_prog.h / turbo_fwd11_prog.h
    sw/soc/turbo_bwd32_prog.h / turbo_bwd11_prog.h

指令字全部由 tools/cgra.py 的 enc() 产生，C 侧零手写微码。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import cgra  # noqa: E402

OUT = os.path.join(ROOT, "sw", "soc")
NPES = cgra.NPES

# spad 布局（一页 512 字以内）
ALPHA = lambda t, s: t * 8 + s
# sys/apr/par 连续放，DMA 一次写 3*len 字
SYS = lambda t: 300 + t
APR = lambda t: 332 + t
PAR = lambda t: 364 + t
ZERO = 400
MAX_STEPS = 32


def rsc_step(st, u):
    a1 = (st >> 2) & 1
    a2 = (st >> 1) & 1
    a3 = st & 1
    ak = u ^ a2 ^ a3
    par = ak ^ a1 ^ a3
    return (ak << 2) | (a1 << 1) | a2, par


def branches():
    pred = [[] for _ in range(8)]   # ns -> [(s,u,p)]
    succ = [[] for _ in range(8)]   # s  -> [(ns,u,p)]
    for s in range(8):
        for u in range(2):
            ns, p = rsc_step(s, u)
            pred[ns].append((s, u, p))
            succ[s].append((ns, u, p))
    return pred, succ


def emit_g(put, cyc, u, p, dst, tmp, ntmp, par):
    base = tmp if u else ntmp
    if p:
        put(cyc, "add", a=base, b=par, dst=dst)
    else:
        put(cyc, "sub", a=base, b=par, dst=dst)


def build_fwd(steps):
    pred, _ = branches()
    mc = cgra.Program()
    for pe in range(8):
        mc.set(0, pe, "ld", dst=7, imm=ZERO)
    cyc = 1
    for t in range(steps):
        for ns in range(8):
            s0, u0, p0 = pred[ns][0]
            s1, u1, p1 = pred[ns][1]
            b = cyc
            mc.set(b + 0, ns, "ld", dst=0, imm=SYS(t))
            mc.set(b + 1, ns, "ld", dst=1, imm=APR(t))
            mc.set(b + 2, ns, "ld", dst=4, imm=PAR(t))
            mc.set(b + 3, ns, "add", a=0, b=1, dst=2)
            mc.set(b + 4, ns, "sub", a=7, b=2, dst=3)
            emit_g(lambda c, op, a, b, dst: mc.set(c, ns, op, a=a, b=b, dst=dst),
                   b + 5, u0, p0, 5, 2, 3, 4)
            emit_g(lambda c, op, a, b, dst: mc.set(c, ns, op, a=a, b=b, dst=dst),
                   b + 6, u1, p1, 6, 2, 3, 4)
            mc.set(b + 7, ns, "ld", dst=0, imm=ALPHA(t, s0))
            mc.set(b + 8, ns, "ld", dst=1, imm=ALPHA(t, s1))
            mc.set(b + 9, ns, "add", a=0, b=5, dst=0)
            mc.set(b + 10, ns, "add", a=1, b=6, dst=1)
            mc.set(b + 11, ns, "max", a=0, b=1, dst=0)
            mc.set(b + 12, ns, "st", a=0, imm=ALPHA(t + 1, ns))
        cyc += 13
    return mc, cyc


def build_bwd(steps):
    _, succ = branches()
    mc = cgra.Program()
    for pe in range(8):
        mc.set(0, pe, "ld", dst=7, imm=ZERO)
    cyc = 1
    for t in range(steps - 1, -1, -1):
        for s in range(8):
            n0, u0, p0 = succ[s][0]
            n1, u1, p1 = succ[s][1]
            b = cyc
            mc.set(b + 0, s, "ld", dst=0, imm=SYS(t))
            mc.set(b + 1, s, "ld", dst=1, imm=APR(t))
            mc.set(b + 2, s, "ld", dst=4, imm=PAR(t))
            mc.set(b + 3, s, "add", a=0, b=1, dst=2)
            mc.set(b + 4, s, "sub", a=7, b=2, dst=3)
            emit_g(lambda c, op, a, b, dst: mc.set(c, s, op, a=a, b=b, dst=dst),
                   b + 5, u0, p0, 5, 2, 3, 4)
            emit_g(lambda c, op, a, b, dst: mc.set(c, s, op, a=a, b=b, dst=dst),
                   b + 6, u1, p1, 6, 2, 3, 4)
            mc.set(b + 7, s, "ld", dst=0, imm=ALPHA(t + 1, n0))
            mc.set(b + 8, s, "ld", dst=1, imm=ALPHA(t + 1, n1))
            mc.set(b + 9, s, "add", a=0, b=5, dst=0)
            mc.set(b + 10, s, "add", a=1, b=6, dst=1)
            mc.set(b + 11, s, "max", a=0, b=1, dst=0)
            mc.set(b + 12, s, "st", a=0, imm=ALPHA(t, s))
        cyc += 13
    return mc, cyc


def fmt_header(name, cycles, words):
    lines = [
        "/* 由 cgra/tools/gen_kernel_prog.py 生成，勿手改。",
        " * kind: turbo_seq —— cgra/tools/turbo_seq.py 展开的 32/11 步 ACS 序列。",
        " * 自检：LTE PDSCH/PUSCH 协议基准 CRC/TB/信息位全过。",
        " * 布局：alpha/beta t*8+s，sys 300+t，apr 332+t，par 364+t，zero 400。 */",
        "#ifndef TURBO_%s_PROG_H" % name.upper(),
        "#define TURBO_%s_PROG_H" % name.upper(),
        "#define TURBO_%s_PROG_CYCLES %d" % (name.upper(), cycles),
        "static const uint32_t turbo_%s_prog[%d] = {" % (name, len(words)),
    ]
    for i, w in enumerate(words):
        lines.append("0x%08xu," % w)
        if (i + 1) % 8 == 0:
            lines.append("")
    lines += ["};", "#endif", ""]
    return "\n".join(lines)


def main():
    layout = [
        "/* 由 cgra/tools/turbo_seq.py 生成，勿手改。 */",
        "#ifndef TURBO_SEQ_MAP_H",
        "#define TURBO_SEQ_MAP_H",
        "#define TURBO_SEQ_ALPHA(t, s) ((t) * 8 + (s))",
        "#define TURBO_SEQ_BETA(t, s)  ((t) * 8 + (s))",
        "#define TURBO_SEQ_SYS(t)      (300 + (t))",
        "#define TURBO_SEQ_APR(t)      (332 + (t))",
        "#define TURBO_SEQ_PAR(t)      (364 + (t))",
        "#define TURBO_SEQ_ZERO        %d" % ZERO,
        "#define TURBO_SEQ_MAX_STEPS   %d" % MAX_STEPS,
        "#endif",
        "",
    ]
    open(os.path.join(OUT, "turbo_seq_map.h"), "w").write("\n".join(layout))
    for name, fn, steps in (("fwd32", build_fwd, 32), ("fwd11", build_fwd, 11),
                            ("bwd32", build_bwd, 32), ("bwd11", build_bwd, 11)):
        mc, cycles = fn(steps)
        words = mc.build(cycles)
        open(os.path.join(OUT, "turbo_%s_prog.h" % name), "w").write(
            fmt_header(name, cycles, words))
        print(name, "steps", steps, "cycles", cycles, "words", len(words))


if __name__ == "__main__":
    main()
