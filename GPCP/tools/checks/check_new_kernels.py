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
"""新增 kernel 的独立黄金模型对账（docs/protocols/README.md / AGENTS §4.18 的教训）。

编译器自带的 check_dfg 只证明「微码 == YAML 的 DFG 语义」；如果 YAML 里的
公式本身写错，两边一起错、照样 0 差异（PRACH 那次教训）。本脚本**不复用
DFG 语义**，而是：
  1. 读编译器生成的 *_prog.h（真实产物，非重建的 Microcode）；
  2. 按「算法规格」独立算期望：QAM 用 38.211 的 LEVEL_SCALE/MAGNITUDE 表、
     LDPC 用 GF(2) 异或、Turbo 用 8 状态 Max-Log-MAP 递推、WiFi 用 min-sum；
  3. 把激励写进 spad，用 cgra.py 的 ISA 仿真器跑微码，逐值比对。

任何一步不一致就 exit 1。
"""
import os
import re
import sys
import random

ROOT = _ROOT
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "cgra", "tools"))

import kernel_spec as ks          # noqa: E402
import rule_map as rm             # noqa: E402
import target as hw               # noqa: E402

T = hw.load()
W = T.words
NPES = T.npes


def _s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def _sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# ---------------------------------------------------------------- 读生成物
def load_prog(name):
    """从 sw/soc/gen/<name>_prog.h 读回 (cycles, words)，并读 layout 宏拿到叶子/汇地址。"""
    p = os.path.join(ROOT, "sw", "soc", "gen", name + "_prog.h")
    if not os.path.exists(p):
        raise SystemExit("缺少生成物 %s（先跑 make kernels）" % p)
    s = open(p, encoding="utf-8").read()
    m = re.search(r"#define\s+\w+_PROG_CYCLES\s+(\d+)", s)
    if not m:
        raise SystemExit("%s 里找不到 *_PROG_CYCLES" % p)
    cycles = int(m.group(1))
    body = re.search(r"=\s*\{(.*?)\}\s*;", s, re.S)
    if not body:
        raise SystemExit("%s 里找不到指令字数组" % p)
    words = [int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{8})u?", body.group(1))]
    if len(words) != cycles * NPES:
        raise SystemExit("%s 指令字数 %d != %d×%d" % (p, len(words), cycles, NPES))
    return cycles, words


def load_layout(name):
    """从生成的 *_layout.h 读 SPAD_LEAF_*/SPAD_SINK_*/ADDR_* 的 (base,stride)。"""
    p = os.path.join(ROOT, "sw", "soc", "gen", name + "_layout.h")
    if not os.path.exists(p):
        raise SystemExit("缺少布局契约 %s" % p)
    s = open(p, encoding="utf-8").read()
    out = {}
    for kind, key, base, stride in re.findall(
            r"#define\s+\w+_SPAD_(LEAF|SINK)_([A-Z0-9_]+)_BASE\s+(\d+)u?"
            r"[\s\S]*?#define\s+\w+_SPAD_\1_\2_STRIDE\s+(\d+)u?", s):
        out[(kind, key.lower())] = (int(base), int(stride))
    for kind, key, base, stride in re.findall(
            r"#define\s+\w+_ADDR_([A-Z0-9_]+)_BASE\s+(\d+)u?"
            r"[\s\S]*?#define\s+\w+_ADDR_\1_STRIDE\s+(\d+)u?", s):
        out[("SINK", key.lower())] = (int(base), int(stride))
    return out


def decode(word):
    """cgra.enc 的逆：32 位指令字 -> (op, a, b, dst, imm)。"""
    op = (word >> 27) & 0x1F
    a = (word >> 23) & 0xF
    b = (word >> 19) & 0xF
    dst = (word >> 16) & 0x7
    imm = word & 0xFFFF
    return op, a, b, dst, imm


def run_isa(words, cycles, spad):
    """按 ISA 语义逐拍执行生成的指令字（与 cgra 的 RTL/仿真口径一致）。"""
    inv_ops = {v: k for k, v in T.ops.items()}
    inv_src = {v: k for k, v in T.src.items()}
    regs = [[0] * T.regs for _ in range(NPES)]
    for cyc in range(cycles):
        link = [0] * NPES
        nxt = list(link)
        for pe in range(NPES):
            w = words[cyc * NPES + pe]
            opn, a, b, dst, imm = decode(w)
            op = inv_ops.get(opn)
            if op in (None, "nop"):
                continue
            # 源编码 0..7 = 通用寄存器，8..15 = 特殊源（n/e/s/w/zero/imm/...）
            sa, sb = (a if a < 8 else inv_src.get(a)), (b if b < 8 else inv_src.get(b))

            def val(s):
                if isinstance(s, int):
                    return _s32(regs[pe][s])
                if s == "zero":
                    return 0
                if s == "imm":
                    return imm
                if s in ("link", "n", "e", "s", "w", "acc"):
                    return _s32(link[pe])
                raise SystemExit("未知源 %r" % s)

            if op == "st":
                spad[imm] = regs[pe][a] & 0xFFFFFFFF
                continue
            if op == "ld":
                v = _s32(spad.get(imm, 0))
            else:
                x, y = val(sa), val(sb)
                if op == "mov": v = x
                elif op == "add": v = _s32(x + y)
                elif op == "sub": v = _s32(x - y)
                elif op == "mul": v = _s32(_sx16(x) * _sx16(y))
                elif op == "and": v = x & y
                elif op == "xor": v = _s32(x ^ y)
                elif op == "shr": v = _s32(_s32(x) >> (imm & 31))
                elif op == "shl": v = _s32(x << (imm & 31))
                elif op == "max": v = x if x > y else y
                elif op == "min": v = x if x < y else y
                else: raise SystemExit("不支持的 op %r" % op)
            regs[pe][dst] = v & 0xFFFFFFFF
            nxt[pe] = regs[pe][dst]
        link = nxt
    return spad


def put(spad, layout, kind, key, inst, value):
    e = layout.get((kind, key))
    if e is None:
        raise SystemExit("布局契约里没有 %s/%s" % (kind, key))
    spad[e[0] + e[1] * inst] = value & 0xFFFFFFFF


def get(spad, layout, key, inst):
    e = layout.get(("SINK", key))
    if e is None:
        raise SystemExit("布局契约里没有 SINK/%s" % key)
    return _s32(spad.get(e[0] + e[1] * inst, 0))


def check_case(name, spec_name, feed, golden):
    """feed(spad, layout, inst) 写激励；golden(inst) -> {sink: 期望值}。"""
    cycles, words = load_prog(name)
    layout = load_layout(name)
    spad = {}
    for inst in range(NPES):
        feed(spad, layout, inst)
    run_isa(words, cycles, spad)
    bad = []
    for inst in range(NPES):
        for key, want in golden(inst).items():
            got = get(spad, layout, key, inst)
            if got != want:
                bad.append((inst, key, got, want))
    if bad:
        print("[FAIL] %-14s %d/%d 个值不一致，首 4: %s"
              % (name, len(bad), NPES * 3, bad[:4]), file=sys.stderr)
        return 1
    print("[ OK ] %-14s %d 拍 × %d PE，独立黄金逐值一致（%d PE × 汇）"
          % (name, cycles, NPES, NPES))
    return 0


# ---------------------------------------------------------------- 独立黄金模型
# 38.211 7.3.1.2：平均功率归一化的电平尺度（与 tools/models/nr_qam.py 同一张表，
# 但这里按标准公式独立推导幅度，不调用 qam_map，避免「拿被测对象当参照」）。
LEVEL_SCALE = {2: 2000, 4: 894, 6: 436}
MAGNITUDE = {
    1: {(): 1},
    2: {(0,): 3, (1,): 1},
    3: {(0, 0): 1, (0, 1): 3, (1, 1): 5, (1, 0): 7},
}


def branch_level(bits):
    """38.211 Gray 映射：b0 是符号位，其余位是 Gray 编码的幅度。"""
    n = len(bits)
    sign = 1 if bits[0] == 0 else -1
    return sign * MAGNITUDE[n][tuple(bits[1:])]


def make_qam(qm, spec_name):
    n = qm // 2
    amp = LEVEL_SCALE[qm]
    rnd = random.Random(0xC0FFEE + qm)
    cases = []
    for inst in range(NPES):
        bits = [rnd.randint(0, 1) for _ in range(qm)]
        ib, qb = bits[:n], bits[n:]
        cases.append((bits, ib, qb))

    def feed(spad, layout, inst):
        bits, ib, qb = cases[inst]
        for i in range(n):
            put(spad, layout, "LEAF", "b%d" % i, inst, ib[i])
            put(spad, layout, "LEAF", "q%d" % i, inst, qb[i])
        put(spad, layout, "LEAF", "one", inst, 1)
        if n == 2:                      # 只有 16QAM 用 three 常数
            put(spad, layout, "LEAF", "three", inst, 3)
        put(spad, layout, "LEAF", "amp", inst, amp)

    def golden(inst):
        _bits, ib, qb = cases[inst]
        return {"ilev": branch_level(ib) * amp,
                "qlev": branch_level(qb) * amp}

    return feed, golden


def make_ldpc_acc():
    rnd = random.Random(0x1D9C)
    edges = [[rnd.getrandbits(32) for _ in range(20)] for _ in range(NPES)]

    def feed(spad, layout, inst):
        put(spad, layout, "LEAF", "zero", inst, 0)
        for k in range(20):
            put(spad, layout, "LEAF", "e%d" % k, inst, edges[inst][k])

    def golden(inst):
        v = 0
        for e in edges[inst]:
            v ^= e
        return {"acc": _s32(v)}

    return feed, golden


def make_turbo():
    rnd = random.Random(0x7B0)
    cases = [(rnd.randint(-1 << 20, 1 << 20),
              rnd.randint(-1 << 16, 1 << 16),
              rnd.randint(-1 << 20, 1 << 20),
              rnd.randint(-1 << 16, 1 << 16)) for _ in range(NPES)]

    def feed(spad, layout, inst):
        a0, g0, a1, g1 = cases[inst]
        for k, v in (("a0", a0), ("g0", g0), ("a1", a1), ("g1", g1)):
            put(spad, layout, "LEAF", k, inst, v)

    def golden(inst):
        a0, g0, a1, g1 = cases[inst]
        # Max-Log-MAP 前向递推：alpha[k+1][ns] = max over (s,u)->ns (alpha[k][s]+gamma)
        return {"m": max(_s32(a0 + g0), _s32(a1 + g1))}

    return feed, golden


def make_wifi_vn():
    rnd = random.Random(0xF1E)
    cases = [([rnd.randint(-3000, 3000) for _ in range(24)],
              rnd.randint(-3000, 3000)) for _ in range(NPES)]

    def feed(spad, layout, inst):
        msgs, ch = cases[inst]
        for k in range(24):
            put(spad, layout, "LEAF", "m%d" % k, inst, msgs[k])
        put(spad, layout, "LEAF", "ch", inst, ch)
        put(spad, layout, "LEAF", "zero", inst, 0)

    def golden(inst):
        msgs, ch = cases[inst]
        # min-sum 变量节点：total = ch + Σm；外信息 o_k = total - m_k
        t = ch
        for m in msgs:
            t = _s32(t + m)
        out = {"o%d" % k: _s32(t - m) for k, m in enumerate(msgs)}
        out["sg"] = _s32(_s32(t) >> 31)
        return out

    return feed, golden


def make_wifi_cn():
    """kind: cn 的专用调度器：24 条边的 min1 / min2 / sign。"""
    rnd = random.Random(0xC4)
    sentinel = 32767
    cases = [[rnd.randint(-3000, 3000) for _ in range(24)] for _ in range(20)]

    # CN 镜像把 20 个提升行分两半：PE0..15 处理 0..15，PE0..3 处理 16..19。
    # 布局来自 rule_map.schedule_cn：边 k 在 bank k、word=提升行号；
    # min1/min2/sign 写到 res_banks=[26,27,28]，哨兵在 bank 30。
    def check():
        cycles, words = load_prog("wifi_cn24")
        spad = {}
        bad = 0
        for w, msgs in enumerate(cases):
            for k, v in enumerate(msgs):
                spad[k * W + w] = v & 0xFFFFFFFF
            spad[30 * W + w] = sentinel & 0xFFFFFFFF
        run_isa(words, cycles, spad)
        for w, msgs in enumerate(cases):
            neg = 0
            col = []
            for x in msgs:
                if x < 0:
                    neg += 1
                    col.append(-x)
                else:
                    col.append(x)
            a, b = sorted(col)[:2]
            want = {26: a, 27: b, 28: (-1 if neg % 2 else 0)}
            for bank, exp in want.items():
                got = _s32(spad.get(bank * W + w, 0))
                if got != exp:
                    bad += 1
                    if bad <= 4:
                        print("  w=%d bank=%d got=%d want=%d" % (w, bank, got, exp))
        if bad:
            print("[FAIL] wifi_cn24    %d 个值不一致" % bad, file=sys.stderr)
            return 1
        print("[ OK ] wifi_cn24    %d 拍 × %d PE，独立黄金逐值一致（20 提升行 × 3）"
              % (cycles, NPES))
        return 0
    return check


def main():
    rc = 0
    for qm, name in ((2, "qam_qpsk"), (4, "qam_16qam"), (6, "qam_64qam")):
        feed, golden = make_qam(qm, name)
        rc |= check_case(name, "qam_map", feed, golden)
    for name, maker in (("ldpc_enc_acc", make_ldpc_acc),
                        ("turbo_acs", make_turbo),
                        ("wifi_vn", make_wifi_vn)):
        feed, golden = maker()
        rc |= check_case(name, name, feed, golden)
    rc |= make_wifi_cn()()
    print("=== 独立黄金模型对账：%s ===" % ("全部通过" if rc == 0 else "存在失败"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
