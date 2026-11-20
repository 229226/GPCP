#!/usr/bin/env python3
# --- tools path bootstrap (subdirs import root modules) ---
import os as _os, sys as _sys
_T = _os.path.dirname(_os.path.abspath(__file__))
_TOOLS = _T if _os.path.basename(_T) == "tools" else _os.path.dirname(_T)
for _p in (_TOOLS, _os.path.join(_TOOLS, "models"), _os.path.join(_TOOLS, "checks"),
           _os.path.join(_os.path.dirname(_TOOLS), "cgra", "tools")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
_ROOT = _os.path.dirname(_TOOLS)
# --- end bootstrap ---
"""重复造轮子审计：在 C / Python / Makefile 里找重复实现。

判定：
  exact  —— 归一化后函数体逐行相同
  near   —— 行集合 Jaccard >= --sim（默认 0.80）且两侧都 >= --min-lines 行

归一化保留结构（去掉注释、空行、纯括号行、首尾空白），因此"改了变量名/常量"
的复制品仍然能被 near 抓到。

    python3 tools/dup_audit.py                 # 全量报告
    python3 tools/dup_audit.py --sim 0.9       # 更严格
    python3 tools/dup_audit.py --only c        # 只看 C
"""

import argparse
import ast
import collections
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".git", "build", "third_party", "obj_dir", "__pycache__", "docs"}


# ---------------------------------------------------------------- extraction

def norm_lines(lines):
    out = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if re.fullmatch(r"[{}();]+", s):
            continue
        if s.startswith("//") or s.startswith("#") and not s.startswith("#!"):
            if s.startswith("#") and re.match(r"#\s*(if|endif|include|define|"
                                              r"else|elif|pragma)", s):
                pass                      # keep preprocessor
            else:
                continue
        out.append(re.sub(r"\s+", " ", s))
    return out


C_SIG = re.compile(
    r"^(?:static\s+|inline\s+|extern\s+)*"
    r"(?:void|int|char|uint\d+_t|int\d+_t|size_t|unsigned|long|float|double|"
    r"struct\s+\w+)\s*\**\s*(\w+)\s*\([^;]*\)\s*$")


def c_functions(path):
    """Brace-matching extraction; returns [(name, line_no, [body lines])]."""
    try:
        src = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return []
    out = []
    i = 0
    while i < len(src):
        ln = src[i]
        m = C_SIG.match(ln.strip())
        if m and not ln.strip().startswith("#"):
            # body starts at the next line that is '{' or on the same line
            j = i
            while j < len(src) and "{" not in src[j]:
                j += 1
                if j - i > 2:
                    break
            if j < len(src) and "{" in src[j]:
                depth, k = 0, j
                while k < len(src):
                    depth += src[k].count("{") - src[k].count("}")
                    if depth <= 0 and k >= j:
                        break
                    k += 1
                out.append((m.group(1), i + 1, src[j:k + 1]))
                i = k + 1
                continue
        i += 1
    return out


def py_functions(path):
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return []
    lines = src.splitlines()
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not hasattr(node, "end_lineno"):
            continue
        body = lines[node.lineno - 1:node.end_lineno]
        out.append((node.name, node.lineno, body))
    return out


def walk(exts):
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if os.path.splitext(f)[1] in exts:
                yield os.path.relpath(os.path.join(base, f), ROOT)


# ---------------------------------------------------------------- analysis

def collect(exts, extract):
    funcs = []
    for rel in walk(exts):
        for name, line, body in extract(os.path.join(ROOT, rel)):
            nb = norm_lines(body)
            if len(nb) < 5:
                continue
            funcs.append({"file": rel, "name": name, "line": line,
                          "body": nb, "sig": hash("\n".join(nb))})
    return funcs


def cluster(funcs, sim, min_lines):
    """Union exact groups, then merge near-identical groups by Jaccard."""
    by_sig = collections.defaultdict(list)
    for f in funcs:
        by_sig[f["sig"]].append(f)

    groups = []
    for sig, members in by_sig.items():
        groups.append({"members": members, "kind": "exact",
                       "lines": len(members[0]["body"])})

    # near-dup between distinct signature groups
    reps = [(g, {ln for m in g["members"] for ln in m["body"]})
            for g in groups]
    merged = [False] * len(groups)
    for i in range(len(groups)):
        if merged[i]:
            continue
        for j in range(i + 1, len(groups)):
            if merged[j]:
                continue
            a, b = reps[i][1], reps[j][1]
            if min(len(a), len(b)) < min_lines:
                continue
            inter = len(a & b)
            if not inter:
                continue
            jac = inter / float(len(a) + len(b) - inter)
            if jac >= sim:
                groups[i]["members"].extend(groups[j]["members"])
                groups[i]["kind"] = "near"
                groups[i]["sim"] = max(groups[i].get("sim", 0), jac)
                merged[j] = True
    return [g for i, g in enumerate(groups) if not merged[i]]


def report(funcs, groups, sim, min_lines, top):
    dups = [g for g in groups
            if len(g["members"]) > 1 and g["lines"] >= min_lines]
    dups.sort(key=lambda g: -g["lines"] * (len(g["members"]) - 1))

    wasted = sum(g["lines"] * (len(g["members"]) - 1) for g in dups)
    total = sum(f["body"].__len__() for f in funcs)
    near = sum(1 for g in dups if g["kind"] == "near")

    print("扫描函数 %d 个（归一化后总行 %d）" % (len(funcs), total))
    print("重复簇 %d 个（exact %d / near %d），冗余行数 ≈ %d（占 %.1f%%）\n"
          % (len(dups), len(dups) - near, near, wasted,
             100.0 * wasted / max(total, 1)))

    for g in dups[:top]:
        members = g["members"]
        files = sorted({m["file"] for m in members})
        lo = min(len(m["body"]) for m in members)
        hi = max(len(m["body"]) for m in members)
        tag = g["kind"] if g["kind"] == "exact" else \
            "near %.0f%%" % (100 * g.get("sim", sim))
        print("%-9s %2d 份 × %3d~%3d 行  %s"
              % (tag, len(members), lo, hi,
                 "%s()" % members[0]["name"]))
        for f in files:
            cnt = sum(1 for m in members if m["file"] == f)
            names = ", ".join(sorted({m["name"] for m in members
                                      if m["file"] == f}))
            print("      %-42s %s%s" % (f, names,
                                        "" if cnt == 1 else "  (%d 份)" % cnt))
        print()


# ---------------------------------------------------------------- makefile

def makefile_dups(min_lines=2, top=12):
    print("=== Makefile 重复配方 ===")
    for rel in ("sim/soc/Makefile", "sw/soc/Makefile", "Makefile"):
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        lines = open(path, encoding="utf-8").read().splitlines()
        cur, rec = None, collections.defaultdict(list)
        for ln in lines:
            m = re.match(r"^([A-Za-z0-9_.$()/-]+):", ln)
            if m:
                cur = m.group(1)
                continue
            if cur and ln.startswith("\t"):
                rec[cur].append(ln.strip())
        buckets = collections.defaultdict(list)
        for tgt, body in rec.items():
            if len(body) >= min_lines:
                buckets["\n".join(body)].append(tgt)
        hits = [(b.splitlines(), ts) for b, ts in buckets.items() if len(ts) > 1]
        hits.sort(key=lambda h: -len(h[0]) * (len(h[1]) - 1))
        wasted = sum(len(b) * (len(t) - 1) for b, t in hits)
        print("  %-22s 目标 %3d 个，完整重复配方 %d 组，冗余 %d 行"
              % (rel, len(rec), len(hits), wasted))
        for body, tgts in hits[:top]:
            print("      %d 行 × %d 目标: %s" % (len(body), len(tgts),
                                               ", ".join(sorted(tgts)[:6])
                                               + (" ..." if len(tgts) > 6 else "")))
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", type=float, default=0.80)
    ap.add_argument("--min-lines", type=int, default=10)
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--only", choices=["c", "py"])
    a = ap.parse_args()

    funcs = []
    if a.only != "py":
        funcs += collect({".c", ".h"}, c_functions)
    if a.only != "c":
        funcs += collect({".py"}, py_functions)
    groups = cluster(funcs, a.sim, a.min_lines)
    report(funcs, groups, a.sim, a.min_lines, a.top)
    if not a.only:
        makefile_dups()


if __name__ == "__main__":
    main()
