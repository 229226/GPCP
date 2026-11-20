#!/usr/bin/env python3
"""wlbench testbench：同源双编对账。

流程：
  1. 用主机 gcc 编出的 wlbench_host 跑同一份 C 源码，得到 golden 结果字；
  2. 读取 GPCP 跑出的 dump（同样的 index value 文本）；
  3. 按字段类别做容差比较，输出 PASS/FAIL。

benchmark 本身不做任何中间正确性检查；所有判定都在这里。
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import result_spec as S  # noqa: E402


def parse_words(path):
    vals = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                idx = int(parts[0], 0)
                val = int(parts[1], 0)
            except ValueError:
                continue
            vals[idx] = val & 0xFFFFFFFF
    return vals


def run_host(host, host_args, result_path):
    cmd = [host] + host_args + ["--quiet", "--result", result_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit("host 运行失败（exit %d）" % proc.returncode)
    return parse_words(result_path)


def category_map():
    m = {}
    for _idx, (name, cat) in S.HEADER.items():
        m[name] = cat
    for _idx, name, cat in S.SCALARS:
        m[name] = cat
    for _lo, name, cat in S.BITS:
        m[name] = cat
    for _off, name, cat, _width in S.UE_FIELDS:
        for u in range(16):
            m["ue%d_%s" % (u, name)] = cat
    return m


CATEGORY = category_map()


def compare(golden, got, scale=1.0, strict=False):
    gn = S.named_values(golden)
    gt = S.named_values(got)
    mismatches = []
    for name, g in gn.items():
        if name not in gt:
            mismatches.append((name, g, None, 0.0))
            continue
        v = gt[name]
        cat = CATEGORY.get(name, "exact")
        tol = 0.0 if strict else S.tolerance(cat, g, scale)
        if abs(v - g) > tol:
            mismatches.append((name, g, v, tol))
    return mismatches


def print_report(mismatches, total):
    print("checked %d fields, %d mismatches" % (total, len(mismatches)))
    for name, g, v, tol in mismatches[:20]:
        if v is None:
            print("  [MISS] %-22s golden=%s" % (name, g))
        else:
            print("  [DIFF] %-22s golden=%-12s got=%-12s |d|=%-10s tol=%.1f"
                  % (name, g, v, abs(v - g), tol))
    if len(mismatches) > 20:
        print("  ... and %d more" % (len(mismatches) - 20))


def selftest(host):
    ok = True
    with tempfile.TemporaryDirectory() as td:
        golden_path = os.path.join(td, "golden.txt")
        golden = run_host(host, ["--scenario", "mixed", "--slots", "200",
                                 "--seed", "1"], golden_path)
        if not golden:
            print("[FAIL] host 没有产出结果字")
            return 1
        if len(golden) < S.RESULT_WORDS:
            print("[FAIL] 结果字不足：%d < %d" % (len(golden), S.RESULT_WORDS))
            return 1

        same = compare(golden, golden, strict=True)
        print("[%s] golden 自比较" % (" OK " if not same else "FAIL"))
        ok = ok and not same

        bad = dict(golden)
        bad[4] = (bad.get(4, 0) + 1000) & 0xFFFFFFFF
        m = compare(golden, bad)
        caught = any(x[0] == "nr_dl_tb" for x in m)
        print("[%s] 变异 nr_dl_tb +1000 被捕获" % (" OK " if caught else "FAIL"))
        ok = ok and caught

        bad2 = dict(golden)
        bad2[35] = (bad2.get(35, 0) + 100000) & 0xFFFFFFFF
        m2 = compare(golden, bad2)
        caught2 = any(x[0] == "nr_nack_ppm" for x in m2)
        print("[%s] 变异 nr_nack_ppm +100000 被捕获" % (" OK " if caught2 else "FAIL"))
        ok = ok and caught2

        bad3 = dict(golden)
        del bad3[41]
        m3 = compare(golden, bad3)
        caught3 = any(x[0] == "lat_mean_mslot" and x[2] is None for x in m3)
        print("[%s] 缺字段 lat_mean_mslot 被捕获" % (" OK " if caught3 else "FAIL"))
        ok = ok and caught3

    print("selftest: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wlbench-testbench")
    ap.add_argument("--host", required=True, help="主机版 wlbench_host 路径")
    ap.add_argument("--dump", help="GPCP 结果字文件（index value）；省略则用 host 自比较")
    ap.add_argument("--args", default="--scenario mixed --slots 200 --seed 1",
                    help="传给 host 的场景参数")
    ap.add_argument("--strict", action="store_true", help="所有字段要求逐值相等")
    ap.add_argument("--tol-scale", type=float, default=1.0, help="容差整体缩放")
    ap.add_argument("--json", help="把结论写到 JSON")
    ap.add_argument("--selftest", action="store_true", help="跑变异自检")
    ap.add_argument("--print-names", action="store_true", help="打印下标与字段名")
    a = ap.parse_args(argv)

    if a.print_names:
        for idx in range(S.RESULT_WORDS):
            print("%d %s" % (idx, S.INDEX_NAMES.get(idx, "?")))
        return 0

    if a.selftest:
        return selftest(a.host)

    host_args = shlex.split(a.args)
    with tempfile.TemporaryDirectory() as td:
        golden_path = os.path.join(td, "golden.txt")
        golden = run_host(a.host, host_args, golden_path)
        if not golden:
            raise SystemExit("host 没有产出结果字")
        if a.dump:
            got = parse_words(a.dump)
        else:
            got = golden

    mismatches = compare(golden, got, a.tol_scale, a.strict)
    total = len(S.named_values(golden))
    print_report(mismatches, total)

    correct = len(mismatches) == 0
    print("verdict: %s" % ("PASS" if correct else "FAIL"))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump({
                "correct": correct,
                "checked": total,
                "mismatches": [
                    {"field": n, "golden": g, "got": v, "tol": t}
                    for n, g, v, t in mismatches
                ],
            }, f, indent=2, ensure_ascii=False)
            f.write("\n")
    return 0 if correct else 1


if __name__ == "__main__":
    sys.exit(main())
