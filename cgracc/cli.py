"""cgracc 命令行：info / schedule / check / emit。"""
from __future__ import annotations

import argparse
import sys

from .archreg import DEFAULT_ROOT, load_arch
from .conformance import run_all
from .ir.dfg import load_dfg
from .ir.schedule import load_schedule
from .sched.list_sched import ListScheduler
from .sched.modulo import ModuloScheduler
from .verify.legality import check

SCHEDULERS = {"list": ListScheduler, "modulo": ModuloScheduler}


def _load(args):
    model, emitter = load_arch(args.arch, args.root)
    dfg = load_dfg(args.dfg) if getattr(args, "dfg", None) else None
    return model, emitter, dfg


def cmd_info(args) -> int:
    model, _ = load_arch(args.arch, args.root)
    print(model.summary())
    return 0


def cmd_conformance(args) -> int:
    model, _ = load_arch(args.arch, args.root)
    results = run_all(model)
    bad = 0
    for r in results:
        if r.ok:
            print(f"[ OK ] {r.name}")
        else:
            bad += 1
            print(f"[FAIL] {r.name}")
            for f in r.failures:
                print(f"       {f}")
    print(f"conformance: {len(results) - bad} passed, {bad} failed")
    return 1 if bad else 0


def cmd_schedule(args) -> int:
    model, _, dfg = _load(args)
    sched = SCHEDULERS[args.scheduler]().schedule(dfg, model)
    sched.save(args.out)
    print(f"schedule: {sched.run_cycles} cycles, {len(sched.slots)} slots -> {args.out}")
    return 0


def cmd_check(args) -> int:
    model, _, dfg = _load(args)
    sched = load_schedule(args.schedule)
    vs = check(sched, dfg, model)
    for v in vs:
        print(v)
    print(f"check: {len(vs)} violation(s)")
    return 1 if vs else 0


def cmd_emit(args) -> int:
    model, emitter, dfg = _load(args)
    sched = load_schedule(args.schedule)
    text = emitter.emit(sched, dfg, model)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"emit: {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cgracc", description="architecture-agnostic CGRA compiler")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="架构插件根目录（默认 arch/）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info", help="打印架构模型摘要")
    s.add_argument("--arch", required=True)
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("conformance", help="跑接入验收用例（6 正向 + 4 负向）")
    s.add_argument("--arch", required=True)
    s.set_defaults(func=cmd_conformance)

    s = sub.add_parser("schedule", help="DFG -> Schedule")
    s.add_argument("--arch", required=True)
    s.add_argument("--dfg", required=True)
    s.add_argument("--scheduler", default="list", choices=sorted(SCHEDULERS))
    s.add_argument("-o", "--out", default="build/schedule.json")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("check", help="合法性检查")
    s.add_argument("--arch", required=True)
    s.add_argument("--dfg", required=True)
    s.add_argument("--schedule", required=True)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("emit", help="Schedule -> 架构产物")
    s.add_argument("--arch", required=True)
    s.add_argument("--dfg", required=True)
    s.add_argument("--schedule", required=True)
    s.add_argument("-o", "--out")
    s.set_defaults(func=cmd_emit)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
