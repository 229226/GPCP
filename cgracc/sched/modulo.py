"""modulo 调度器（软件流水）。

思路（Rau 的迭代模调度，简化版）：
  1. 下界 MII = max(ResMII, RecMII)
       ResMII = ceil(节点数 / 单元数)
       RecMII = max over 回边 ceil(环上总延迟 / 距离)
  2. 从 II=MII 开始尝试；按关键路径高度排序，逐个放到"依赖满足 + 资源空闲"的
     最早一拍。资源表按 `cycle % II` 取模，所以第 i 次迭代与第 i+1 次迭代会
     自动共用同一个 kernel 槽位。
  3. 放不下就 II+1 重来，直到成功或超过上限。

产出：`Schedule(ii=II, run_cycles=(trip-1)*II + span)`。
合法性由 verify.legality.check 复核（R4/R6 按 II 投影，R7 检查回边）。
"""
from __future__ import annotations

from collections import defaultdict
from math import ceil

from ..ir.dfg import DFG
from ..ir.model import MachineModel
from ..ir.schedule import Schedule, Slot
from .base import Scheduler


class _Fail(Exception):
    """在当前 II 下排不出合法调度。"""


class ModuloScheduler(Scheduler):
    name = "modulo"

    def __init__(self, ii_cap: int | None = None, ii_min: int | None = None):
        self.ii_cap = ii_cap            # 手动限制最大 II（None = 8*MII）
        self.ii_min = ii_min            # 强制从某个 II 起试（搜索器用来枚举候选）

    def mii(self, dfg: DFG, model: MachineModel) -> int:
        return max(1, self._res_mii(dfg, model), self._rec_mii(dfg, model))

    def schedule(self, dfg: DFG, model: MachineModel) -> Schedule:
        mii = self.mii(dfg, model)
        start = max(mii, self.ii_min or 0)
        cap = self.ii_cap or 8 * mii
        cap = max(cap, start)
        cap = min(cap, model.time.max_run_cycles)
        for ii in range(start, cap + 1):
            try:
                return self._try_ii(dfg, model, ii)
            except _Fail:
                continue
        raise RuntimeError(f"{dfg.name}: II 从 {start} 试到 {cap} 仍排不出合法调度")

    # ---------------- MII ----------------
    @staticmethod
    def _res_mii(dfg: DFG, model: MachineModel) -> int:
        return ceil(len(dfg.nodes) / model.compute.units)

    def _rec_mii(self, dfg: DFG, model: MachineModel) -> int:
        if not dfg.back_edges:
            return 1
        nodes = {n.id: n for n in dfg.nodes}
        succ = dfg.forward_edges()
        lat = {nid: model.compute.op(n.op).latency for nid, n in nodes.items()}

        best = 1
        for be in dfg.back_edges:
            if be.src == be.dst:
                delay = lat[be.src]
            else:
                delay = self._longest_path(nodes, succ, lat, be.dst, be.src)
                if delay is None:      # 不是真回环，只是跨迭代输入
                    continue
            best = max(best, ceil(delay / be.distance))
        return best

    @staticmethod
    def _longest_path(nodes, succ, lat, start: str, target: str) -> int | None:
        """start -> target 的前向最长延迟（含两端节点）；无路径返回 None。"""
        memo: dict[str, int | None] = {}

        def rec(u: str) -> int | None:
            if u == target:
                return lat[u]
            if u in memo:
                return memo[u]
            best = None
            for s in succ.get(u, ()):
                v = rec(s)
                if v is not None:
                    best = v if best is None else max(best, v)
            memo[u] = None if best is None else lat[u] + best
            return memo[u]

        return rec(start)

    # ---------------- 固定 II 的调度 ----------------
    def _try_ii(self, dfg: DFG, model: MachineModel, ii: int) -> Schedule:
        nodes = {n.id: n for n in dfg.nodes}
        lat = {nid: model.compute.op(n.op).latency for nid, n in nodes.items()}
        succ = dfg.forward_edges()
        back = dfg.back_edge_pairs()

        # 关键路径高度：先排"离出口最远"的节点（标准 modulo 排序）
        hmemo: dict[str, int] = {}

        def height(u: str) -> int:
            if u in hmemo:
                return hmemo[u]
            best = 0
            for s in succ.get(u, ()):
                best = max(best, height(s))
            hmemo[u] = lat[u] + best
            return hmemo[u]

        order = sorted(dfg.nodes, key=lambda n: -height(n.id))

        t: dict[str, int] = {}
        placed_at: dict[str, tuple[int, str | None, int | None]] = {}
        issue: dict[tuple[int, int], int] = defaultdict(int)
        rd: dict[tuple[int, str, int], int] = defaultdict(int)
        wr: dict[tuple[int, str, int], int] = defaultdict(int)

        in_back: dict[str, list[tuple[str, int]]] = defaultdict(list)
        out_back: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for be in dfg.back_edges:
            in_back[be.dst].append((be.src, be.distance))
            out_back[be.src].append((be.dst, be.distance))

        for n in order:
            if n.op not in model.compute.ops:
                raise RuntimeError(f"算子 '{n.op}' 不在模型 '{model.name}' 里")

            earliest = 0
            for src in n.inputs:
                if src in t and (src, n.id) not in back:
                    earliest = max(earliest, t[src] + lat[src])
            for src, d in in_back[n.id]:
                if src in t:
                    earliest = max(earliest, t[src] + lat[src] - d * ii)

            latest: int | None = None
            for dst, d in out_back[n.id]:
                if dst in t:
                    bound = t[dst] + d * ii - lat[n.id]
                    latest = bound if latest is None else min(latest, bound)

            space = n.space
            if space is None and "mem" in model.compute.op(n.op).writes:
                space = next(iter(model.store), None)
            addr = n.imm if space is not None else None
            bank = (model.store[space].bank_of(addr)
                    if space in model.store and addr is not None else None)
            is_write = "mem" in model.compute.op(n.op).writes

            placed: tuple[int, int] | None = None
            horizon = 4 * ii + model.compute.units + 4
            for cand in range(earliest, earliest + horizon):
                if latest is not None and cand > latest:
                    break
                slot = cand % ii
                unit = self._free_unit(slot, model, issue)
                if unit is None:
                    continue
                if bank is not None:
                    key = (slot, space, bank)
                    table = wr if is_write else rd
                    cap = (model.store[space].write_ports_per_bank if is_write
                           else model.store[space].read_ports_per_bank)
                    if table[key] >= cap:
                        continue
                placed = (cand, unit)
                break
            if placed is None:
                raise _Fail(f"节点 '{n.id}' 在 II={ii} 下放不下")

            cyc, unit = placed
            t[n.id] = cyc
            placed_at[n.id] = (unit, space, addr)
            issue[(unit, cyc % ii)] += 1
            if bank is not None:
                (wr if is_write else rd)[(cyc % ii, space, bank)] += 1

        span = max(t.values(), default=-1) + 1
        total = (max(dfg.trip_count, 1) - 1) * ii + span
        if total > model.time.max_run_cycles:
            raise _Fail(f"循环总周期 {total} 超过 run 上限 {model.time.max_run_cycles}")

        slots = []
        for n in dfg.nodes:
            unit, space, addr = placed_at[n.id]
            slots.append(Slot(node=n.id, op=n.op, unit=unit, cycle=t[n.id],
                              dst=n.dst, addr=addr, space=space))
        return Schedule(name=dfg.name, run_cycles=total, slots=tuple(slots), ii=ii)

    @staticmethod
    def _free_unit(slot: int, model: MachineModel,
                   issue: dict[tuple[int, int], int]) -> int | None:
        for unit in range(model.compute.units):
            if issue[(unit, slot)] < model.compute.issue_per_unit_per_cycle:
                return unit
        return None
