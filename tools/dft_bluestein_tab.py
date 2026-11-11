#!/usr/bin/env python3
"""生成 Bluestein DFT 的 chirp 表（Q15）。

chirp[n] = exp(+j*pi*n^2/N)，N = 200 / 360。
表项是 int16 Q15；n^2 先对 2N 取模，避免大角度。
"""
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "sw/soc/dft_bluestein_tab.h"


def q15(v: float) -> int:
    q = int(round(v * 32768.0))
    return 32767 if q >= 32768 else (-32768 if q < -32768 else q)


def table(n: int):
    cos, sin = [], []
    for i in range(n):
        a = math.pi * ((i * i) % (2 * n)) / n
        cos.append(q15(math.cos(a)))
        sin.append(q15(math.sin(a)))
    return cos, sin


def emit(name: str, n: int) -> str:
    cos, sin = table(n)
    lines = ["static const int16_t dft_chirp%d_cos[%d] = {" % (n, n)]
    for i in range(0, n, 12):
        lines.append("    " + ",".join(str(v) for v in cos[i:i + 12]) + ",")
    lines.append("};")
    lines.append("static const int16_t dft_chirp%d_sin[%d] = {" % (n, n))
    for i in range(0, n, 12):
        lines.append("    " + ",".join(str(v) for v in sin[i:i + 12]) + ",")
    lines.append("};")
    return "\n".join(lines)


def main():
    text = [
        "/* 由 tools/dft_bluestein_tab.py 生成，勿手改。",
        " * chirp[n] = exp(+j*pi*n^2/N)，Q15；Bluestein 正向用 conj(chirp)。 */",
        "#ifndef DFT_BLUESTEIN_TAB_H",
        "#define DFT_BLUESTEIN_TAB_H",
        "#include <stdint.h>",
        emit("200", 200),
        emit("360", 360),
        "#endif",
        "",
    ]
    OUT.write_text("\n".join(text), encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
