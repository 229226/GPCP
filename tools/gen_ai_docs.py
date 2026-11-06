#!/usr/bin/env python3
"""把 AGENTS.md 里的台账规则块同步到其余 AI 入口文档。

规则块的唯一来源是 AGENTS.md 中 <!-- devlog-rule:v1 --> ... <!-- /devlog-rule -->
之间的文本。改了口径只改这一处，然后：

    python3 tools/gen_ai_docs.py sync     # 覆盖写入其余入口文档
    python3 tools/devlog.py check         # 校验六处一致

手工复制到 6 个文件是这类规则最常见的腐烂方式，所以做成命令。
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import devlog as D

BEGIN, END = D.BLOCK_BEGIN, D.BLOCK_END

# 文件缺失时用这些头补建（已存在则只替换块，不动头）
NEW_FILE_HEADERS = {
    "README.md": "# GPCP\n\n",
    "CLAUDE.md": ("# CLAUDE.md — Claude Code 入口\n\n"
                  "> 本仓库的 agent 规则以 [`AGENTS.md`](AGENTS.md) 为**唯一事实来源**。\n"
                  "> 与 AGENTS.md 冲突时一律以 AGENTS.md 为准。\n\n"),
    "GEMINI.md": ("# GEMINI.md — Gemini CLI 入口\n\n"
                  "> 本仓库的 agent 规则以 [`AGENTS.md`](AGENTS.md) 为**唯一事实来源**。\n"
                  "> 与 AGENTS.md 冲突时一律以 AGENTS.md 为准。\n\n"),
    ".cursorrules": ("# GPCP — Cursor 规则\n"
                     "# 完整规则见 AGENTS.md（唯一事实来源）；本文件是入口摘要。\n\n"),
    ".github/copilot-instructions.md": (
        "# Copilot / 代码助手指令\n\n"
        "> 本仓库的 agent 规则以 [`AGENTS.md`](../AGENTS.md) 为**唯一事实来源**。\n"
        "> 与 AGENTS.md 冲突时一律以 AGENTS.md 为准。\n\n"),
}


def canonical_block():
    b = D.rule_block(os.path.join(ROOT, "AGENTS.md"))
    if b is None:
        sys.exit("AGENTS.md 缺规则块（%s ... %s）" % (BEGIN, END))
    return b


def sync(targets):
    block = canonical_block()
    frag = "%s\n%s\n%s\n" % (BEGIN, block, END)
    changed = 0
    for rel in targets:
        path = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(path) or ROOT, exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(NEW_FILE_HEADERS.get(rel, "# %s\n\n" % rel) + frag)
            print("  created %s" % rel)
            changed += 1
            continue
        s = open(path, encoding="utf-8").read()
        m = re.search(re.escape(BEGIN) + r".*?" + re.escape(END), s, re.S)
        if not m:
            s = s.rstrip("\n") + "\n\n" + frag
            print("  appended block to %s" % rel)
        elif m.group(0) == frag.rstrip("\n"):
            print("  ok      %s" % rel)
            continue
        else:
            s = s[:m.start()] + frag.rstrip("\n") + s[m.end():]
            print("  updated %s" % rel)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(s)
        changed += 1
    print("== synced %d file(s) from AGENTS.md ==" % changed)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["sync"])
    ap.add_argument("--files", nargs="*",
                    help="默认同步 devlog.ENTRY_DOCS 中除 AGENTS.md 外的全部")
    a = ap.parse_args()
    targets = a.files or [f for f in D.ENTRY_DOCS if f != "AGENTS.md"]
    sync(targets)


if __name__ == "__main__":
    main()
