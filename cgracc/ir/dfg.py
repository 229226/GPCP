"""DFG：架构无关的数据流图。

节点 = 一次算子求值；边 = 值依赖（输入可以是另一个节点的 id，或寄存器名 / 常量）。

循环用 `back_edges` 表达：`BackEdge(src, dst, distance)` 表示"第 i 次迭代的
dst 读第 i-distance 次迭代的 src"。回边**不**参与前向拓扑排序，因此 DFG 本身
仍然是 DAG；它只影响 modulo 调度的 RecMII 与合法性检查（R7）。

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
class BackEdge:
    src: str                          # 生产者的节点 id
    dst: str                          # 消费者的节点 id
    distance: int = 1                 # 迭代距离 d：dst 在第 i 次迭代读 src 的第 i-d 次


@dataclass(frozen=True)
class DFG:
    name: str
    nodes: tuple[Node, ...] = field(default_factory=tuple)
    back_edges: tuple[BackEdge, ...] = field(default_factory=tuple)
    trip_count: int = 1               # 循环迭代次数（非循环 kernel = 1）

    def node(self, nid: str) -> Node:
        for n in self.nodes:
            if n.id == nid:
                return n
        raise KeyError(f"DFG 里没有节点 '{nid}'")

    def back_edge_pairs(self) -> set[tuple[str, str]]:
        """(src, dst) 集合：这些输入不是前向边。"""
        return {(e.src, e.dst) for e in self.back_edges}

    def forward_edges(self) -> dict[str, list[str]]:
        ids = {n.id for n in self.nodes}
        back = self.back_edge_pairs()
        succ: dict[str, list[str]] = {n.id: [] for n in self.nodes}
        for n in self.nodes:
            for src in n.inputs:
                if src in ids and (src, n.id) not in back:
                    succ[src].append(n.id)
        return succ

    def components(self) -> dict[str, str]:
        """按节点间依赖（含回边）做连通分量：私有寄存器堆的机器上，
        同一分量的节点必须落在同一个执行单元。"""
        ids = {n.id for n in self.nodes}
        parent = {n: n for n in ids}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for n in self.nodes:
            for src in n.inputs:
                if src in ids:
                    ra, rb = find(src), find(n.id)
                    if ra != rb:
                        parent[rb] = ra
        return {n: find(n) for n in ids}

    def topo_order(self) -> list[Node]:
        succ = self.forward_edges()
        indeg = {n.id: 0 for n in self.nodes}
        for src, outs in succ.items():
            for s in outs:
                indeg[s] += 1
        ready = [nid for nid, d in indeg.items() if d == 0]
        by_id = {n.id: n for n in self.nodes}
        out: list[Node] = []
        while ready:
            nid = ready.pop(0)
            out.append(by_id[nid])
            for s in succ[nid]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    ready.append(s)
        if len(out) != len(self.nodes):
            raise ValueError(f"DFG '{self.name}' 的前向边有环（循环请用 back_edges）")
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
    back = tuple(
        BackEdge(str(e["src"]), str(e["dst"]), int(e.get("distance", 1)))
        for e in raw.get("back_edges", ())
    )
    for e in back:
        if e.distance < 1:
            raise ValueError(f"回边 {e.src}->{e.dst} 的 distance 必须 ≥ 1")
        if e.src not in ids or e.dst not in ids:
            raise ValueError(f"回边 {e.src}->{e.dst} 引用了不存在的节点")
    return DFG(name=str(raw["name"]), nodes=nodes, back_edges=back,
               trip_count=int(raw.get("trip_count", 1)))
