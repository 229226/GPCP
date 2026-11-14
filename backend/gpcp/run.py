#!/usr/bin/env python3
"""从 wlbench bundle 直接构建并跑一次（W2）。

之前 backend/gpcp 只能读别的流程留下的 dump；这里把"执行"补上：

    python3 run.py --bundle /home/zzq/wlbench/build/bundles/nr_mimo \
                   --backend gpcp-cgra

做的四件事：
  1. 从 bundle 的 ir.json + cgra/target.yaml 算 plan（backend/gpcp/lower.py）；
  2. 把 plan.h + bundle 的 stimulus.bin 摆到自己的 staging 目录；
  3. 编 proto_runner（ops 实现仍按场景名选，那是硬件层）；
  4. 跑 Verilator，dump 落到 sim/soc/build/，再交给 adapter.collect。

不再经过 build/proto/<场景>/plan-base.json，也不读 proto/scenarios/*.py。
没有对应 `<场景>_ops.inc` 的 bundle 会明确报"不支持的算子"，而不是崩。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import adapter as A  # noqa: E402

ROOT = A.GPCP
SW_SOC = ROOT / "sw" / "soc"
SIM = ROOT / "sim" / "soc" / "build" / "Vgpcp_soc"
TWIDDLE = ROOT / "build" / "fft" / "twiddle.bin"
STAGE_ROOT = HERE / "build" / "run"
ENV = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}


def _die(msg, code=2):
    sys.stderr.write("[FAIL] %s\n" % msg)
    raise SystemExit(code)


def stage(name, bundle):
    """把 plan.h 与 bundle 的激励摆到 staging 目录。"""
    d = STAGE_ROOT / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plan.h").write_text(A.build_plan_h(name, bundle=bundle), encoding="utf-8")
    shutil.copyfile(bundle / "stimulus.bin", d / "stimulus.bin")
    return d


def build(name, stage_dir, cpu):
    if not (SW_SOC / (name + "_ops.inc")).exists():
        _die("没有 sw/soc/%s_ops.inc：这个 bundle 的算子还没有硬件实现"
             "（lowering 能出计划，但跑不了）" % name, 3)
    cmd = ["make", "-C", str(SW_SOC), "proto",
           "PROTO=" + name, "PROTO_DIR=" + str(stage_dir)]
    if cpu:
        cmd.append("PROTO_CPU=1")
    print("[run] " + " ".join(cmd))
    subprocess.run(cmd, check=True, env=ENV)


def simulate(name, cpu, max_cycles):
    suffix = "_cpu" if cpu else ""
    bin_path = ROOT / "build" / "sw" / ("proto_runner_%s%s.bin" % (name, suffix))
    dump = A.DUMPS / ("proto_%s%s_dump.txt" % (name, suffix))
    if not bin_path.exists():
        _die("缺少 %s" % bin_path)
    if not TWIDDLE.exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "fft_plan.py"),
                        "lut", str(TWIDDLE)], check=True, env=ENV)
    cmd = [str(SIM),
           "--load", "%s@0x80000000" % bin_path,
           "--load", "%s@0x80400000" % (STAGE_ROOT / name / "stimulus.bin"),
           "--load", "%s@0x80600000" % (STAGE_ROOT / name / "stimulus.bin"),
           "--load", "%s@0x80100000" % TWIDDLE,
           "--results", "0x80070000", "--nwords", "1500",
           "--dump", str(dump), "--max-cycles", str(max_cycles)]
    print("[run] %s（dump -> %s）" % (SIM.name, dump))
    subprocess.run(cmd, check=True, env=ENV)
    return dump


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gpcp-run")
    ap.add_argument("--bundle", required=True, help="bundle 目录（含 ir.json）")
    ap.add_argument("--backend", choices=sorted(A.BACKENDS), default="gpcp-cgra")
    ap.add_argument("--out", help="metrics.json 输出路径")
    ap.add_argument("--max-cycles", type=int, default=40000000)
    a = ap.parse_args(argv)

    bundle = Path(a.bundle).resolve()
    if not (bundle / "ir.json").exists():
        _die("%s 不是 bundle（缺 ir.json）" % bundle)
    name = bundle.name
    cpu = a.backend == "cva6-scalar"

    st = stage(name, bundle)
    build(name, st, cpu)
    simulate(name, cpu, a.max_cycles)

    m = A.collect_metrics(name, a.backend)
    blob = json.dumps(m, indent=2, ensure_ascii=False) + "\n"
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(blob, encoding="utf-8")
        print("[run] metrics -> %s" % a.out)
    print(blob, end="")
    return 0 if m["correct"] else 1


if __name__ == "__main__":
    sys.exit(main())
