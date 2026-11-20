#!/usr/bin/env python3
"""硬件目标描述层（AGENTS.md 4.19）：kernel YAML 与物理 CGRA 之间唯一的换算点。

    cgra/target.yaml   -- 阵列几何 / spad 编址 / ISA / 命名区域（硬件真相）
    cgra/kernels/*.yaml -- 逻辑地址：region 名、slot 表达式、pe_per_bank
    target.lower()      -- 逻辑地址 -> (base, stride) flat spad 坐标

下层（rule_map / kernel_spec / gen_kernel_prog）只看 lower 出来的 (base, stride)，
不直接引用 16 / 32 / 8 这类硬件数字。换 target 时 YAML 不用改。
"""
import ast
import os
import threading

try:
    import yaml
except ImportError:                                       # pragma: no cover
    yaml = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DEFAULT_PATH = os.path.join(ROOT, "cgra", "target.yaml")

# 扁平 checkout（没有 cgra/target.yaml）时的兜底，数值与 4x4 v1 一致。
FALLBACK = {
    "name": "gpcp-cgra-4x4-v1-fallback",
    "array": {"rows": 4, "cols": 4},
    "pe": {"regs": 8},
    "spad": {"banks": 32, "words_per_bank": 32, "bank_bits": 5,
             "word_bits": 5, "flat_max": 1023, "host_window": 0xA000},
    "isa": {
        "ops": {"nop": 0, "mov": 1, "add": 2, "sub": 3, "mul": 4, "mac": 5,
                "lacc": 6, "shr": 7, "shl": 8, "and": 9, "xor": 10,
                "max": 11, "min": 12, "macl": 13, "ld": 14, "st": 15},
        "src": {"r0": 0, "r1": 1, "r2": 2, "r3": 3, "r4": 4, "r5": 5,
                "r6": 6, "r7": 7, "n": 8, "e": 9, "s": 10, "w": 11,
                "zero": 12, "imm": 13, "acc": 14, "link": 15},
    },
    "imem": {"max_cycles": 512},
    "timing": {"load": "combinational", "store": "synchronous",
               "default_src": "zero"},
    "regions": {"IN": 0, "CONST": 1, "DATA": 2, "REF": 8, "MIN1": 10,
                "MIN2": 11, "SIGN": 12, "OUT": 16, "LLR": 24,
                "SCRATCH": 24, "HARD": 25, "SENTINEL": 30},
}

_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Constant,
            ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod,
            ast.USub, ast.UAdd, ast.Load)


def eval_expr(expr, symbols):
    """安全求值一个逻辑地址表达式（只允许名字与 + - * // % 运算）。"""
    if isinstance(expr, bool):
        raise ValueError(f"布尔值不是地址表达式：{expr!r}")
    if isinstance(expr, (int, float)):
        return int(expr)
    if not isinstance(expr, str):
        raise ValueError(f"非法地址表达式：{expr!r}")
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"地址表达式含不允许的语法：{expr!r}")
        if isinstance(node, ast.Name) and node.id not in symbols:
            raise ValueError(f"地址表达式引用了未定义符号 {node.id!r}（{expr!r}）")
    return int(eval(compile(tree, "<logical-addr>", "eval"),
                    {"__builtins__": {}}, symbols))


class Target:
    def __init__(self, d):
        self.name = str(d.get("name", "unknown"))
        a, p, s = d["array"], d["pe"], d["spad"]
        self.rows, self.cols = int(a["rows"]), int(a["cols"])
        self.npes = self.rows * self.cols
        self.regs = int(p["regs"])
        self.banks = int(s["banks"])
        self.words = int(s["words_per_bank"])          # W
        self.word_bits = int(s.get("word_bits", self.words.bit_length() - 1))
        self.bank_bits = int(s.get("bank_bits", self.banks.bit_length() - 1))
        self.flat_max = int(s.get("flat_max", (1 << (self.word_bits + self.bank_bits)) - 1))
        self.host_window = int(s.get("host_window", 0xA000))
        self.ops = dict(d["isa"]["ops"])
        self.src = dict(d["isa"]["src"])
        self.max_cycles = int(d["imem"]["max_cycles"])
        self.timing = dict(d.get("timing", {}))
        self.regions = {str(k): int(v) for k, v in d["regions"].items()}
        if self.words != 1 << self.word_bits:
            raise ValueError("words_per_bank 必须是 2 的幂")
        if self.banks > 1 << self.bank_bits:
            raise ValueError("bank_bits 装不下 banks")
        if any(not (0 <= v < self.banks) for v in self.regions.values()):
            raise ValueError("regions 里有越界的 bank 号")

    def symbols(self, pe=0, k=0, extra=None):
        d = dict(self.regions)
        d.update(W=self.words, WORDS=self.words, NPES=self.npes,
                 BANKS=self.banks, REGS=self.regs, pe=pe, k=k, SHIFT=0)
        if extra:
            d.update(extra)
        return d

    def eval(self, expr, pe=0, k=0, extra=None):
        return eval_expr(expr, self.symbols(pe, k, extra))

    def region(self, name, k=0, extra=None):
        """解析 bank 字段：符号名 / 「名字 + k」 / 纯数字。"""
        return self.eval(name, 0, k, extra)

    def flat(self, at, pe, k=0, extra=None):
        """逻辑地址 -> 该 PE 上的 flat spad 下标。

        at:
          region: IN            # 命名区域（可写表达式，如 "IN + k"）
          slot: "2*k"           # 区域内的逻辑字位（可含 k / SHIFT / pe）
          pe_per_bank: 2        # 一个 bank 容纳几个 PE 的数据块；
                                # W/g 就是每个 PE 占的字数，S = W/g
          broadcast: true       # 可选：所有 PE 读同一个字（stride=0）
        """
        W = self.words
        g = int(at.get("pe_per_bank", 1))
        region = self.eval(at.get("region", "IN"), pe, k, extra)
        slot = self.eval(at.get("slot", 0), pe, k, extra)
        if at.get("broadcast") or g == 0:
            return region * W + slot
        if g <= 0 or W % g:
            raise ValueError(f"pe_per_bank={g} 必须整除 words_per_bank={W}")
        return (region + pe // g) * W + slot + (W // g) * (pe % g)

    def lower(self, at, k=0, extra=None):
        """按 target 对每个 PE 采样求值，仿射化成现有 (base, stride) 接口。"""
        vals = [self.flat(at, pe, k, extra) for pe in range(self.npes)]
        base = vals[0]
        stride = vals[1] - vals[0] if self.npes > 1 else 0
        if stride < 0 or any(v != base + stride * i for i, v in enumerate(vals)):
            raise ValueError(f"逻辑地址对 pe 不是仿射的：{vals[:4]} ... {at!r}")
        return base, stride


_CACHE = {}
_LOCK = threading.Lock()


def load(path=None):
    p = path or DEFAULT_PATH
    with _LOCK:
        if p in _CACHE:
            return _CACHE[p]
        d = {k: dict(v) if isinstance(v, dict) else v for k, v in FALLBACK.items()}
        if yaml is not None and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
            for key, val in user.items():
                d[key] = dict(val) if isinstance(val, dict) else val
        t = Target(d)
        _CACHE[p] = t
        return t


def hw():
    """给 kernel_spec / image_spec 等模块用的默认 target 句柄。"""
    return load()


if __name__ == "__main__":
    t = load()
    print(f"target={t.name} npes={t.npes} W={t.words} banks={t.banks} "
          f"regs={t.regs} imem={t.max_cycles} regions={len(t.regions)}")
    for at in ({"region": "IN", "slot": 0, "pe_per_bank": 1},
               {"region": "REF", "slot": "k", "pe_per_bank": 1},
               {"region": "IN", "slot": "2*k", "pe_per_bank": 2},
               {"region": "SCRATCH", "slot": 0, "pe_per_bank": 16},
               {"region": "SENTINEL", "slot": 0, "broadcast": True}):
        print(at, "->", t.lower(at))
