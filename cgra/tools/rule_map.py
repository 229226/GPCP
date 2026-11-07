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
        self.imm = {}            # name -> 移位立即数（仅 shr/shl 使用；其余算子为 0）
        self.addr = {}           # name -> (base, stride)  平坦 spad 地址 = base + stride*instance
        self.order = []
        self.edges = []

    def node(self, name, opcode, imm=0):
        self.opcode[name] = opcode
        self.imm[name] = imm
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
        """声明序优先的稳定拓扑排序（Kahn）。

        在不破坏依赖的前提下，尽可能按「构造/YAML 声明顺序」发射算子，
        从而把中间结果的活跃区间缩短、降低寄存器压力——这正是 MMSE/FFT 这类
        宽 DFG 能塞进 8 个 GPR 的关键（见 mmse_2x1 / fft_butterfly 的实测）。
        不再是「先发完所有 level-1 再发 level-2」，否则 8 个并行乘积会同时占满寄存器。
        """
        import heapq
        indeg = {n: 0 for n in self.order}
        succ = {n: [] for n in self.order}
        for s, d, _o in self.edges:
            indeg[d] = indeg.get(d, 0) + 1
            succ[s].append(d)
        pos = {n: i for i, n in enumerate(self.order)}
        ready = [i for i, n in enumerate(self.order) if indeg[n] == 0]
        heapq.heapify(ready)                 # 小根堆，按声明下标
        out = []
        while ready:
            i = heapq.heappop(ready)
            out.append(self.order[i])
            for d in succ[self.order[i]]:
                indeg[d] -= 1
                if indeg[d] == 0:
                    heapq.heappush(ready, pos[d])
        return out


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
    d.node("H0", "shr", imm=31)      # 硬判决：算术右移 31 位取符号（-1/0）
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


def _touch(liv, name, cyc):
    """记录 name 在 cyc 拍被使用（首次/末次），用于 L2-A 的生命区间。"""
    if liv[name][0] is None:
        liv[name][0] = cyc
    liv[name][1] = cyc


def schedule(dfg, instances=16, banks=None, sink_addrs=None, addrs=None,
             return_liveness=False):
    """规则映射：一个实例一条串行指令流，跑在自己的 PE 上。

    返回 (Microcode, 层号表, 汇地址表, 叶子地址表)；return_liveness=True 时额外返回
    (生命区间表, 最大活跃寄存器数)。

    banks / sink_addrs：若给定则使用（L2-A 由 bank_alloc 分配）；否则沿用旧的
    leaf_banks / sink_bank 硬编码（与 rule_map.main 的 --dfg/--bn 路径保持兼容）。
    """
    mc = Microcode()
    lv = dfg.levels()
    if banks is None:
        banks = leaf_banks(dfg)
    if sink_addrs is None:
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
    liveness = {n: [None, None] for n in topo}
    max_regs = 0
    end = 0
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
                if addrs and s in addrs:
                    base, stride = addrs[s]
                    imm = base + stride * w          # 平坦寻址：base + stride*instance
                else:
                    bank = banks.get(s)
                    if bank is None:
                        raise SystemExit(f"操作数 {s} 既不在寄存器里也不是已知叶子（v0 不支持 spill）")
                    imm = spad_idx(bank, w)
                if not free:
                    raise SystemExit(f"寄存器不够：{n} 取 {s} 时无空闲寄存器")
                r = free.pop()
                mc.put(cyc, w, "ld", dst=r, imm=imm)
                _touch(liveness, s, cyc)
                cyc += 1
                srcs.append(r)
                tmp.append(r)
            if not free:
                raise SystemExit(f"寄存器不够：{n} 的结果无处可放")
            dst = free.pop()
            a = srcs[0] if srcs else "zero"
            b = srcs[1] if len(srcs) > 1 else "zero"
            # 移位立即数：仅 shr/shl 使用 YAML 里声明的 imm（计划书 §6.2 之外的通用需求，
            # 例如 FFT/MMSE 的 Q15 缩放 >>1 / >>15）；其余算子 imm 恒 0。
            imm = dfg.imm.get(n, 0) if op in ("shr", "shl") else 0
            if op == "neg":                 # 伪算子：sub a=zero b=x
                mc.put(cyc, w, "sub", a="zero", b=a, dst=dst)
            else:
                mc.put(cyc, w, op, a=a, b=b, dst=dst, imm=imm)
            _touch(liveness, n, cyc)
            cyc += 1
            reg_of[n] = dst
            max_regs = max(max_regs, 8 - len(free))   # 当前活跃寄存器数
            free.extend(tmp)              # 叶子寄存器用完即还
            for s, _o in dfg.preds(n):    # 上游结果的最后一个消费者就是本算子
                if s in reg_of and lastuse[s] <= idx[n]:
                    free.append(reg_of.pop(s))
            if not dfg.succs(n):          # 汇：写回 spad
                sa = sink_addrs[n]
                imm = (sa[0] + sa[1] * w) if isinstance(sa, tuple) else spad_idx(sa, w)
                mc.put(cyc, w, "st", a=dst, imm=imm)
                _touch(liveness, n, cyc)
                cyc += 1
                free.append(reg_of.pop(n))
        end = max(end, cyc)
    # 叶子常驻 spad 整个程序（输入由外部一次性写入，自检只初始化一次）
    for l in dfg.leaves():
        if liveness[l][0] is None:
            liveness[l] = [0, end]
        else:
            liveness[l][1] = end
    liv = {n: (liveness[n][0] if liveness[n][0] is not None else 0,
               liveness[n][1] if liveness[n][1] is not None else 0)
           for n in topo}
    if return_liveness:
        return mc, lv, sink_addrs, banks, liv, max_regs
    return mc, lv, sink_addrs, banks


# ---------------------------------------------------------------- ISA 仿真
def _s32(v):
    v &= 0xFFFFFFFF
    return v - 0x100000000 if v & 0x80000000 else v


def _sx16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _neighbour(pe, d, n=4):
    """PE 的 N/E/S/W 邻居编号（越界返回 None）。"""
    r, c = divmod(pe, n)
    if d == "n":
        r -= 1
    elif d == "s":
        r += 1
    elif d == "e":
        c += 1
    elif d == "w":
        c -= 1
    if not (0 <= r < n and 0 <= c < n):
        return None
    return r * n + c


def simulate(mc, spad, n=4):
    """按 ISA 语义逐拍执行：寄存器堆 + spad + link（link = 上一拍写出的值，NOP/ST 保持）。"""
    regs = [[0] * 8 for _ in range(NPES)]
    link = [0] * NPES                        # 本拍邻居可见的 link（上一拍写入值）
    for cyc in range(mc.cycles):
        nxt = list(link)                     # 默认保持
        for pe in range(NPES):
            w = mc.words.get((cyc, pe))
            if not w:
                continue
            op, a, b, dst, imm = w
            if op == "nop":
                continue

            def val(s):
                if isinstance(s, int):
                    return regs[pe][s]
                if s == "zero":
                    return 0
                if s == "imm":
                    return imm
                if s == "link":
                    return link[pe]
                if s in ("n", "e", "s", "w"):
                    nb = _neighbour(pe, s, n)
                    return 0 if nb is None else link[nb]
                raise SystemExit(f"仿真器不支持源 {s}")

            if op == "st":
                spad[imm] = regs[pe][a]
                continue
            if op == "ld":
                v = spad.get(imm, 0)
            else:
                x, y = val(a), val(b)
                if op == "mov":
                    v = x
                elif op == "add":
                    v = _s32(x + y)
                elif op == "sub":
                    v = _s32(x - y)
                elif op == "mul":
                    v = _s32(_sx16(x) * _sx16(y))
                elif op == "and":
                    v = x & y
                elif op == "xor":
                    v = x ^ y
                elif op == "shr":
                    v = _s32(_s32(x) >> (imm & 31))
                elif op == "shl":
                    v = _s32(x << (imm & 31))
                elif op == "max":
                    v = x if _s32(x) > _s32(y) else y
                elif op == "min":
                    v = x if _s32(x) < _s32(y) else y
                else:
                    raise SystemExit(f"仿真器不支持 op {op}")
            regs[pe][dst] = v & 0xFFFFFFFF
            nxt[pe] = regs[pe][dst]           # 任何寄存器写都会更新 link
        link = nxt
    return regs


# ---------------------------------------------------------------- BN 拆分成 4 个 PE/行
#: 24 槽（真实最大列度 23 + 1 个补零槽，与流式微码的 BNSLOT=24 一致）按 4 个 PE 均分
BN_GROUPS = ((0, 6), (6, 12), (12, 18), (18, 24))
BN_SPLIT_CYC = 31          # 一个 pass（4 行）的拍数（6 条消息/PE）

def schedule_bn_split(rows=20, nmsg=24):
    """k=4 的 BN 调度：一行拆到 4 个**横向相邻**的 PE，消息常驻寄存器，
    部分和与 acc 走 link（1 跳 1 拍，不碰 spad）。每 4 行一个 pass，每 pass 31 拍。

    时间线（j = 0..3 是 PE 列，每组 6 条消息；se = 2*6-1 = 11 是最后一个 ADD 的拍）：
      c0..5    LD m0..m5 → r1..r6
      c6       MOV  r0 = r1
      c7..se   ADD  r0 += r_i（r2..r6）        → 各组部分和
      se+1     p2: ADD r0 += 'e'（p3 的部分和）
      se+2     p1: ADD r0 += 'e'（p2 合并值）；p0: LD llr → r7
      se+3     p0: ADD r0 += r7
      se+4     p0: ADD r0 += 'e'（p1 合并值）= acc
      se+5     p0: SHR r7 = r0 >> 31；p1: MOV r7 = 'w'
      se+6     p0: ST r7 → bank 25；p2: MOV r7 = 'w'
      se+7     p0 开始减法；p3: MOV r7 = 'w'
      se+6..  各 PE 对自己的 6 条边做 SUB + ST（2 拍/边），最后一个在 c30
    """
    if nmsg != 24:
        raise SystemExit("schedule_bn_split 目前只支持 24 个消息槽（= 23 + 1 个补零）")
    mc = Microcode()
    size = BN_GROUPS[0][1] - BN_GROUPS[0][0]
    se = 2 * size - 1                     # 最后一个 ADD 的拍
    start = {0: se + 7, 1: se + 6, 2: se + 7, 3: se + 8}
    for r in range(rows):
        base = BN_SPLIT_CYC * (r // 4)
        pr = r % 4
        for j, (lo, hi) in enumerate(BN_GROUPS):
            pe = pr * 4 + j
            for i, k in enumerate(range(lo, hi)):
                mc.put(base + i, pe, "ld", dst=1 + i, imm=spad_idx(k, r))
            mc.put(base + size, pe, "mov", a=1, dst=0)
            for i in range(1, size):
                mc.put(base + size + i, pe, "add", a=0, b=1 + i, dst=0)
            if j == 0:
                mc.put(base + se + 2, pe, "ld", dst=7, imm=spad_idx(24, r))
                mc.put(base + se + 3, pe, "add", a=0, b=7, dst=0)
                mc.put(base + se + 4, pe, "add", a=0, b="e", dst=0)     # acc
                mc.put(base + se + 5, pe, "shr", a=0, dst=7, imm=31)
                mc.put(base + se + 6, pe, "st", a=7, imm=spad_idx(25, r))
            elif j == 1:
                mc.put(base + se + 2, pe, "add", a=0, b="e", dst=0)
                mc.put(base + se + 5, pe, "mov", a="w", dst=7)          # 收到 acc
            elif j == 2:
                mc.put(base + se + 1, pe, "add", a=0, b="e", dst=0)
                mc.put(base + se + 6, pe, "mov", a="w", dst=7)
            else:
                mc.put(base + se + 7, pe, "mov", a="w", dst=7)
            accr, dstr = (0, 7) if j == 0 else (7, 0)
            for i, k in enumerate(range(lo, hi)):
                mc.put(base + start[j] + 2 * i, pe, "sub", a=accr, b=1 + i, dst=dstr)
                mc.put(base + start[j] + 2 * i + 1, pe, "st", a=dstr, imm=spad_idx(k, r))
    return mc


def check_bn_split(mc, rows=20, nmsg=24, seed=1234):
    """拆分成 4 PE/行 的黄金模型自检（激励/期望与 check_bn 相同）。"""
    spad, exp = bn_case(rows=rows, nmsg=nmsg, seed=seed)
    simulate(mc, spad)
    errs = [(idx, spad.get(idx), want) for idx, want in exp.items() if spad.get(idx) != want]
    return errs, len(exp)


# ---------------------------------------------------------------- CN（min1/min2/sign）
CN_EDGES = 10            # BG2 最大检查节点度数（不足补 sentinel）
CN_SENTINEL_BANK = 30
CN_CYC_HALF = 86         # 3 初始化 + 10 边 × 8 拍 + 3 写回 = 86
CN_CYC_TOTAL = 2 * CN_CYC_HALF


def schedule_cn(edges=CN_EDGES, sentinel_bank=CN_SENTINEL_BANK):
    """一次程序跑完 20 个提升行（PE0..15 处理 0..15，PE0..3 处理 16..19）。

    每个 PE / 提升行：对 10 条边（不足补 +32767）算 min1、min2、sign。
    每条边 8 拍：`LD / SHR(符号位) / XOR / SUB（|x|）/ XOR(sign 累加) /
    MIN / MAX / MIN（min1/min2 更新）`；两个 min 值成对更新只是把 lo/hi
    先算出来，**并不省指令**（插入 (min1,min2) 本身就要 4 拍）—— 真正省的是
    "20 行一次跑完、程序只装一次"（见 docs/10 §13）。
    寄存器：r3=min1, r4=min2, r5=sign；r0/r1/r2 与 r6/r7 是两条边的临时量。
    """
    if edges % 2:
        raise SystemExit("schedule_cn 要求偶数条边（成对处理）")
    mc = Microcode()
    for half, (npe, word_base) in enumerate(((16, 0), (4, 16))):
        base = half * CN_CYC_HALF
        for pe in range(npe):
            w = word_base + pe
            cyc = base
            mc.put(cyc, pe, "ld", dst=3, imm=spad_idx(sentinel_bank, w)); cyc += 1
            mc.put(cyc, pe, "ld", dst=4, imm=spad_idx(sentinel_bank, w)); cyc += 1
            mc.put(cyc, pe, "xor", a=5, b=5, dst=5); cyc += 1
            for p in range(edges // 2):
                ka, kb = 2 * p, 2 * p + 1
                # 边 a：r0 = x, r1 = 符号位, r2 = |x|
                mc.put(cyc, pe, "ld", dst=0, imm=spad_idx(ka, w)); cyc += 1
                mc.put(cyc, pe, "shr", a=0, dst=1, imm=31); cyc += 1
                mc.put(cyc, pe, "xor", a=0, b=1, dst=2); cyc += 1
                mc.put(cyc, pe, "sub", a=2, b=1, dst=2); cyc += 1
                mc.put(cyc, pe, "xor", a=5, b=1, dst=5); cyc += 1
                # 边 b：r6 = x, r7 = 符号位, r0 = |x|
                mc.put(cyc, pe, "ld", dst=6, imm=spad_idx(kb, w)); cyc += 1
                mc.put(cyc, pe, "shr", a=6, dst=7, imm=31); cyc += 1
                mc.put(cyc, pe, "xor", a=6, b=7, dst=0); cyc += 1
                mc.put(cyc, pe, "sub", a=0, b=7, dst=0); cyc += 1
                mc.put(cyc, pe, "xor", a=5, b=7, dst=5); cyc += 1
                # {lo,hi} 插入 (min1,min2)
                mc.put(cyc, pe, "min", a=2, b=0, dst=1); cyc += 1     # lo
                mc.put(cyc, pe, "max", a=2, b=0, dst=6); cyc += 1     # hi
                mc.put(cyc, pe, "min", a=4, b=6, dst=7); cyc += 1     # min(min2,hi)
                mc.put(cyc, pe, "max", a=3, b=1, dst=2); cyc += 1     # max(min1,lo)
                mc.put(cyc, pe, "min", a=2, b=7, dst=4); cyc += 1     # min2'
                mc.put(cyc, pe, "min", a=3, b=1, dst=3); cyc += 1     # min1'
            mc.put(cyc, pe, "st", a=3, imm=spad_idx(10, w)); cyc += 1
            mc.put(cyc, pe, "st", a=4, imm=spad_idx(11, w)); cyc += 1
            mc.put(cyc, pe, "st", a=5, imm=spad_idx(12, w)); cyc += 1
    return mc


def cn_case(rows=20, edges=CN_EDGES, sentinel=32767, seed=99):
    """CN 激励（spad 索引口径）+ 黄金 min1/min2/sign。"""
    rnd = random.Random(seed)
    spad, exp = {}, {}
    for w in range(rows):
        for k in range(edges):
            spad[spad_idx(k, w)] = rnd.randint(-512, 512) & 0xFFFFFFFF
        spad[spad_idx(CN_SENTINEL_BANK, w)] = sentinel
        col = [_s32(spad[spad_idx(k, w)]) for k in range(edges)]
        ab = sorted(abs(x) for x in col)
        exp[spad_idx(10, w)] = ab[0] & 0xFFFFFFFF
        exp[spad_idx(11, w)] = ab[1] & 0xFFFFFFFF
        neg = sum(1 for x in col if x < 0)
        exp[spad_idx(12, w)] = (-1 if neg % 2 else 0) & 0xFFFFFFFF
    return spad, exp


def check_cn(mc, rows=20, seed=99):
    spad, exp = cn_case(rows=rows, seed=seed)
    simulate(mc, spad)
    errs = [(i, spad.get(i), w) for i, w in exp.items() if spad.get(i) != w]
    return errs, len(exp)


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


def write_hex(prefix, mc, spad):
    """写成台架可读的 PREFIX.prog.hex / PREFIX.preload.hex。"""
    os.makedirs(os.path.dirname(prefix) or ".", exist_ok=True)
    with open(prefix + ".prog.hex", "w") as f:
        for w in mc.build():
            f.write(f"0x{w:08x}\n")
    with open(prefix + ".preload.hex", "w") as f:
        for idx, v in sorted(spad.items()):
            f.write(f"0x{SPAD_WIN + idx * 4:04x} 0x{v:08x}\n")
    return prefix + ".prog.hex", prefix + ".preload.hex"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bn", action="store_true", help="整条 BN（24 槽）")
    ap.add_argument("--bn-split", action="store_true",
                    help="BN 拆成 4 个 PE/行（消息常驻寄存器 + link 广播 acc）")
    ap.add_argument("--cn", action="store_true",
                    help="CN 的 min1/min2/sign（成对归约，20 个提升行一次跑完）")
    ap.add_argument("--dfg", help="任意 dot（叶子 m<k>/llr 沿用 BN 布局，其余从 bank 26 起）")
    ap.add_argument("--instances", type=int, default=16)
    ap.add_argument("--rows", type=int, default=20, help="--bn-split 处理多少行（Zc=20）")
    ap.add_argument("--emit", help="把微码写成二进制（cycle-major，每字 32 bit）")
    ap.add_argument("--emit-hex", metavar="PREFIX",
                    help="写成台架可读的 PREFIX.prog.hex / PREFIX.preload.hex")
    a = ap.parse_args()

    if a.cn:
        mc = schedule_cn()
        errs, nvals = check_cn(mc)
        print(f"ldpc_cn: 成对 min1/min2 归约，20 个提升行一次跑完；"
              f"微码 {mc.cycles} 拍 × {NPES} PE")
        print(f"  自检：{nvals} 个 spad 值，差异 {len(errs)} 个")
        for idx, got, want in errs[:6]:
            print(f"    idx={idx} got=0x{(got or 0):08x} want=0x{want:08x}")
        print(f"  对照：手写 CN 每行 2 × 90 = 180 拍（本版 {mc.cycles} 拍）")
        if a.emit_hex:
            spad, _exp = cn_case()
            write_hex(a.emit_hex, mc, spad)
            print(f"  台架激励写入 {a.emit_hex}.prog.hex / .preload.hex")
        return 0 if not errs else 1

    if a.bn_split:
        rows = min(a.rows, 20)
        mc = schedule_bn_split(rows=rows)
        errs, nvals = check_bn_split(mc, rows=rows)
        passes = -(-rows // 4)
        print(f"ldpc_bn/split4: 每行 4 个 PE、每次 4 行；{rows} 行 = {passes} pass，"
              f"微码 {mc.cycles} 拍 × {NPES} PE")
        print(f"  自检：{nvals} 个 spad 值，差异 {len(errs)} 个")
        for idx, got, want in errs[:8]:
            print(f"    idx={idx} got=0x{(got or 0):08x} want=0x{want:08x}")
        print(f"  对照：v0（1 PE/行）20 行 = 2 次 × 118 = 236 拍；手写微码 = 2 × 125 = 250 拍")
        if a.emit_hex:
            spad, _exp = bn_case(rows=rows)
            write_hex(a.emit_hex, mc, spad)
            print(f"  台架激励写入 {a.emit_hex}.prog.hex / .preload.hex")
        return 0 if not errs else 1

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
