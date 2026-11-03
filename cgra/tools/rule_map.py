#!/usr/bin/env python3
"""规则映射器原型 v0：DFG → 本项目的 CGRA 微码（并自带 ISA 仿真逐位自检）。

规则（刻意保持简单、可解释）：
  1. **拍号 = 数据依赖层号（串行版）**：一个 PE 上按拓扑序串行发射，每条指令 1 拍；
  2. **算子结果留在寄存器里**（r0..r7 线性扫描分配，8 个够用），只有叶子（LD）和
     汇（ST）访问 scratchpad；
  3. **并行只来自"数据实例"**：第 w 个实例跑在第 w 个 PE 上（BN 里 w = 提升行号），
     与手写 BN 微码的并行方式一致；
  4. 叶子 k 读 spad `bank=k, word=实例号`；汇写回 `bank=k`（BN 的减法片），
     或写到指定 bank（硬判决 → bank 25）。这套布局与 `sw/soc/pdsch_bn1_bm.c` 相同。

不做的事（v1 再加）：link 路由、跨 PE 共享算子、PE 内多拍流水重叠、寄存器 spill。

用法：
    python3 cgra/tools/rule_map.py --bn                     # 整条 BN（24 槽）自检
    python3 cgra/tools/rule_map.py --dfg build/bn_chunks3/sub_0.dot
"""
import argparse
import os
import random
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools"))
from cgra import NPES, enc, spad_idx          # noqa: E402  （复用 ISA 编码，避免两套定义）

SPAD_WIN = 0xA000                              # scratchpad 的主机窗口基址（AXI 字节地址）


# ---------------------------------------------------------------- DFG
class DFG:
    """节点 = (name, opcode)；边 = (src, dst, operand)。叶子 = 没有入边的算子。"""

    def __init__(self):
        self.opcode = {}
        self.order = []
        self.edges = []

    def node(self, name, opcode):
        self.opcode[name] = opcode
        self.order.append(name)

    def edge(self, src, dst, operand):
        self.edges.append((src, dst, operand))

    def preds(self, name):
        return [(s, o) for s, d, o in self.edges if d == name]

    def succs(self, name):
        return [d for s, d, _o in self.edges if s == name]

    def leaves(self):
        return [n for n in self.order if not self.preds(n)]

    def sinks(self):
        return [n for n in self.order if not self.succs(n)]

    def levels(self):
        lv = {}
        for n in self.order:                     # 文件顺序保证确定性
            p = self.preds(n)
            lv[n] = 0 if not p else 1 + max(lv[s] for s, _o in p)
        return lv

    def topo(self):
        lv = self.levels()
        return sorted(self.order, key=lambda n: (lv[n], self.order.index(n)))


def parse_dot(path):
    """读 CGRA-ME 的 dot 方言（属性值不加引号；节点名 [A-Za-z_]+[0-9]*）。"""
    txt = open(path).read()
    dfg = DFG()
    for line in txt.splitlines():
        line = line.strip()
        m = re.match(r"(\w+)\s*\[opcode=(\w+)\]", line)
        if m:
            dfg.node(m.group(1), m.group(2))
            continue
        m = re.match(r"(\w+)\s*->\s*(\w+)\s*\[operand=(\d+)\]", line)
        if m:
            dfg.edge(m.group(1), m.group(2), int(m.group(3)))
    if not dfg.order:
        raise ValueError(f"{path}: 没解析出任何算子节点")
    return dfg


def build_bn_dfg(nmsg=23):
    """整条 BN（24 槽）：acc = Σmsg + llr；out_k = acc − msg_k；hard = acc >> 31。"""
    d = DFG()
    for k in range(nmsg):
        d.node(f"m{k}", "add")
    d.node("llr", "add")
    # 串行累加（寄存器只需 2 个，和手写微码同构）
    acc = "m0"
    for k in range(1, nmsg):
        name = f"A{k}"
        d.node(name, "add")
        d.edge(acc, name, 0)
        d.edge(f"m{k}", name, 1)
        acc = name
    d.node("ALL", "add")
    d.edge(acc, "ALL", 0)
    d.edge("llr", "ALL", 1)
    for k in range(nmsg):
        d.node(f"S{k}", "sub")
        d.edge("ALL", f"S{k}", 0)
        d.edge(f"m{k}", f"S{k}", 1)
    d.node("H0", "shr")
    d.edge("ALL", "H0", 0)
    return d


# ---------------------------------------------------------------- 调度
class Microcode:
    def __init__(self):
        self.words = {}          # (cycle, pe) -> (op, a, b, dst, imm)
        self.cycles = 0

    def put(self, cycle, pe, op, a="zero", b="zero", dst=0, imm=0):
        self.words[(cycle, pe)] = (op, a, b, dst, imm)
        self.cycles = max(self.cycles, cycle + 1)

    def build(self):
        return [enc(*self.words.get((c, p), ("nop",))) for c in range(self.cycles)
                for p in range(NPES)]


def leaf_bank(name):
    """叶子 → spad bank（与手写 BN 布局一致：msg_k → bank k，llr → bank 24）。"""
    if name == "llr":
        return 24
    m = re.match(r"m(\d+)$", name)
    return int(m.group(1)) if m else None


def leaf_banks(dfg):
    """整张图的叶子地址表：msg_k → bank k、llr → bank 24（与手写 BN 布局一致），
    其余叶子（切块里的 acc 读取 C_j、部分和 P/Q）从 bank 26 起，避免和减法片的
    原位写回（S_k → bank k）撞车。
    """
    banks, nxt = {}, 26
    for name in dfg.leaves():
        b = leaf_bank(name)
        if b is None:
            b = nxt
            nxt += 1
            if b > 31:
                raise SystemExit("通用叶子太多（最多 6 个，bank 26..31）")
        banks[name] = b
    return banks


def sink_bank(name):
    """汇 → 写回 bank（减法片写回自己那条消息的槽，硬判决 → bank 25）。"""
    m = re.match(r"S(\d+)$", name)
    if m:
        return int(m.group(1))
    if name == "H0":
        return 25
    return None


def schedule(dfg, instances=16):
    """规则映射：一个实例一条串行指令流，跑在自己的 PE 上。

    返回 (Microcode, 层号表, 汇地址表, 叶子地址表)：汇地址表记录每个汇写到哪个 bank，
    未约定地址的汇（通用 DFG 里的中间汇）按出现顺序分到 bank 26 起。
    """
    mc = Microcode()
    lv = dfg.levels()
    banks = leaf_banks(dfg)
    # 汇地址必须在实例循环之前定好（否则每个实例会分到不同 bank）
    sink_addrs = {}
    for n in dfg.sinks():
        b = sink_bank(n)
        if b is None:
            # 通用 DFG：按出现顺序分到 bank 26 起。注意 spad_idx 只保留
            # bank[4:0]（页内地址），所以不能用到 32 以上。
            b = 26 + len(sink_addrs)
            if b > 31:
                raise SystemExit("通用 DFG 的汇太多（最多 6 个，bank 26..31）")
        sink_addrs[n] = b
    topo = dfg.topo()
    idx = {n: i for i, n in enumerate(topo)}
    # 每个值的"最后一个消费者"位置（叶子/算子都用它判断寄存器何时可以释放）
    lastuse = {n: (max(idx[d] for d in dfg.succs(n)) if dfg.succs(n) else idx[n])
               for n in topo}
    for w in range(instances):
        cyc = 0
        reg_of = {}                       # 只记录"算子结果 -> 寄存器"
        free = list(range(8))[::-1]       # pop() 从 r0 开始分配
        for n in topo:
            op = dfg.opcode[n]
            if not dfg.preds(n):          # 叶子：不单独发射，由消费者现场 LD
                continue
            tmp, srcs = [], []            # tmp = 本拍现场 LD 进来的叶子寄存器
            for s, _operand in sorted(dfg.preds(n), key=lambda t: t[1]):
                if s in reg_of:
                    srcs.append(reg_of[s])
                    continue
                bank = banks.get(s)
                if bank is None:
                    raise SystemExit(f"操作数 {s} 既不在寄存器里也不是已知叶子（v0 不支持 spill）")
                if not free:
                    raise SystemExit(f"寄存器不够：{n} 取 {s} 时无空闲寄存器")
                r = free.pop()
                mc.put(cyc, w, "ld", dst=r, imm=spad_idx(bank, w))
                cyc += 1
                srcs.append(r)
                tmp.append(r)
            if not free:
                raise SystemExit(f"寄存器不够：{n} 的结果无处可放")
            dst = free.pop()
            a = srcs[0] if srcs else "zero"
            b = srcs[1] if len(srcs) > 1 else "zero"
            imm = 31 if op == "shr" else 0
            mc.put(cyc, w, op, a=a, b=b, dst=dst, imm=imm)
            cyc += 1
            reg_of[n] = dst
            free.extend(tmp)              # 叶子寄存器用完即还
            for s, _o in dfg.preds(n):    # 上游结果的最后一个消费者就是本算子
                if s in reg_of and lastuse[s] <= idx[n]:
                    free.append(reg_of.pop(s))
            if not dfg.succs(n):          # 汇：写回 spad
                bank = sink_addrs[n]
                mc.put(cyc, w, "st", a=dst, imm=spad_idx(bank, w))
                cyc += 1
                free.append(reg_of.pop(n))
    return mc, lv, sink_addrs, banks


# ---------------------------------------------------------------- ISA 仿真
def _s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def _sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def simulate(mc, spad):
    """按 ISA 语义逐拍执行（寄存器堆 + spad；本原型不用 link）。"""
    regs = [[0] * 8 for _ in range(NPES)]
    for cyc in range(mc.cycles):
        for pe in range(NPES):
            w = mc.words.get((cyc, pe))
            if not w:
                continue
            op, a, b, dst, imm = w
            if op == "nop":
                continue
            if op == "ld":
                regs[pe][dst] = spad.get(imm, 0)
                continue
            # 源选择器（本原型只用寄存器与 zero/imm）
            def val(s):
                if isinstance(s, int):
                    return regs[pe][s]
                if s == "zero":
                    return 0
                if s == "imm":
                    return imm
                raise SystemExit(f"仿真器不支持源 {s}")
            x, y = val(a), val(b)
            if op == "mov":
                r = x
            elif op == "add":
                r = _s32(x + y)
            elif op == "sub":
                r = _s32(x - y)
            elif op == "mul":
                r = _s32(_sx16(x) * _sx16(y))
            elif op == "and":
                r = x & y
            elif op == "xor":
                r = x ^ y
            elif op == "shr":
                r = _s32(_s32(x) >> (imm & 31))
            elif op == "shl":
                r = _s32(x << (imm & 31))
            elif op == "st":
                spad[imm] = regs[pe][a]
                continue
            else:
                raise SystemExit(f"仿真器不支持 op {op}")
            regs[pe][dst] = r & 0xFFFFFFFF
    return regs


def bn_case(rows=16, nmsg=23, seed=1234):
    """BN 的随机激励 + 黄金结果（spad 索引口径，和微码里的 imm 一致）。"""
    rnd = random.Random(seed)
    spad, exp = {}, {}
    for w in range(rows):
        msgs = [rnd.randint(-3000, 3000) for _ in range(nmsg)]
        llr = rnd.randint(-3000, 3000)
        for k in range(nmsg):
            spad[spad_idx(k, w)] = msgs[k] & 0xFFFFFFFF
        spad[spad_idx(24, w)] = llr & 0xFFFFFFFF
        v = llr + sum(msgs)
        for k in range(nmsg):
            exp[spad_idx(k, w)] = (v - msgs[k]) & 0xFFFFFFFF
        exp[spad_idx(25, w)] = (-1 if v < 0 else 0) & 0xFFFFFFFF
    return spad, exp


def check_bn(mc, rows=16, nmsg=23, seed=1234):
    """黄金模型：v = llr + Σmsg；out_k = v − msg_k；hard = −1 if v < 0 else 0。"""
    spad, exp = bn_case(rows, nmsg, seed)
    simulate(mc, spad)
    errs = [(idx, spad.get(idx), want) for idx, want in exp.items() if spad.get(idx) != want]
    return errs, len(exp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bn", action="store_true", help="整条 BN（24 槽）")
    ap.add_argument("--dfg", help="任意 dot（叶子必须是 m<k>/llr，汇必须是 S<k>/H0）")
    ap.add_argument("--instances", type=int, default=16)
    ap.add_argument("--emit", help="把微码写成二进制（cycle-major，每字 32 bit）")
    ap.add_argument("--emit-hex", metavar="PREFIX",
                    help="写成台架可读的 PREFIX.prog.hex / PREFIX.preload.hex（BN 模式）")
    a = ap.parse_args()

    if a.bn:
        dfg = build_bn_dfg()
        name = "ldpc_bn"
    elif a.dfg:
        dfg = parse_dot(a.dfg)
        name = os.path.basename(a.dfg)
    else:
        ap.error("需要 --bn 或 --dfg")

    mc, lv, sink_addrs, banks = schedule(dfg, a.instances)
    nops = len(dfg.order)
    print(f"{name}: {nops} 个算子 / {len(dfg.leaves())} 个叶子 / "
          f"{len(dfg.sinks())} 个汇，依赖深度 {max(lv.values())}")
    print(f"  微码 {mc.cycles} 拍 × {NPES} PE（每个实例 1 个 PE，共 {a.instances} 个实例）")

    if a.bn:
        errs, nvals = check_bn(mc, rows=min(a.instances, 16))
        print(f"  自检：{nvals} 个 spad 值，差异 {len(errs)} 个")
        for idx, got, want in errs[:5]:
            print(f"    idx={idx} got=0x{got:08x} want=0x{want:08x}")
        ok = not errs
    else:
        # 通用模式：随机叶子值，把所有汇的写回值与黄金模型逐一对账
        rnd = random.Random(7)
        spad, exp = {}, {}
        for inst in range(min(a.instances, 16)):
            vals_cache = {}
            for l in dfg.leaves():
                v = rnd.randint(-5000, 5000)
                vals_cache[l] = _s32(v)
                spad[spad_idx(banks[l], inst)] = v & 0xFFFFFFFF
            for n in dfg.topo():
                if not dfg.preds(n):       # 叶子已在上面赋值
                    continue
                op = dfg.opcode[n]
                srcs = [vals_cache[s] for s, _o in sorted(dfg.preds(n), key=lambda t: t[1])]
                if op == "add":
                    vals_cache[n] = _s32(srcs[0] + srcs[1])
                elif op == "sub":
                    vals_cache[n] = _s32(srcs[0] - srcs[1])
                elif op == "shr":
                    vals_cache[n] = _s32(srcs[0] >> 31)
                else:
                    ap.error(f"通用模式的黄金模型不支持 {op}")
                if not dfg.succs(n):
                    exp[spad_idx(sink_addrs[n], inst)] = vals_cache[n] & 0xFFFFFFFF
        simulate(mc, spad)
        errs = [(i, spad.get(i), w) for i, w in exp.items() if spad.get(i) != w]
        print(f"  自检：{len(exp)} 个汇值，差异 {len(errs)} 个")
        for idx, got, want in errs[:5]:
            gs = f"0x{got:08x}" if got is not None else "缺失"
            print(f"    idx={idx} got={gs} want=0x{want:08x}")
        ok = not errs

    if a.emit:
        os.makedirs(os.path.dirname(a.emit) or ".", exist_ok=True)
        with open(a.emit, "wb") as f:
            for word in mc.build():
                f.write(word.to_bytes(4, "little"))
        print(f"  微码写入 {a.emit}（{mc.cycles * NPES} 字）")

    if a.emit_hex:
        if not a.bn:
            ap.error("--emit-hex 目前只用于 --bn")
        spad, _exp = bn_case(rows=min(a.instances, 16))
        os.makedirs(os.path.dirname(a.emit_hex) or ".", exist_ok=True)
        with open(a.emit_hex + ".prog.hex", "w") as f:
            for word in mc.build():
                f.write(f"0x{word:08x}\n")
        with open(a.emit_hex + ".preload.hex", "w") as f:
            for idx, val in sorted(spad.items()):
                f.write(f"0x{SPAD_WIN + idx * 4:04x} 0x{val:08x}\n")
        print(f"  台架激励写入 {a.emit_hex}.prog.hex / .preload.hex"
              f"（{len(spad)} 个预装载字，cycles={mc.cycles}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
