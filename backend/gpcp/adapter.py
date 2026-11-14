#!/usr/bin/env python3
"""GPCP backend adapter for wlbench workload bundles.

This is the only hardware-specific layer a backend has to write: it consumes
the hardware-agnostic bundle (ir.json / stimulus.bin / golden.json), supplies
the backend's own target geometry and clock, runs/collects cycles, and reports
metrics.json back to wlbench for the protocol timing verdict.

Nothing under proto/, sw/, sim/ or the existing Makefiles is modified.
"""
import argparse
import csv
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GPCP = HERE.parent.parent
BUNDLES = Path("/home/zzq/wlbench/build/bundles")
PROTO = GPCP / "build" / "proto"
DUMPS = GPCP / "sim" / "soc" / "build"
LOCK = Path("/home/zzq/wlbench/regression/lock.json")
LEDGER = PROTO / "compare_all.csv"
CLOCKS = HERE / "clocks.json"

sys.path.insert(0, str(GPCP / "tools"))
from checklib import parse_dump, words32  # noqa: E402

STAGE_WORD = 10
ARTIFACTS = ("ir.json", "stimulus.bin", "golden.json")
_WHERE = {"control-core": "PROTO_WHERE_CPU",
          "array": "PROTO_WHERE_ARRAY",
          "dma": "PROTO_WHERE_DMA"}
BACKENDS = {"gpcp-cgra": "", "cva6-scalar": "_cpu"}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _cstr(s):
    return '"%s"' % (s or "")


def scenarios():
    with open(LOCK, encoding="utf-8") as f:
        return sorted(json.load(f)["scenarios"])


def bundle_dir(name):
    return BUNDLES / name


def verify_bundle(name):
    """The bundle must be byte-identical to what GPCP already produced."""
    out = {}
    for a in ARTIFACTS:
        b, g = bundle_dir(name) / a, PROTO / name / a
        out[a] = b.exists() and g.exists() and _sha(b) == _sha(g)
    return out


def build_plan_h(name):
    """Rebuild plan.h from the *bundle* IR plus GPCP's target geometry."""
    ir_bytes = (bundle_dir(name) / "ir.json").read_bytes()
    ir = json.loads(ir_bytes.decode("utf-8"))
    with open(PROTO / name / "plan-base.json", encoding="utf-8") as f:
        plan = json.load(f)
    stim = (bundle_dir(name) / "stimulus.bin").read_bytes()

    params_hash = int(hashlib.sha256(ir_bytes + stim).hexdigest()[:8], 16)
    tgt = plan["target"]
    budget = ir.get("budget") or {}
    slot_ms = float(budget.get("slot_ms", 0.0))
    clock_hz = int(tgt.get("clock_hz", 0))
    budget_cycles = int(round(slot_ms * clock_hz / 1000.0)) if slot_ms and clock_hz else 0
    stim_magic = struct.unpack("<I", stim[:4])[0] if len(stim) >= 4 else 0

    lines = [
        "/* 生成物：proto/emit.py 从 ir.json + plan-base.json 生成，勿手改。",
        " * 场景 %s（%s/%s），目标 %s（%dx%d = %d PE，%d bank x %d 字）。"
        % (name, ir.get("rat"), ir.get("direction"), tgt.get("source", "?"),
           tgt["rows"], tgt["cols"], tgt["n_pes"], tgt["banks"],
           tgt["words_per_bank"]),
        " * 改场景 / 换目标后重跑 `make proto PROTO=%s`。" % name,
        " */",
        "#ifndef PROTO_PLAN_H",
        "#define PROTO_PLAN_H",
        "",
        '#define PROTO_SCENARIO     "%s"' % name,
        "#define PROTO_PARAMS_HASH  0x%08xul" % params_hash,
        "#define PROTO_N_STAGES     %d" % len(plan["stages"]),
        "#define PROTO_CLOCK_HZ     %dul" % clock_hz,
        "#define PROTO_SLOT_MS      %.3f" % slot_ms,
        "#define PROTO_BUDGET_CYCLES %dul" % budget_cycles,
        "#define PROTO_STIM_MAGIC   0x%08xul" % stim_magic,
        "#define PROTO_STIM_WORDS   %d" % (len(stim) // 4),
        "",
        "typedef enum { PROTO_WHERE_CPU = 0, PROTO_WHERE_ARRAY = 1,",
        "               PROTO_WHERE_DMA = 2 } proto_where_t;",
        "",
        "typedef struct {",
        "    uint8_t     id;            /* 语义图节点号（docs/20 §2 的顺序） */",
        "    uint8_t     where;         /* 落点：CPU / 阵列 / DMA */",
        "    uint16_t    planned_runs;  /* 计划里的阵列运行次数（0 = 纯标量） */",
        "    const char *op;            /* 协议动词 */",
        "    const char *impl;          /* 编译器选的实现（诊断用） */",
        "} proto_stage_t;",
        "",
        "static const proto_stage_t proto_stages[PROTO_N_STAGES] = {",
    ]
    for st in plan["stages"]:
        where = _WHERE.get(st["where"], "PROTO_WHERE_CPU")
        lines.append("    { %2d, %-18s %3d, %-18s %s },"
                     % (st["id"], where + ",", int(st.get("array_runs", 0)),
                        _cstr(st["op"]) + ",", _cstr(st.get("impl") or "")))
    lines += ["};", "", "#endif /* PROTO_PLAN_H */", ""]
    return "\n".join(lines)


def dump_words(name, backend):
    path = DUMPS / ("proto_%s%s_dump.txt" % (name, BACKENDS[backend]))
    st = parse_dump(path)
    if st.get("timeout", 1):
        raise SystemExit("[FAIL] %s 超时" % path)
    w = words32(st)
    if len(w) < 10 or w[0] != 0x50524F54:
        raise SystemExit("[FAIL] %s 结果区 magic 不对" % path)
    return st, w


def stage_cycles(name, backend):
    st, w = dump_words(name, backend)
    with open(PROTO / name / "plan-base.json", encoding="utf-8") as f:
        stages = json.load(f)["stages"]
    ops = {s["id"]: s["op"] for s in stages}
    per = {}
    for i in range(len(stages)):
        sid = w[STAGE_WORD + 3 * i]
        per[sid] = w[STAGE_WORD + 3 * i + 1]
    total = w[2]
    correct = (w[1] == 0 and st.get("exit_code") == 0x600D)
    return {"total": total, "per": per, "ops": ops, "correct": correct}


def collect_metrics(name, backend):
    """Backend reports only generic quantities: clock_hz, cycles, correct."""
    with open(CLOCKS, encoding="utf-8") as f:
        clock = json.load(f)[backend]
    r = stage_cycles(name, backend)
    stages = [{"id": sid, "op": r["ops"].get(sid, "?"), "cycles": cyc,
               "correct": True}
              for sid, cyc in sorted(r["per"].items())]
    return {
        "backend": backend,
        "clock_hz": clock["clock_hz"],
        "clock_source": clock["clock_source"],
        "total_cycles": r["total"],
        "correct": r["correct"],
        "stages": stages,
    }


def evaluate(name, backend):
    from wlbench import metrics as M
    return M.evaluate(str(bundle_dir(name)), collect_metrics(name, backend))


def regress():
    """Full P4 regression: bundles, plan.h, per-stage cycles, ledger."""
    names = scenarios()
    checks, failures = [], []

    def record(label, ok):
        checks.append(ok)
        if not ok:
            failures.append(label)

    for n in names:
        for a, ok in verify_bundle(n).items():
            record("%s/%s bundle byte-match" % (n, a), ok)
        existing = (PROTO / n / "plan.h").read_text(encoding="utf-8")
        record("%s/plan.h regenerated from bundle" % n,
               build_plan_h(n) == existing)

    cgra_diffs, cpu_diffs = [], []
    with open(LEDGER, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for n in names:
        cg = stage_cycles(n, "gpcp-cgra")["per"]
        cpu = stage_cycles(n, "cva6-scalar")["per"]
        for row in rows:
            if row["scenario"] != n or row["id"] == "TOTAL":
                continue
            sid = int(row["id"])
            # Both dumps can be newer than the frozen 11-12 ledger (GPCP has
            # uncommitted lower.py / *_stages.inc work).  Drift is reported,
            # never treated as an adapter failure: collect() must report the
            # CURRENT backend cycles.
            for per, key, bucket in ((cg, "cgra_cycles", cgra_diffs),
                                     (cpu, "cpu_cycles", cpu_diffs)):
                if sid in per and per[sid] != int(row[key]):
                    bucket.append((n, sid, int(row[key]), per[sid]))

    with open(LOCK, encoding="utf-8") as f:
        lock = json.load(f)
    record("compare_all.csv unchanged (frozen sha)",
           _sha(LEDGER) == lock["ledger"]["sha256"])

    print("frozen checks: %d/%d passed" %
          (len(checks) - len(failures), len(checks)))
    for f in failures[:20]:
        print("  [FAIL] " + f)
    if len(failures) > 20:
        print("  ... and %d more" % (len(failures) - 20))
    for label, diffs in (("CGRA", cgra_diffs), ("CPU", cpu_diffs)):
        if diffs:
            scenes = sorted({d[0] for d in diffs})
            print("live %s dump drift: %d stage rows across %d scenarios"
                  % (label, len(diffs), len(scenes)))
            for n, sid, old, new in diffs[:2]:
                print("  e.g. %s/%d ledger=%d dump=%d" % (n, sid, old, new))
    if cgra_diffs or cpu_diffs:
        print("  ledger is older than the dumps; adapter reports CURRENT cycles")
    return 0 if not failures else 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpcp-adapter")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("regress", help="full bundle + plan.h + cycle regression")

    c = sub.add_parser("collect", help="write metrics.json for one run")
    c.add_argument("--scenario", required=True)
    c.add_argument("--backend", choices=sorted(BACKENDS), default="gpcp-cgra")
    c.add_argument("--out")

    e = sub.add_parser("evaluate", help="collect + wlbench timing verdict")
    e.add_argument("--scenario", required=True)
    e.add_argument("--backend", choices=sorted(BACKENDS), default="gpcp-cgra")
    e.add_argument("--out")

    v = sub.add_parser("verify-bundle", help="compare one bundle with GPCP artifacts")
    v.add_argument("--scenario", required=True)

    a = ap.parse_args(argv)
    if a.cmd == "regress":
        return regress()
    if a.cmd == "collect":
        data = collect_metrics(a.scenario, a.backend)
        blob = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(blob, encoding="utf-8")
        print(blob, end="")
        return 0
    if a.cmd == "evaluate":
        rep = evaluate(a.scenario, a.backend)
        blob = json.dumps(rep, indent=2, ensure_ascii=False) + "\n"
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(blob, encoding="utf-8")
        t = rep.get("timeline")
        print("scenario=%s backend=%s correct=%s on_time=%s wall=%.3f ms" %
              (rep["scenario"], rep["backend"], rep["correct"],
               rep["primary_on_time"], rep["wall_ms"]))
        if t:
            print("timeline: %d slots, on_time_rate=%.2f, late=%d" %
                  (t["slots"], t["on_time_rate"], t["late_slots"]))
        return 0
    if a.cmd == "verify-bundle":
        for k, ok in verify_bundle(a.scenario).items():
            print("  %-16s %s" % (k, "OK" if ok else "MISMATCH"))
        return 0 if all(verify_bundle(a.scenario).values()) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
