"""DFG：架构无关的数据流图。

节点 = 一次算子求值；边 = 值依赖（输入可以是另一个节点的 id，或寄存器名 / 常量）。
这一层不允许出现任何物理地址、bank、拍号——那些属于 Schedule。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Node:
    id: str
    op: str
    inputs: tuple[str, ...] = ()
    imm: int | None = None            # 立即数（LD/ST 的地址、移位的位数）
    dst: str | None = None            # 期望写回的位置：rN / acc / link；None = 由调度器分配
    space: str | None = None          # 逻辑存储空间名（架构无关，物理地址由调度器决定）


@dataclass(frozen=True)
class DFG:
    name: str
    nodes: tuple[Node, ...] = field(default_factory=tuple)

    def node(self, nid: str) -> Node:
        for n in self.nodes:
            if n.id == nid:
                return n
        raise KeyError(f"DFG 里没有节点 '{nid}'")

    def producers(self) -> dict[str, str]:
        """node id -> 生产它的节点 id（只统计节点间依赖）。"""
        ids = {n.id for n in self.nodes}
        prod: dict[str, str] = {}
        for n in self.nodes:
            for src in n.inputs:
                if src in ids:
                    prod[src] = n.id
        return prod

    def topo_order(self) -> list[Node]:
        ids = {n.id for n in self.nodes}
        indeg: dict[str, int] = {n.id: 0 for n in self.nodes}
        succ: dict[str, list[str]] = {n.id: [] for n in self.nodes}
        for n in self.nodes:
            for src in n.inputs:
                if src in ids:
                    indeg[n.id] += 1
                    succ[src].append(n.id)
        ready = [nid for nid, d in indeg.items() if d == 0]
        out: list[Node] = []
        by_id = {n.id: n for n in self.nodes}
        while ready:
            nid = ready.pop(0)
            out.append(by_id[nid])
            for s in succ[nid]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    ready.append(s)
        if len(out) != len(self.nodes):
            raise ValueError(f"DFG '{self.name}' 有环，无法调度")
        return out


def load_dfg(path: str | Path) -> DFG:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    nodes = tuple(
        Node(
            id=str(n["id"]),
            op=str(n["op"]),
            inputs=tuple(n.get("inputs", ())),
            imm=n.get("imm"),
            dst=n.get("dst"),
            space=n.get("space"),
        )
        for n in raw["nodes"]
    )
    ids = [n.id for n in nodes]
    if len(ids) != len(set(ids)):
        raise ValueError(f"DFG '{raw['name']}' 有重复节点 id")
    return DFG(name=str(raw["name"]), nodes=nodes)
