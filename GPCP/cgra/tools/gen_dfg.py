#!/usr/bin/env python3
"""生成 CGRA-ME 可直接读取的 DFG（dot 方言：属性值不加引号，见 docs/compiler/README.md）。

目前只有一类：N 抽头 FIR 的加法树
    y = sum_{k<N} mul_k(x_k, c_k)          —— N 个 mul + (N-1) 个 add = 2N-1 个算子
输入/输出节点故意不生成：CGRA-ME 这个版本对 `input=1` 节点 feed 算子判 infeasible
（实测见 docs/compiler/README.md），本项目的真实 DFG 后面用 load/store 算子对接 spad。

    python3 cgra/tools/gen_dfg.py fir 16 cgra/dfg/fir16.dot
"""
import sys


def close(text, last):
    """把最后一个 val 接到一个自环 sink 上，使 DFG 没有悬空输出。

    CGRA-ME 的退火映射器（AnnealMapper::routeVal）遇到 output.size()==0 的 val
    会段错误（实测，见 docs/compiler/README.md）；ILP 不受影响。sink 只有 1 条自环边，
    II=1 时正好走 PE 内部寄存器（1 拍延迟后回绕），不引入额外时间约束。
    """
    body = text.rstrip()
    assert body.endswith("}")
    body = body[:-1]
    return (body + f"  sink [opcode=add];\n"
                   f"  {last} -> sink [operand=0];\n"
                   f"  sink -> sink [operand=1];\n}}\n")


def fir(n):
    """返回 (dot 文本, 最后一个算子名, 算子数)。"""
    lines = ["digraph {"]
    for k in range(n):
        lines.append(f"  mul{k} [opcode=mul];")
    level = [f"mul{k}" for k in range(n)]
    add = 0
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            if i + 1 == len(level):
                nxt.append(level[i])
                continue
            name = f"add{add}"
            add += 1
            lines.append(f"  {name} [opcode=add];")
            lines.append(f"  {level[i]} -> {name} [operand=0];")
            lines.append(f"  {level[i + 1]} -> {name} [operand=1];")
            nxt.append(name)
        level = nxt
    lines.append("}")
    return "\n".join(lines) + "\n", level[0], 2 * n - 1


def main():
    if len(sys.argv) < 4 or sys.argv[1] != "fir":
        print(__doc__)
        return 2
    n = int(sys.argv[2])
    text, last, ops = fir(n)
    if len(sys.argv) > 4 and sys.argv[4] == "--close":
        text = close(text, last)
        ops += 1
    with open(sys.argv[3], "w") as f:
        f.write(text)
    print(f"wrote {sys.argv[3]}: fir{n}, {ops} ops")
    return 0


if __name__ == "__main__":
    sys.exit(main())
