#!/usr/bin/env python3
"""生成 `sw/soc/pdsch_bn_prog.h`：规则映射器（link 版，4 PE/行）自动排出的 BN 微码。

    python3 cgra/tools/gen_bn_prog.py [--rows 20] [--out sw/soc/pdsch_bn_prog.h]

判据：`make -C sim/soc run-pdsch-bn1` + `python3 tools/check_pdsch_bn1.py`（520 个值逐值对账）。
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import rule_map as rm                      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20)
    ap.add_argument("--out", default=os.path.join(ROOT, "sw/soc/pdsch_bn_prog.h"))
    a = ap.parse_args()

    mc = rm.schedule_bn_split(rows=a.rows)
    words = mc.build()
    errs, nvals = rm.check_bn_split(mc, rows=a.rows)
    if errs:
        print(f"自检失败：{len(errs)}/{nvals} 个值不同，拒绝生成", file=sys.stderr)
        return 1

    with open(a.out, "w") as f:
        f.write("/* 由 cgra/tools/gen_bn_prog.py 生成，勿手改。\n"
                f" * 规则映射器（link 版）：每行 4 个 PE，{a.rows} 行 = {a.rows // 4} pass，"
                f"共 {mc.cycles} 拍 × {rm.NPES} PE。\n"
                f" * 布局：bank k = 第 k 条消息（24 槽）、bank 24 = LLR、bank 25 = 硬判决，\n"
                f" *       word = 提升行号（0..{a.rows - 1}）。\n"
                f" * 自检：{nvals} 个 spad 值、0 差异（模型）；台架见 run-pdsch-bn1。 */\n")
        f.write("#ifndef PDSCH_BN_PROG_H\n#define PDSCH_BN_PROG_H\n\n")
        f.write(f"#define BN_PROG_CYCLES {mc.cycles}\n")
        f.write(f"#define BN_PROG_PES    {rm.NPES}\n\n")
        f.write(f"static const unsigned int bn_prog[{mc.cycles} * {rm.NPES}]\n"
                f"    __attribute__((aligned(8))) = {{\n")
        for i, w in enumerate(words):
            f.write(f"0x{w:08x}u,")
            if (i + 1) % 8 == 0:
                f.write("\n")
        f.write("\n};\n\n#endif\n")
    print(f"wrote {a.out}: {mc.cycles} 拍 × {rm.NPES} PE，{len(words)} 字，"
          f"自检 {nvals} 值 0 差异")
    return 0


if __name__ == "__main__":
    sys.exit(main())
