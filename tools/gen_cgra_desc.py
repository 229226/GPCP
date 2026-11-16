#!/usr/bin/env python3
"""从 rtl/cgra/cgra_desc64.yml 生成 64B 复合描述符的三份字段视图。

生成物（不要手改）：
  rtl/cgra/cgra_desc_pkg.sv   RTL 解析函数（输入是一个 64-bit AXI beat）
  sw/include/gpcp_desc64.h    C 构造 / 读取
  sim/cgra/desc64.py          台架构造 / 校验

用法：
  python3 tools/gen_cgra_desc.py            # 重新生成
  python3 tools/gen_cgra_desc.py --check    # CI/提交前：与磁盘不一致就失败
"""
import argparse
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "rtl", "cgra", "cgra_desc64.yml")
OUT_SV = os.path.join(ROOT, "rtl", "cgra", "cgra_desc_pkg.sv")
OUT_C = os.path.join(ROOT, "sw", "include", "gpcp_desc64.h")
OUT_PY = os.path.join(ROOT, "sim", "cgra", "desc64.py")

HEADER = "// 由 tools/gen_cgra_desc.py 从 rtl/cgra/cgra_desc64.yml 生成，不要手改。\n"
HEADER_PY = "# 由 tools/gen_cgra_desc.py 从 rtl/cgra/cgra_desc64.yml 生成，不要手改。\n"


def load_fields():
    with open(SPEC) as f:
        spec = yaml.safe_load(f)
    fields = []
    for name, v in spec["fields"].items():
        word, lo, hi = int(v["word"]), int(v["lo"]), int(v["hi"])
        if not (0 <= word < 16 and 0 <= lo <= hi <= 31):
            sys.exit("字段 %s 越界: word=%d [%d:%d]" % (name, word, hi, lo))
        beat = word // 2
        base = (word % 2) * 32
        fields.append({"name": name, "word": word, "lo": lo, "hi": hi,
                       "beat": beat, "blo": base + lo, "bhi": base + hi,
                       "width": hi - lo + 1})
    return fields


def gen_sv(fields):
    out = [HEADER,
           "// 64B 复合描述符字段访问器：每个字段只在 YAML 里定义一次。\n",
           "package cgra_desc_pkg;\n"]
    for f in fields:
        w = f["width"]
        out.append("\n  // %s: word %d [%d:%d]（beat%d [%d:%d]）\n"
                   % (f["name"], f["word"], f["hi"], f["lo"],
                      f["beat"], f["bhi"], f["blo"]))
        out.append("  function automatic logic [%d:0] d64_%s(input logic [63:0] b);\n"
                   % (w - 1, f["name"]))
        out.append("    return b[%d:%d];\n" % (f["bhi"], f["blo"]))
        out.append("  endfunction\n")
    out.append("\nendpackage\n")
    return "".join(out)


def gen_c(fields):
    out = [HEADER,
           "#ifndef GPCP_DESC64_H\n#define GPCP_DESC64_H\n\n#include <stdint.h>\n\n",
           "#define GPCP_DESC64_WORDS 16u\n\n"]
    for f in fields:
        w, lo, word = f["width"], f["lo"], f["word"]
        mask = 0xFFFFFFFF if w == 32 else ((1 << w) - 1) << lo
        out.append("/* %s: word %d [%d:%d] */\n" % (f["name"], word, f["hi"], f["lo"]))
        out.append("static inline void gpcp_d64_set_%s(uint32_t *d, uint32_t v)\n" % f["name"])
        out.append("{\n    d[%d] = (d[%d] & ~0x%08xu) | ((v & 0x%08xu) << %d);\n}\n"
                   % (word, word, mask, mask >> lo, lo))
        out.append("static inline uint32_t gpcp_d64_%s(const uint32_t *d)\n" % f["name"])
        out.append("{\n    return (d[%d] >> %d) & 0x%08xu;\n}\n\n"
                   % (word, lo, mask >> lo))
    out.append("#endif /* GPCP_DESC64_H */\n")
    return "".join(out)


def gen_py(fields):
    out = [HEADER_PY,
           '"""64B 复合描述符字段表（由 YAML 生成）。"""\n\n',
           "# name: (word, lo, width)\nFIELDS = {\n"]
    for f in fields:
        out.append('    "%s": (%d, %d, %d),\n' % (f["name"], f["word"], f["lo"], f["width"]))
    out.append("}\n\n\n")
    out.append("def get(desc, name):\n"
               "    word, lo, width = FIELDS[name]\n"
               "    return (desc[word] >> lo) & ((1 << width) - 1)\n\n\n")
    out.append("def put(desc, name, v):\n"
               "    word, lo, width = FIELDS[name]\n"
               "    m = ((1 << width) - 1) << lo\n"
               "    desc[word] = (desc[word] & ~m) | ((v << lo) & m)\n\n\n")
    out.append("def make(**kw):\n"
               "    desc = [0] * 16\n"
               "    for k, v in kw.items():\n"
               "        if k not in FIELDS:\n"
               "            raise KeyError(\"未知描述符字段: %s\" % k)\n"
               "        put(desc, k, v)\n"
               "    return desc\n")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验生成物是否最新")
    args = ap.parse_args()

    fields = load_fields()
    want = {OUT_SV: gen_sv(fields), OUT_C: gen_c(fields), OUT_PY: gen_py(fields)}

    if args.check:
        stale = []
        for path, text in want.items():
            if not os.path.exists(path) or open(path).read() != text:
                stale.append(path)
        for p in stale:
            print("STALE %s" % os.path.relpath(p, ROOT))
        if stale:
            print("先运行：python3 tools/gen_cgra_desc.py")
            return 1
        print("desc64 生成物已同步（%d 个字段）" % len(fields))
        return 0

    for path, text in want.items():
        with open(path, "w") as f:
            f.write(text)
        print("wrote %s" % os.path.relpath(path, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
