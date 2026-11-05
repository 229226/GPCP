#!/usr/bin/env python3
"""生成 `sw/soc/prach_prog.h`：PRACH 循环移位相关（复参考 + 16 路蝶形归约）微码。

    env -u LD_LIBRARY_PATH python3 tools/gen_prach_prog.py

微码本体在 `tools/cgra.py` 的 `kernel_zc_corr()` 里，`make test` 的 `prach_corr`
kernel 已在 Verilator 台架上逐值对过（16 个分部 + 总和）。这里只是把同一份 words
导出成 C 数组，供 run-prach / run-rach 装载；判据见
`make -C sim/soc run-prach` + `python3 tools/check_prach.py`。

spad 布局（页内线性字地址 = bank*32 + word，与 cgra.py 常量一致）：
    0..253   接收 chip y[i]=(re,im)                     bank 0..7
    256..509 候选参考 r[i]=(re,im)（每个 d 重新 DMA）    bank 8..15
    512..543 每 PE 分部 (re,im)                         bank 16
    544..575 蝶形中间级，最终 (re,im) 在 word 28/29      bank 17
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import cgra                              # noqa: E402
import nr_rach as R                      # noqa: E402

NPES = cgra.NPES
PREAMBLE, TA = 2, 9


def build():
    """The kernel whose microcode is exported, with the benchmark's own stimulus."""
    chips = R.zc_chips()
    ref = chips[:cgra.PRACH_TAPS]
    d = PREAMBLE * 15 + TA
    y = [chips[(i + d) % R.L_RA] for i in range(cgra.PRACH_TAPS)]
    return cgra.kernel_zc_corr(y, ref, name="prach_prog")


def main():
    out = os.path.join(ROOT, "sw", "soc", "prach_prog.h")
    k = build()
    with open(out, "w") as f:
        f.write("/* 由 tools/gen_prach_prog.py 生成，勿手改。\n"
                " * 本体 = tools/cgra.py kernel_zc_corr()（make test 的 prach_corr kernel，\n"
                " * Verilator 台架上 16 个分部 + 总和逐值对账）。\n"
                " * %d 拍 x %d PE；spad：0..253 rx chip / 256..509 参考 /\n"
                " * 512..543 每 PE 分部 / 544..575 蝶形（最终 re,im = bank17 word28,29）。\n"
                " */\n" % (k.cycles, NPES))
        f.write("#ifndef PRACH_PROG_H\n#define PRACH_PROG_H\n\n")
        f.write("#define PRACH_PROG_CYCLES %d\n" % k.cycles)
        f.write("#define PRACH_PROG_PES    %d\n\n" % NPES)
        f.write("static const unsigned int prach_prog[%d * %d]\n"
                "    __attribute__((aligned(8))) = {\n" % (k.cycles, NPES))
        for i, w in enumerate(k.words):
            f.write("0x%08xu," % w)
            if (i + 1) % 8 == 0:
                f.write("\n")
        f.write("\n};\n\n#endif\n")
    print("wrote %s: %d cycles x %d PEs, %d words" % (out, k.cycles, NPES,
                                                      len(k.words)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
