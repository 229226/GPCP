#!/usr/bin/env python3
"""docs/protocols/README.md 的第 2~3 步：场景 → IR（与目标无关）→ 各目标的落点计划。

    python3 proto/compile.py --scenario nr_pdcch
    python3 proto/compile.py --all
    python3 proto/compile.py --scenario nr_pdcch --no-whatif

输出（build/proto/<scenario>/）：
    ir.json              语义图，**对所有目标逐字节相同**
    plan-<target>.json   落点计划（唯一含硬件数字的产物）
    summary.txt          人读摘要（供 CI 日志与 report.md 引用）
"""
import argparse
import hashlib
import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import lower as LOW                     # noqa: E402
import check_hll as CHK                 # noqa: E402

TARGETS = [
    os.path.join(_ROOT, "cgra", "target.yaml"),
    os.path.join(_HERE, "targets", "whatif-8x8.yaml"),
]


def load_scenario(path):
    """执行场景文件，拿到它构造好的 S（只允许 proto.hll 的动词，已被静态检查过）。"""
    name = "proto_scenario_" + os.path.basename(path).replace(".py", "")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "S"):
        raise SystemExit("%s 里没有模块级 S = scenario(...)" % path)
    return mod.S


def compile_one(path, outdir, use_whatif=True):
    bad = CHK.check_file(path)
    if bad:
        for line, msg in sorted(bad):
            print("  %s:%d  %s" % (path, line, msg))
        raise SystemExit("[FAIL] %s 未通过 docs/protocols/README.md 检查" % path)

    S = load_scenario(path)
    ir = S.ir()
    ir_bytes = S.ir_bytes()

    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "ir.json"), "wb") as f:
        f.write(ir_bytes)
    ir_sha = hashlib.sha256(ir_bytes).hexdigest()[:16]

    base = LOW.read_target(TARGETS[0])
    variants = [("base", base)]
    if use_whatif:
        variants.append(("whatif", LOW.apply_overrides(base, TARGETS[1])))

    lines = ["scenario : %s (%s/%s)" % (ir["scenario"], ir["rat"], ir["direction"]),
             "ir sha   : %s  (%d bytes)" % (ir_sha, len(ir_bytes))]
    all_ok = True
    shas = []
    for name, tgt in variants:
        plan, ok = LOW.lower(ir, tgt)
        shas.append(plan["ir_sha"])
        with open(os.path.join(outdir, "plan-%s.json" % name), "w",
                  encoding="utf-8") as f:
            f.write(LOW.dumps(plan))
        mark = "OK " if ok else "FAIL"
        lines.append("[%s] target %-7s %dx%d = %d pe, %d banks x %d words, imem %d cycles"
                     % (mark, name, tgt["rows"], tgt["cols"], tgt["n_pes"], tgt["banks"],
                        tgt["words_per_bank"], tgt["imem_cycles"]))
        for st in plan["stages"]:
            note = ("  " + "；".join(st["notes"])) if st["notes"] else ""
            lines.append("       %-2d %-17s -> %-10s %-16s%s"
                         % (st["id"], st["op"], st["where"],
                            st["impl"] or "-", note))
        if not ok:
            all_ok = False
            lines.append("       needs: %s" % ", ".join(plan["needs"]))

    # 换厨房测试：同一场景对两个目标必须得到同一个 IR 摘要
    if len(set(shas)) != 1:
        lines.append("[FAIL] IR 随目标变化了（抽象泄漏）：%s" % shas)
        all_ok = False
    else:
        lines.append("[ OK ] 换厨房测试：两个目标的 IR 摘要一致（%s）" % shas[0])

    txt = "\n".join(lines)
    with open(os.path.join(outdir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)
    return all_ok


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--no-whatif", action="store_true")
    ap.add_argument("--out", default=os.path.join(_ROOT, "build", "proto"))
    a = ap.parse_args(argv)

    scdir = os.path.join(_HERE, "scenarios")
    if a.all:
        paths = sorted(os.path.join(scdir, f) for f in os.listdir(scdir)
                       if f.endswith(".py") and not f.startswith("_"))
    elif a.scenario:
        p = os.path.join(scdir, a.scenario + ".py")
        paths = [p if os.path.exists(p) else a.scenario]
    else:
        print(__doc__)
        return 2

    ok = True
    for p in paths:
        name = os.path.basename(p).replace(".py", "")
        print("== %s ==" % name)
        ok &= compile_one(p, os.path.join(a.out, name), not a.no_whatif)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
