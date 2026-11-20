#!/usr/bin/env python3
"""把 BG2 变量节点（24 个消息槽）的 BN 数据流切成 <=8 算子的子图，逐个交给 CGRA-ME ILP。

背景（docs/protocols/README.md）：真实 BG2 的最大列度是 23（实测：`sw/include/ldpc_bg2.h` 的 42x52 基图，
列的变量节点度数分布为 1..23），所以流式 BN 微码用 24 个消息槽（第 24 槽补零）。
每个提升行（一个 PE）的数据流：

    acc  = msg[0] + ... + msg[22] + llr            # 24 个叶子，23 个 ADD
    out_k = acc - msg[k]          (k = 0..22)      # 23 个 SUB
    hard = acc >> 31                               # 1 个 SHR

本脚本把这个图按数据依赖切成小片（每片 <= 8 个算子），每片生成一个 dot，单独跑
CGRA-ME 的 ILP 映射器（II=1，8x8 架构），最后汇总"片子数 / 映射成功率 / 求解时间 /
每片用到的 PE 数与拍数"。叶子（真实芯片里的 LD）在这里建模成"无操作数算子"，
因为这一版 CGRA-ME 对 `input=1` 节点一律判 infeasible（docs/compiler/README.md）。

注意节点命名：dot 词法只认 `[A-Za-z_]+[0-9]*`（字母/下划线在前、数字在后），
`a0_0` 这种"字母数字下划线混排"会报 `syntax error, unexpected ID`。

    python3 cgra/tools/bn_chunks.py --out build/bn_chunks --ii 1 --limit 30
"""
import argparse
import os
import re
import subprocess
import sys

NSLOT = 23          # 真实最大列度（24 个槽的第 24 个是补零）
G1 = 4              # 第一层每片处理多少个消息
G2 = 3              # 减法片：每片 3 个消息 + 1 个 acc


class Chunk:
    def __init__(self, name, kind):
        self.name = name
        self.kind = kind
        self.nodes = []      # (name, opcode, [operand producers])
        self.edges = []      # (src, dst, operand)

    def leaf(self, name, opcode="add"):
        self.nodes.append((name, opcode, [], True))
        return name

    def op(self, name, opcode, srcs):
        self.nodes.append((name, opcode, list(srcs), False))
        for i, s in enumerate(srcs):
            self.edges.append((s, name, i))
        return name

    def dot(self):
        lines = ["digraph {"]
        for name, opcode, _srcs, _leaf in self.nodes:
            lines.append(f"  {name} [opcode={opcode}];")
        for src, dst, operand in self.edges:
            lines.append(f"  {src} -> {dst} [operand={operand}];")
        lines.append("}")
        return "\n".join(lines) + "\n"

    def nops(self):
        return len(self.nodes)


def build_chunks():
    """返回 (chunks, leaf_deps)。

    leaf_deps 记录每片依赖的上游片（真实硬件里这些值要经 spad 传递），用于估算拍数。
    """
    chunks = []

    # 第一层：24 个叶子（23 个 msg + llr）分组求和
    partials = []
    idx = 0
    group = 0
    while idx < NSLOT:
        c = Chunk(f"g1_{group}", "sum")
        names = []
        for k in range(idx, min(idx + G1, NSLOT)):
            names.append(c.leaf(f"m{k}"))
        acc = names[0]
        for j, n in enumerate(names[1:]):
            acc = c.op(f"A{j}", "add", [acc, n])
        partials.append(f"p{group}")
        chunks.append(c)
        idx += G1
        group += 1
    ngroup = group

    # 第二层：把第一层的部分和（+ llr）合并
    c = Chunk("g2_a", "sum")
    acc = None
    for g in range(ngroup):
        if g >= 4:
            continue
        n = c.leaf(f"P{g}")
        acc = n if acc is None else c.op(f"B{g}", "add", [acc, n])
    chunks.append(c)

    c = Chunk("g2_b", "sum")
    acc = None
    for g in range(4, ngroup):
        n = c.leaf(f"P{g}")
        acc = n if acc is None else c.op(f"B{g}", "add", [acc, n])
    llr = c.leaf("llr")
    acc = c.op(f"B{ngroup}", "add", [acc, llr])
    chunks.append(c)

    # 第三层：两个部分和相加 = acc
    c = Chunk("g3_acc", "sum")
    a = c.leaf("Q0")
    b = c.leaf("Q1")
    acc_final = c.op("ACC", "add", [a, b])
    chunks.append(c)

    # 减法片：每片 3 个消息 + 3 个 acc 叶子（acc 在真机上本来就是每条边各读一次 spad，
    # 这里也每条边一个叶子，避免 val 的扇出 > 2 —— CGRA-ME 的 ILP 对扇出 >= 3 的
    # val 会出现"求解成功但取不出映射"的 bug，实测见 docs/compiler/README.md）
    sub = 0
    while sub < NSLOT:
        c = Chunk(f"sub_{sub // G2}", "sub")
        for j, k in enumerate(range(sub, min(sub + G2, NSLOT))):
            base = c.leaf(f"C{j}")
            m = c.leaf(f"m{k}")
            c.op(f"S{j}", "sub", [base, m])
        chunks.append(c)
        sub += G2

    # 硬判决
    c = Chunk("hard", "shr")
    a = c.leaf("C0")
    c.op("H0", "shr", [a])
    chunks.append(c)

    return chunks, acc_final


def run_chunk(arch, dot, ii, limit, workdir, edges=()):
    cmd = ["bash", os.path.join(os.path.dirname(os.path.abspath(__file__)), "map.sh"),
           arch, dot, str(ii), "ilp", str(limit)]
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    out = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=limit + 120).stdout
    mapped = "[map] mapped=yes" in out
    m = re.search(r"Solving Time \(sec\) : (\S+)", out)
    secs = float(m.group(1)) if m else None
    cost = re.search(r"Primal Bound       : \+(\S+)", out)
    cost = float(cost.group(1)) if cost else None
    # 驱动自己打印的表：[map] op NAME OPCODE CYCLE:NODE（CGRA-ME 的 outputMapping()
    # 对多扇出 val 会抛 map::at，不能用它来解析）
    used = re.findall(r"^\[map\] op (\w+) \S+ (\d+):(block_\d+_\d_)", out, re.M)
    where = {n: (int(c), b) for n, c, b in used}
    cycles = [c for c, _b in where.values()]
    pes = sorted({b for _c, b in where.values()})
    # 同拍依赖：本项目的 PE 只有 1 拍延迟的 link 寄存器，生产者与消费者落在同一拍
    # 就要求"组合逻辑跨 PE 直连"，在真机上不可实现（docs/architecture/cgra.md）
    same = sum(1 for s, d, _o in edges
               if where.get(s) and where.get(d) and where[s][0] == where[d][0])
    return dict(mapped=mapped, secs=secs, cost=cost, npe=len(pes),
                cycles=(max(cycles) + 1 if cycles else None), same=same, out=out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", default="cgra/arch/gpcp-8x8-mesh.xml")
    ap.add_argument("--out", default="build/bn_chunks")
    ap.add_argument("--ii", type=int, default=1)
    ap.add_argument("--limit", type=int, default=30, help="每片 ILP 时限（秒）")
    ap.add_argument("--skip-run", action="store_true", help="只生成 dot，不跑映射器")
    a = ap.parse_args()

    chunks, _acc = build_chunks()
    os.makedirs(a.out, exist_ok=True)

    rows = []
    total_secs = 0.0
    for c in chunks:
        path = os.path.join(a.out, c.name + ".dot")
        with open(path, "w") as f:
            f.write(c.dot())
        if a.skip_run:
            print(f"{c.name:8s} {c.kind:4s} ops={c.nops():2d} -> {path}")
            continue
        r = run_chunk(a.arch, path, a.ii, a.limit, a.out, c.edges)
        total_secs += r["secs"] or 0.0
        rows.append((c.name, c.kind, c.nops(), r))
        print(f"{c.name:8s} {c.kind:4s} ops={c.nops():2d} mapped={str(r['mapped']):5s} "
              f"{r['secs'] if r['secs'] is not None else -1:6.2f}s cost={r['cost']} "
              f"pe={r['npe']} cycles={r['cycles']} samecycle={r['same']}")

    if a.skip_run:
        print(f"\n共 {len(chunks)} 片，算子合计 {sum(c.nops() for c in chunks)}")
        return 0

    ok = sum(1 for _n, _k, _o, r in rows if r["mapped"])
    same = sum(r["same"] for _n, _k, _o, r in rows)
    print(f"\n映射成功 {ok}/{len(rows)} 片，同拍依赖边 {same} 条，求解总耗时 {total_secs:.1f}s，"
          f"算子数合计 {sum(o for _n, _k, o, _r in rows)}")
    csv = os.path.join(a.out, "summary.csv")
    with open(csv, "w") as f:
        f.write("chunk,kind,ops,mapped,secs,cost,pes,cycles,samecycle\n")
        for n, k, o, r in rows:
            f.write(f"{n},{k},{o},{int(r['mapped'])},{r['secs']},{r['cost']},"
                    f"{r['npe']},{r['cycles']},{r['same']}\n")
    print(f"汇总写入 {csv}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
