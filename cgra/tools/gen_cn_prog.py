#!/usr/bin/env python3
"""薄壳：转发到统一入口 gen_kernel_prog.py（kernel_spec + rule_map）。

    python3 cgra/tools/gen_cn_prog.py [--out sw/soc/pdsch_cn_prog.h]

实际微码由 rule_map.schedule_cn 生成（见 docs/18-微码自动化.md）。
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import gen_kernel_prog as gk                         # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    return gk.run_spec(os.path.join(ROOT, "cgra/kernels/cn.yaml"), a.out)


if __name__ == "__main__":
    sys.exit(main())
