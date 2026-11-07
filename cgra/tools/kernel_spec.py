#!/usr/bin/env python3
"""kernel_spec: 读 YAML kernel 配置 → 校验 → 构造 rule_map.DFG（只构图，不调度）。

设计（见 docs/18-微码自动化.md）：
- 既有 BN/CN 用 `kind: bn` / `kind: cn` 直连 rule_map 里已验证的调度器
  （schedule_bn_split / schedule_cn），保证与仓库现有产物逐字节一致；
- 全新 kernel 用 `kind: dfg`，由 build_dfg() 构造 rule_map.DFG，
  再交给 rule_map.schedule() 通用调度（bank/路由的硬编码留给 L2 消除）。

铁律：只复用 cgra.enc / OPS / SRC / spad_idx，禁止第二套编码表（开发计划书 §3）。
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from cgra import OPS, NPES, enc, spad_idx   # noqa: E402
import rule_map as rm                         # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None


def load_spec(path):
    if yaml is None:
        raise SystemExit("需要 PyYAML：pip install pyyaml")
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------- bank 写法展开
def _expand_range(expr):
    """bank/地址的三种写法 → int 列表：
       int          -> [int]
       "lo..hi"     -> list(range(lo, hi+1))
       "lo..hi:step"-> list(range(lo, hi+1, step))   （flat 地址常用步长 2）
       [a, b, ..]   -> 递归展开
    """
    if isinstance(expr, int):
        return [expr]
    if isinstance(expr, list):
        out = []
        for v in expr:
            out.extend(_expand_range(v))
        return out
    if isinstance(expr, str):
        if ".." in expr:
            rng, _, step = expr.partition(":")
            lo, hi = rng.split("..")
            return list(range(int(lo), int(hi) + 1, int(step) if step else 1))
        return [int(expr)]
    raise ValueError(f"非法 bank 写法: {expr!r}")


def _expand_keys(expr):
    """key 的两种写法 → 名字列表：
       "m0"       -> ["m0"]
       "m0..m22"  -> ["m0".."m22"]（前缀 + 数字范围）
    """
    if not isinstance(expr, str):
        raise ValueError(f"非法名字：{expr!r}")
    if ".." in expr:
        left, right = expr.split("..", 1)
        ml = re.match(r"^(.*?)(\d+)$", left)
        mr = re.match(r"^(.*?)(\d+)$", right)
        if not ml or not mr:
            raise ValueError(f"范围 key 必须形如 name0..name9：{expr!r}")
        prefix = ml.group(1)
        if mr.group(1) != prefix:
            raise ValueError(f"范围 key 两端前缀不一致：{expr!r}")
        start, end = int(ml.group(2)), int(mr.group(2))
        step = 1 if end >= start else -1
        return [f"{prefix}{i}" for i in range(start, end + step, step)]
    return [expr]


def expand_mapping(mapping):
    """{key: {bank: X}} → {name: bank(int|None)}，key 与 bank 并行展开且长度一致。

    bank 缺省（写法 `{}` 或不写）时值为 None，表示「交给 bank_alloc 自动分配」（L2-A）。
    """
    out = {}
    for k, v in mapping.items():
        bank_expr = v.get("bank") if isinstance(v, dict) else v
        names = _expand_keys(k)
        if bank_expr is None:
            for name in names:
                if name in out:
                    raise ValueError(f"名字重复：{name!r}")
                out[name] = None
            continue
        banks = _expand_range(bank_expr)
        if len(names) != len(banks):
            raise ValueError(
                f"key {k!r} 展开 {len(names)} 个名字，但 bank 展开 {len(banks)} 个，长度不一致")
        for name, bank in zip(names, banks):
            if name in out:
                raise ValueError(f"名字重复：{name!r}")
            out[name] = bank
    return out


def expand_addr_mapping(mapping):
    """{key: {addr: {base: X, stride: S}}} → {name: (base, stride)}。

    寻址公式：spad 平坦地址 = base + stride*instance（instance = PE 号）。
    - `stride: 0` 表示所有 PE 共享同一个字（广播系数，如 FIR 抽头 / 导频）；
    - `stride: 2` 表示每个 PE 往后挪 2 个字（PRACH 的实虚交错 y 窗口）；
    - base 支持单值 / "lo..hi" / "lo..hi:step" / 列表，与名字并行展开。
    带 addr 的叶子/汇不参与 bank 分配（地址是显式平坦地址）。
    """
    out = {}
    for k, v in mapping.items():
        if not isinstance(v, dict) or "addr" not in v:
            continue
        a = v["addr"]
        bases = _expand_range(a.get("base", 0))
        stride = int(a.get("stride", 1))
        names = _expand_keys(k)
        if len(names) != len(bases):
            raise ValueError(
                f"key {k!r} 展开 {len(names)} 个名字，但 addr.base 展开 {len(bases)} 个，长度不一致")
        for name, base in zip(names, bases):
            if name in out:
                raise ValueError(f"名字重复：{name!r}")
            out[name] = (int(base), stride)
    return out


# ---------------------------------------------------------------- 校验
IMM_OPS = {"shr", "shl", "ld", "st"}


def validate(spec):
    """返回错误字符串列表，空表示合法。"""
    errs = []
    if not isinstance(spec, dict):
        return ["spec 顶层必须是映射"]
    kind = spec.get("kind", "dfg")
    if kind not in ("bn", "cn", "dfg"):
        errs.append(f"未知 kind: {kind!r}（应为 bn/cn/dfg）")
    if kind == "dfg" and "ops" not in spec:
        errs.append("dfg 类型必须含 ops")

    op_names = set()
    for i, op in enumerate(spec.get("ops", [])):
        if not isinstance(op, dict) or "name" not in op or "op" not in op:
            errs.append(f"ops[{i}] 必须有 name 与 op")
            continue
        n, o = op["name"], op["op"]
        op_names.add(n)
        if o not in OPS and o != "neg":     # neg = 伪算子，降为 sub a=zero b=x
            errs.append(f"op {n!r} 用了非法算子 {o!r}（合法：{sorted(OPS)} + neg）")
        if o not in IMM_OPS and op.get("imm", 0):
            errs.append(f"op {n!r}({o}) 不允许非零 imm（仅 shr/shl/ld/st 允许）")

    for field in ("leaves", "sinks"):
        m = spec.get(field, {})
        if not isinstance(m, dict):
            errs.append(f"{field} 必须是映射")
            continue
        try:
            expanded = expand_mapping(m)
        except ValueError as e:
            errs.append(f"{field}: {e}")
            continue
        for name, bank in expanded.items():
            if bank is not None and not (0 <= bank <= 31):
                errs.append(f"{field} {name} 的 bank {bank} 越界（必须 0..31）")
        try:
            addrs = expand_addr_mapping(m)
        except ValueError as e:
            errs.append(f"{field}: {e}")
            continue
        inst = int(spec.get("instances", 16))
        for name, (base, stride) in addrs.items():
            if expanded.get(name) is not None:
                errs.append(f"{field} {name} 同时写了 bank 与 addr（只能二选一）")
            if base < 0 or stride < 0:
                errs.append(f"{field} {name} 的 addr base/stride 不能为负")
            elif base + stride * (inst - 1) > 1023:
                errs.append(f"{field} {name} 的 addr 越界："
                            f"{base}+{stride}*{inst - 1} > 1023（imm 只有 10 位）")

    if kind == "dfg":
        leaves = set(expand_mapping(spec.get("leaves", {})).keys())
        allnames = op_names | leaves
        referenced = set()
        for op in spec.get("ops", []):
            for ref in (op.get("a"), op.get("b")):
                if ref is None:
                    continue
                referenced.add(ref)
                if ref not in allnames:
                    errs.append(f"op {op['name']!r} 引用了未声明的名字 {ref!r}")
            if op.get("name") in leaves:
                errs.append(f"op 名字 {op['name']!r} 与叶子名冲突")
        # sinks 里声明的名字必须真的是「没有后继的算子」，否则地址会静默落到分配器
        actual_sinks = op_names - referenced
        declared = set(expand_mapping(spec.get("sinks", {})).keys())
        for name in sorted(declared - actual_sinks):
            errs.append(f"sinks 里的 {name!r} 不是 DFG 的汇（它还有后继 / 不是算子名）")
    return errs


# ---------------------------------------------------------------- 构造 DFG
def build_dfg(spec):
    """spec → rule_map.DFG，只构图不调度（dfg 路径用；bn/cn 不走这里）。"""
    d = rm.DFG()
    leaves = expand_mapping(spec.get("leaves", {}))
    for name in leaves:
        # 叶子无前驱；给占位 opcode，schedule 会跳过无前驱节点、只当 LD 源
        d.node(name, "add")
    # flat 地址（base + stride*instance）：长数组/广播系数走这里，不用 bank
    d.addr.update(expand_addr_mapping(spec.get("leaves", {})))
    for op in spec.get("ops", []):
        n = op["name"]
        # imm 仅 shr/shl 使用（Q15 缩放）；op.get("imm", 0) 缺省即 0
        d.node(n, op["op"], imm=op.get("imm", 0))
        a = op.get("a")
        b = op.get("b")
        if a is not None:
            d.edge(a, n, 0)
        if b is not None:
            d.edge(b, n, 1)
    return d


def resolve_out(spec, path, root):
    """输出路径：spec.out > <root>/sw/soc > <脚本目录>/sw/soc > 脚本目录。

    后两级是为了让本脚本在「扁平 checkout」（kernel/*.yaml 与 tools 同目录、
    没有 cgra/ 与 sw/ 的镜像仓库）里也能直接跑——见 docs/18 §L2-A 备注。
    """
    out = spec.get("out")
    if out:
        if os.path.isabs(out):
            return out
        canonical_out = os.path.join(root, out)
        if os.path.isdir(os.path.dirname(canonical_out)):
            return canonical_out
        local_out = os.path.join(HERE, out)
        if os.path.isdir(os.path.dirname(local_out)):
            return local_out
        return canonical_out
    name = spec.get("name", os.path.splitext(os.path.basename(path))[0])
    fname = f"{name}_prog.h"
    canonical = os.path.join(root, "sw", "soc")
    if os.path.isdir(canonical):
        return os.path.join(canonical, fname)
    local = os.path.join(HERE, "sw", "soc")
    if os.path.isdir(local):
        return os.path.join(local, fname)
    return os.path.join(HERE, fname)
