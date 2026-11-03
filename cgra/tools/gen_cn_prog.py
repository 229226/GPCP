#!/usr/bin/env python3
"""生成 `sw/soc/pdsch_cn_prog.h`：规则映射器排出的 CN（min1/min2/sign）微码。

    python3 cgra/tools/gen_cn_prog.py [--out sw/soc/pdsch_cn_prog.h]

判据：`make -C sim/soc run-pdsch-bg2` + `python3 tools/check_pdsch_bg2.py`
（42 个检查节点 × 20 提升行的 min1/min2/sign 逐值对账）。
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
    ap.add_argument("--out", default=os.path.join(ROOT, "sw/soc/pdsch_cn_prog.h"))
    a = ap.parse_args()

    mc = rm.schedule_cn()
    words = mc.build()
    errs, nvals = rm.check_cn(mc)
    if errs:
        print(f"自检失败：{len(errs)}/{nvals} 个值不同，拒绝生成", file=sys.stderr)
        return 1

    with open(a.out, "w") as f:
        f.write("/* 由 cgra/tools/gen_cn_prog.py 生成，勿手改。\n"
                f" * 规则映射器：每行 1 个 PE、20 个提升行一次跑完（16 + 4），"
                f"共 {mc.cycles} 拍 × {rm.NPES} PE。\n"
                f" * 布局：bank 0..9 = 10 条边（不足由 bank {rm.CN_SENTINEL_BANK} 的 +32767 补齐）、\n"
                f" *       bank 10/11/12 = min1/min2/sign，word = 提升行号。\n"
                f" * 自检：{nvals} 个 spad 值、0 差异（模型）；台架见 run-pdsch-bg2。 */\n")
        f.write("#ifndef PDSCH_CN_PROG_H\n#define PDSCH_CN_PROG_H\n\n")
        f.write(f"#define CN_PROG_CYCLES {mc.cycles}\n")
        f.write(f"#define CN_PROG_PES    {rm.NPES}\n\n")
        f.write(f"static const unsigned int cn_prog[{mc.cycles} * {rm.NPES}]\n"
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
