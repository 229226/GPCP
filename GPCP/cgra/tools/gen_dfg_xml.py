#!/usr/bin/env python3
# gen_dfg_xml.py -- 把我们的 kernel spec 翻译成 Morpher 兼容的 DFG XML
#
# 设计要点（来自对 Morpher DFG.cpp / CGRA_xml_compiler.cpp 的实测）：
#   1. <MutexBB> 必须存在，否则 FirstChildElement("MutexBB") 直接段错误。
#   2. 每个 <Node> 的 ASAP/ALAP 是 assert(!=-1) 的必填项 —— 我们用拓扑层级填。
#   3. <Inputs>/<Outputs>/<RecParents> 都是 assert 非空 —— 即使空也要写<RecParents></RecParents>。
#   4. <Output> 的 type="P" 必须带 NPB="0|1"；数据边用 type="I1"/"I2"。
#   5. 默认（非 lisa training）模式用 <Input>/<Output> 标签（不是 MyInput/MyOutput）。
#   6. LD/ST 若要 mapper 路由到 SPM，节点需 BasePointerName + GEPOffset，且 ADL 的
#      SPM 必须有 DATA_LAYOUT（变量名->SPM 地址）；否则 mapper 在 PathFinderMapper.cpp
#      用 Variable2SPMAddr 断言失败。
#
# 注意：本生成器只负责“产出合法 DFG XML”。能否被 mapper 接受，取决于目标 ADL 的
# 算子词表与端口命名（见 docs/compiler/README.md 的结论：我们的硬件 ADL 并不能被 mapper 接受）。
import argparse
import sys
import json

sys.path.insert(0, "cgra/tools")
sys.path.insert(0, "tools")
import kernel_spec as ks


# 目标 ADL 的算子词表。'gpcp' = 我们的硬件 ADL（LD/ST/...）；'hycube' = 原始 HyCUBE ADL。
OPMAPS = {
    "gpcp": {
        "load": "LD", "store": "ST", "add": "ADD", "sub": "SUB", "mul": "MUL",
        "mac": "MAC", "mov": "MOV", "shr": "SHR", "shl": "SHL", "and": "AND",
        "xor": "XOR", "max": "MAX", "min": "MIN", "lacc": "LACC", "macl": "MACL",
        "const": None,   # 我们的硬件无 MOVC；常量走 ADD/SUB/MAC 的立即数
    },
    "hycube": {
        "load": "OLOAD", "store": "OSTORE", "add": "ADD", "sub": "SUB", "mul": "MUL",
        "mac": "ADD", "mov": "MOV", "shr": "RS", "shl": "LS", "and": "AND",
        "xor": "XOR", "max": "MAX", "min": "MIN", "lacc": "ADD", "macl": "ADD",
        "const": "MOVC",
    },
}


def topo_levels(nodes):
    """返回 {idx: level}，level = 从源（无前驱）起的最长路径。"""
    level = {}
    # 反复松弛直到收敛
    for _ in range(len(nodes) + 2):
        changed = False
        for n in nodes:
            base = 0
            for (p, _operand) in n["preds"]:
                if p in level:
                    base = max(base, level[p] + 1)
                else:
                    base = 0  # 前驱未定，先放 0
                    break
            else:
                if level.get(n["idx"], -1) != base:
                    level[n["idx"]] = base
                    changed = True
        if not changed:
            break
    return level


def spec_to_nodes(spec, opmap):
    """把 kernel_spec 展开成 DFG 节点列表（含 load/store 节点）。

    每个节点: {idx, op, preds:[(src,operand)], succs, const, mem:(name,offset) or None}
    """
    leaves = spec.get("leaves", {})
    sinks = spec.get("sinks", {})
    ops = spec.get("ops", [])
    instances = spec.get("instances", 16)

    nodes = {}
    # 叶子 -> load 节点
    for name, info in leaves.items():
        bank = info.get("bank", 0) if isinstance(info, dict) else info
        mem = (name, int(bank) * 32)  # GEPOffset = bank*32（钩子：让 mapper 携带 {bank,word} 地址）
        nodes[name] = {
            "idx": name, "op": opmap["load"], "preds": [],
            "succs": [], "const": None, "mem": mem,
        }
    # 汇 -> store 节点
    for name, info in sinks.items():
        bank = info.get("bank", 0) if isinstance(info, dict) else info
        mem = (name, int(bank) * 32)
        nodes[name] = {
            "idx": name, "op": opmap["store"], "preds": [],
            "succs": [], "const": None, "mem": mem,
        }
    # 算子
    for op in ops:
        n = op["name"]
        mapped = opmap.get(op["op"], op["op"].upper())
        nodes[n] = {
            "idx": n, "op": mapped, "preds": [],
            "succs": [], "const": op.get("imm"), "mem": None,
        }
    # 连边
    for op in ops:
        n = op["name"]
        for operand, src in (("a", op.get("a")), ("b", op.get("b"))):
            if src is not None:
                if src not in nodes:
                    raise KeyError(f"算子 {n} 的操作数 {operand}={src} 在 spec 中不存在")
                nodes[n]["preds"].append((src, operand))
                nodes[src]["succs"].append(n)
    # 叶子/汇的 succs/preds 已在算子边里建立（叶子被算子消费，汇消费算子）

    # 重新编号 idx 为整数（mapper 要求 int idx）
    name2int = {name: i for i, name in enumerate(sorted(nodes))}
    out = []
    for name, n in nodes.items():
        out.append({
            "idx": name2int[name],
            "orig": name,
            "op": n["op"],
            "preds": [(name2int[p], o) for (p, o) in n["preds"]],
            "succs": [name2int[s] for s in n["succs"]],
            "const": n["const"],
            "mem": n["mem"],
        })
    # 用 orig 名回填 succs 的整数编号（上面用 name 找不到，需二次处理）
    intname = {v: k for k, v in name2int.items()}
    for n in out:
        n["succs"] = [name2int[s] for s in nodes[n["orig"]]["succs"]]
    return out, name2int


def emit_xml(nodes, name2int):
    level = topo_levels(nodes)
    maxlv = max(level.values()) if level else 0
    lines = []
    lines.append('<MutexBB>')
    lines.append('\t<BB1 name="_0_0">')
    lines.append('\t\t<BB2 name="_0_1"/>')
    lines.append('\t</BB1>')
    lines.append('\t<BB1 name="_0_1">')
    lines.append('\t\t<BB2 name="_0_0"/>')
    lines.append('\t</BB1>')
    lines.append('</MutexBB>')
    lines.append(f'<DFG count="{len(nodes)}">')
    intname = {v: k for k, v in name2int.items()}
    for n in sorted(nodes, key=lambda x: x["idx"]):
        lv = level.get(n["idx"], 0)
        asap = lv
        alap = maxlv  # 给 mapper 调度灵活度；ALAP>=ASAP 即可
        attrs = f'idx="{n["idx"]}" ASAP="{asap}" ALAP="{alap}" BB=""'
        if n["const"] is not None:
            attrs += f' CONST="{int(n["const"])}"'
        lines.append(f'<Node {attrs}>')
        lines.append(f'<OP>{n["op"]}</OP>')
        if n["mem"] is not None:
            memname, off = n["mem"]
            lines.append(f'<BasePointerName size="1">{memname}</BasePointerName>')
            lines.append(f'<GEPOffset>{off}</GEPOffset>')
        lines.append('<Inputs>')
        for (p, _o) in n["preds"]:
            lines.append(f'\t<Input idx="{p}"/>')
        lines.append('</Inputs>')
        lines.append('<Outputs>')
        for s in n["succs"]:
            lines.append(f'\t<Output idx="{s}" nextiter="0" type="I1"/>')
        lines.append('</Outputs>')
        lines.append('<RecParents>')
        lines.append('</RecParents>')
        lines.append('</Node>')
    lines.append('</DFG>')
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="kernel spec YAML")
    ap.add_argument("--out", required=True, help="输出 DFG XML")
    ap.add_argument("--opmap", choices=list(OPMAPS), default="gpcp")
    args = ap.parse_args()

    spec = ks.load_spec(args.spec)
    errs = ks.validate(spec)
    if errs:
        print("spec 校验失败:", file=sys.stderr)
        for e in errs:
            print("  ", e, file=sys.stderr)
        return 1

    opmap = OPMAPS[args.opmap]
    nodes, name2int = spec_to_nodes(spec, opmap)
    xml = emit_xml(nodes, name2int)
    with open(args.out, "w") as f:
        f.write(xml)
    print(f"wrote {args.out}: {len(nodes)} 节点, opmap={args.opmap}")
    # 打印编号映射，便于排查
    inv = {v: k for k, v in name2int.items()}
    print("  编号:", ", ".join(f"{inv[i]}={i}" for i in sorted(inv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
