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
    """bank 的三种写法 → int 列表：
       int        -> [int]
       "lo..hi"   -> list(range(lo, hi+1))
       [a, b, ..] -> 递归展开
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
            lo, hi = expr.split("..")
            return list(range(int(lo), int(hi) + 1))
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
        if o not in OPS:
            errs.append(f"op {n!r} 用了非法算子 {o!r}（合法：{sorted(OPS)}）")
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

    if kind == "dfg":
        leaves = set(expand_mapping(spec.get("leaves", {})).keys())
        allnames = op_names | leaves
        for op in spec.get("ops", []):
            for ref in (op.get("a"), op.get("b")):
                if ref is None:
                    continue
                if ref not in allnames:
                    errs.append(f"op {op['name']!r} 引用了未声明的名字 {ref!r}")
            if op.get("name") in leaves:
                errs.append(f"op 名字 {op['name']!r} 与叶子名冲突")
    return errs


# ---------------------------------------------------------------- 构造 DFG
def build_dfg(spec):
    """spec → rule_map.DFG，只构图不调度（dfg 路径用；bn/cn 不走这里）。"""
    d = rm.DFG()
    leaves = expand_mapping(spec.get("leaves", {}))
    for name in leaves:
        # 叶子无前驱；给占位 opcode，schedule 会跳过无前驱节点、只当 LD 源
        d.node(name, "add")
    for op in spec.get("ops", []):
        n = op["name"]
        d.node(n, op["op"])
        a = op.get("a")
        b = op.get("b")
        if a is not None:
            d.edge(a, n, 0)
        if b is not None:
            d.edge(b, n, 1)
    return d


def resolve_out(spec, path, root):
    out = spec.get("out")
    if out:
        return out if os.path.isabs(out) else os.path.join(root, out)
    name = spec.get("name", os.path.splitext(os.path.basename(path))[0])
    return os.path.join(root, "sw", "soc", f"{name}_prog.h")
