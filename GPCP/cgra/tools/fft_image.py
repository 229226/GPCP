#!/usr/bin/env python3
"""kind: fft_image —— FFT 两张镜像的编译器专用调度器。

与 rule_map 里已有的 schedule_cn / schedule_bn_split 同类：radix-4 蝶形有
「累加器常驻 + 四点复用 + twiddle 保活」的固定寄存器约定，通用 DFG 调度器
把它重排后，RTL 的同步 ST / 组合 LD 时序下不再与既有镜像等价（Python ISA
仿真看不出这个差别，run-fft 的逐位比对能）。所以这里把该约定写进编译器：
所有指令字仍由 cgra.enc 产生、由 cgra/tools 统一输出，C 侧零手写微码。

自检是独立的标量黄金模型：按 radix-4 DIF 公式逐 (轮, PE) 算出每个输出的
re/im，与 rm.simulate 跑出的完整镜像逐地址对账（含原地写回的轮间串接）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from cgra import NPES, enc, spad_idx        # noqa: E402
import rule_map as rm                        # noqa: E402


def _s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def _sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def emit_round(mc, base, pe, m, j0, jstep, st0, mstep, tw):
    """一个 radix-4 蝶形输出，28 拍；显式寄存器约定与既有镜像一致。"""
    def put(c, op, a=0, b=0, dst=0, imm=0):
        mc.put(base + c, pe, op, a=a, b=b, dst=dst, imm=imm)

    put(0, "ld", dst=4, imm=j0)
    put(1, "ld", dst=5, imm=j0 + 1)
    put(2, "mov", 4, 0, 0)
    put(3, "mov", 5, 0, 1)
    for j in range(1, 4):
        w = j0 + j * jstep
        f = (j * m) & 3
        put(4 * j + 0, "ld", dst=4, imm=w)
        put(4 * j + 1, "ld", dst=5, imm=w + 1)
        if f == 0:                            # +v
            put(4 * j + 2, "add", 0, 4, 0)
            put(4 * j + 3, "add", 1, 5, 1)
        elif f == 1:                          # -i*v = (v_i, -v_r)
            put(4 * j + 2, "add", 0, 5, 0)
            put(4 * j + 3, "sub", 1, 4, 1)
        elif f == 2:                          # -v
            put(4 * j + 2, "sub", 0, 4, 0)
            put(4 * j + 3, "sub", 1, 5, 1)
        else:                                 # +i*v = (-v_i, v_r)
            put(4 * j + 2, "sub", 0, 5, 0)
            put(4 * j + 3, "add", 1, 4, 1)
    shift = 17 if tw >= 0 else 2
    if tw >= 0:
        put(16, "ld", dst=6, imm=tw)
        put(17, "ld", dst=7, imm=tw + 1)
        put(18, "mul", 0, 6, 2)
        put(19, "mul", 1, 7, 3)
        put(20, "sub", 2, 3, 2)
        put(21, "mul", 0, 7, 3)
        put(22, "mul", 1, 6, 4)
        put(23, "add", 3, 4, 3)
    else:
        put(22, "mov", 0, 0, 2)
        put(23, "mov", 1, 0, 3)
    put(24, "shr", 2, 0, 2, shift)
    put(25, "shr", 3, 0, 3, shift)
    put(26, "st", 2, 0, 0, st0 + m * mstep)
    put(27, "st", 3, 0, 0, st0 + m * mstep + 1)


def rounds(which):
    """产出 (r, pe, m, j0, jstep, st0, mstep, tw) 列表。"""
    out = []
    if which == "stage":
        for r in range(16):
            for pe in range(NPES):
                b, m = pe >> 2, pe & 3
                bi = 4 * r + b
                j0 = 2 * bi
                tw = (512 + (m - 1) * 128 + bi * 2) if m else -1
                out.append((r * 28, pe, m, j0, 128, j0, 128, tw))
    else:
        for r in range(4):                    # pass A: L=64, M=16
            for pe in range(NPES):
                b, m = pe >> 2, pe & 3
                t = 4 * r + b
                site = t * 2
                tw = (512 + (m - 1) * 32 + t * 2) if m else -1
                out.append((r * 28, pe, m, site, 32, site, 32, tw))
        for r in range(4):                    # pass B: L=16, M=4
            for pe in range(NPES):
                b, m = pe >> 2, pe & 3
                beta = 4 * r + b
                sb, t2 = beta >> 2, beta & 3
                site = (sb * 16 + t2) * 2
                tw = (512 + 96 + (m - 1) * 8 + t2 * 2) if m else -1
                out.append((112 + r * 28, pe, m, site, 8, site, 8, tw))
        for r in range(4):                    # pass C: L=4, M=1, 无 twiddle
            for pe in range(NPES):
                b, m = pe >> 2, pe & 3
                site = (4 * r + b) * 4 * 2
                out.append((224 + r * 28, pe, m, site, 2, site, 2, -1))
    return out


def build(which):
    mc = rm.Microcode()
    for base, pe, m, j0, jstep, st0, mstep, tw in rounds(which):
        emit_round(mc, base, pe, m, j0, jstep, st0, mstep, tw)
    mc.cycles = 448 if which == "stage" else 336
    return mc


def golden_round(spad, m, j0, jstep, st0, mstep, tw):
    """标量黄金模型：一个蝶形输出 -> (addr_re, re), (addr_im, im)。"""
    xs = []
    for j in range(4):
        a = j0 + j * jstep
        xs.append((_s32(spad.get(a, 0)), _s32(spad.get(a + 1, 0))))
    ar, ai = xs[0]
    for j in range(1, 4):
        xr, xi = xs[j]
        f = (j * m) & 3
        if f == 0:
            dr, di = xr, xi
        elif f == 1:
            dr, di = xi, -xr
        elif f == 2:
            dr, di = -xr, -xi
        else:
            dr, di = -xi, xr
        ar, ai = _s32(ar + dr), _s32(ai + di)
    if tw >= 0:
        wr, wi = _sx16(spad.get(tw, 0)), _sx16(spad.get(tw + 1, 0))
        pr = _sx16(ar) * wr
        pi = _sx16(ai) * wi
        re = _s32(_s32(pr - pi) >> 17)
        qr = _sx16(ar) * wi
        qi = _sx16(ai) * wr
        im = _s32(_s32(qr + qi) >> 17)
    else:
        re, im = _s32(_s32(ar) >> 2), _s32(_s32(ai) >> 2)
    dst = st0 + m * mstep
    return ((dst, re & 0xFFFFFFFF), (dst + 1, im & 0xFFFFFFFF))


def selfcheck(mc, which):
    """按轮并行快照（所有 LD 早于所有 ST）算 golden，与整镜像 simulate 对账。"""
    import random
    rnd = random.Random(20261108)
    base = {i: rnd.randint(-3000, 3000) & 0xFFFFFFFF for i in range(1024)}
    golden = dict(base)
    exp = {}
    by_at = {}
    for base_c, pe, m, j0, jstep, st0, mstep, tw in rounds(which):
        by_at.setdefault(base_c, []).append((m, j0, jstep, st0, mstep, tw))
    for at in sorted(by_at):
        snap = dict(golden)
        pending = []
        for m, j0, jstep, st0, mstep, tw in by_at[at]:
            pending += list(golden_round(snap, m, j0, jstep, st0, mstep, tw))
        for addr, v in pending:
            exp[addr] = v
            golden[addr] = v
    spad = dict(base)
    rm.simulate(mc, spad)
    errs = [(i, spad.get(i), w) for i, w in exp.items() if spad.get(i) != w]
    return errs, len(exp)


def _fmt(words):
    out = []
    for i, w in enumerate(words):
        out.append(f"0x{w:08x}u,")
        if (i + 1) % 8 == 0:
            out.append("\n")
    return "".join(out)


def write_header(path, sym, mc, which, nvals):
    with open(path, "w", newline="\n") as f:
        f.write("/* 由 cgra/tools/gen_kernel_prog.py 生成，勿手改。\n"
                f" * kind: fft_image —— 编译器专用蝶形调度器（fft_image.py），"
                f"{mc.cycles} 拍 × {NPES} PE。\n"
                f" * 自检：标量黄金模型逐轮对账 {nvals} 个 spad 值、0 差异；"
                f"硬件台架 run-fft / run-ssbfft 逐位比对。\n"
                f" * 布局：{which}，每轮 28 拍、16 轮 radix-4 DIF。 */\n")
        f.write(f"#ifndef {sym}_PROG_H\n#define {sym}_PROG_H\n\n")
        f.write(f"#define {sym}_PROG_CYCLES {mc.cycles}\n")
        f.write(f"#define {sym}_PROG_PES    {NPES}\n\n")
        f.write(f"static const unsigned int {sym.lower()}_prog[{mc.cycles} * {NPES}]\n"
                f"    __attribute__((aligned(8))) = {{\n")
        # 与既有镜像一致：空缺拍填 enc("nop", 0, 0, 0, 0)
        words = [enc(*mc.words.get((c, p), ("nop",)))
                 for c in range(mc.cycles) for p in range(NPES)]
        f.write(_fmt(words))
        f.write("\n};\n\n#endif\n")


def run(spec):
    total = 0
    for which, sym in (("stage", "FFT_STAGE"), ("block", "FFT_BLOCK")):
        mc = build(which)
        errs, nvals = selfcheck(mc, which)
        if errs:
            print(f"[fft_image/{which}] 自检失败 {len(errs)}/{nvals}: {errs[:3]}",
                  file=sys.stderr)
            return 1
        out = os.path.join(ROOT, "sw", "soc", "gen", f"{sym.lower()}_prog.h")
        write_header(out, sym, mc, which, nvals)
        total += nvals
        print(f"wrote {out}: {which} {mc.cycles} 拍 × {NPES} PE，"
              f"{mc.cycles * NPES} 字，黄金模型自检 {nvals} 值 0 差异")
    print(f"[fft_image] 两张镜像合计自检 {total} 值 0 差异")
    return 0
