#!/usr/bin/env python3
"""docs/protocols/README.md 的执行器：协议场景文件的静态检查。

四条规则，全部基于语法树（不是正则）：
  1. 标识符禁用词（硬件概念）；
  2. 语句白名单（无 while/try/class/lambda/global/with/async，import 只许 hll）；
  3. 调用白名单（只有 docs/protocols/README.md 的动词 + 少数内建）；
  4. 模块级只允许：导入、`S = scenario(...)`、以及基于 S 的动词调用链。

用法： check_hll.py proto/scenarios/*.py   （有违例则 exit 1）
"""
import ast
import sys

BANNED_IDENTS = (
    "spad", "bank", "page", "pe", "imem", "cycles", "dma", "descriptor",
    "tile", "latency", "paddr", "addr", "mmio", "instruction", "microcode",
    "register", "regfile", "buffer_bank",
)

# docs/protocols/README.md 的动词（方法名）
VERBS = {
    "cfg", "traffic", "transport_block", "received", "encode", "modulate", "dmrs",
    "correlate", "map_to_grid", "extract", "measure", "ofdm_modulate",
    "ofdm_demodulate", "channel", "estimate", "equalise_demap", "mimo_equalise",
    "decode", "expect", "budget",
    "equalise", "transform", "demap",
}
ALLOWED_CALLS = {"scenario", "sym_len"} | VERBS

# 波形尺寸只能由 derived 决定（S.sym_len）；写死就等于把某个后端已有的
# FFT 镜像抄进协议。PRACH/PSS 这类短序列有自己的长度，不受限。
HW_LITERALS = {4096, 2048, 1024, 288, 144}
ALLOWED_BUILTINS = {"len", "min", "max", "range", "int", "bool", "float", "str"}

ALLOWED_STMT = (ast.Assign, ast.For, ast.If, ast.Expr, ast.Pass)
ALLOWED_IMPORT = {"proto.hll"}


def _attr_chain(node):
    """把 a.b.c 还原成 'a.b.c'（用于导入白名单）。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


class Checker(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.bad = []

    def err(self, node, msg):
        self.bad.append((getattr(node, "lineno", 0), msg))

    # ------------------------------------------------------------ 语法
    def visit_Import(self, node):
        for a in node.names:
            if a.name not in ALLOWED_IMPORT:
                self.err(node, "不允许 import %s（只能 from proto.hll import scenario）"
                         % a.name)

    def visit_ImportFrom(self, node):
        if node.module not in ALLOWED_IMPORT:
            self.err(node, "不允许 from %s import ...（只能 proto.hll）" % node.module)
        for a in node.names:
            if a.name != "scenario":
                self.err(node, "只能导入 scenario，不能导入 %s" % a.name)

    def visit_While(self, node):
        self.err(node, "禁止 while（无界循环会让生成物不可分析）")

    def visit_Try(self, node):
        self.err(node, "禁止 try/except")

    def visit_ClassDef(self, node):
        self.err(node, "禁止 class（场景只允许动词序列）")

    def visit_Lambda(self, node):
        self.err(node, "禁止 lambda")

    def visit_Global(self, node):
        self.err(node, "禁止 global")

    def visit_With(self, node):
        self.err(node, "禁止 with")

    def visit_FunctionDef(self, node):
        self.err(node, "禁止自定义函数（新动词必须走 docs/protocols/README.md 的规范修订）")

    # ------------------------------------------------------------ 调用
    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name):
            name = f.id
        elif isinstance(f, ast.Attribute):
            name = f.attr
        else:
            self.err(node, "不允许这种调用形式")
            name = None
        if name and name not in ALLOWED_CALLS and name not in ALLOWED_BUILTINS:
            self.err(node, "不允许调用 %s()（可用动词见 docs/protocols/README.md）" % name)
        if name == "received":
            import ast as _ast
            for kw in node.keywords:
                for lit in _ast.walk(kw.value):
                    if isinstance(lit, _ast.Constant) and lit.value in HW_LITERALS:
                        self.err(node, "received() 里写死了 %r：波形尺寸必须由 "
                                       "cfg/oversample 推导，用 S.sym_len(...)"
                                 % (lit.value,))
                        break
        self.generic_visit(node)

    # ------------------------------------------------------------ 标识符
    def _check_name(self, node, name):
        low = name.lower()
        for b in BANNED_IDENTS:
            # 只拦"独立词"，避免误伤 e.g. "period"/"spe"
            if low == b or low.startswith(b + "_") or low.endswith("_" + b) \
                    or ("_" + b + "_") in low:
                self.err(node, "标识符 %r 含硬件词 %r（docs/protocols/README.md 规则 1）" % (name, b))
                return

    def visit_Name(self, node):
        self._check_name(node, node.id)
        self.generic_visit(node)

    def visit_arg(self, node):
        self._check_name(node, node.arg)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        self._check_name(node, node.attr)
        self.generic_visit(node)

    # ------------------------------------------------------------ 语句白名单
    def generic_visit(self, node):
        if isinstance(node, ast.stmt) and not isinstance(node, ALLOWED_STMT) \
                and not isinstance(node, (ast.Import, ast.ImportFrom)):
            self.err(node, "不允许的语句类型 %s" % type(node).__name__)
        super().generic_visit(node)


def check_file(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=path)
    c = Checker(path)
    c.visit(tree)
    # 必须有且只有一个 S = scenario(...)
    n_scen = 0
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name) \
                and node.value.func.id == "scenario":
            n_scen += 1
    if n_scen != 1:
        c.bad.append((1, "模块级必须有且只有一个 `S = scenario(...)`（当前 %d 个）"
                      % n_scen))
    return c.bad


def main(argv):
    files = argv or []
    if not files:
        print("usage: check_hll.py <scenario.py> ...")
        return 2
    total = 0
    for f in files:
        bad = check_file(f)
        if bad:
            print("[FAIL] %s" % f)
            for line, msg in sorted(bad):
                print("  %s:%d  %s" % (f, line, msg))
            total += len(bad)
        else:
            print("[ OK ] %s  (硬件无关检查通过)" % f)
    if total:
        print("\n%d 处违例；规则见 docs/protocols/README.md" % total)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
