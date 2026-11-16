#!/usr/bin/env python3
"""生成 sw/soc/gen/rev4_lut.h：4096 点 base-4 位反序表。

rev4(v) 在 ssb_fft.inc 里原来是 rev4n(v, 6)——内层每符号调用数千次，
6 次移位/或改成一次查表。表是纯数学常量，由本脚本生成，勿手改。
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "..", "sw", "soc", "gen", "rev4_lut.h")
N = 4096


def rev4n(v, digits):
    r = 0
    for _ in range(digits):
        r = (r << 2) | (v & 3)
        v >>= 2
    return r


def main():
    vals = [rev4n(v, 6) for v in range(N)]
    lines = [
        "/* 4096 点 base-4 位反序表：rev4(v) = rev4n(v, 6)，与 ssb_fft.inc 的公式一致。",
        " * 编译期常量表，避免内层循环重复 6 次移位/或。 */",
        "#ifndef REV4_LUT_H",
        "#define REV4_LUT_H",
        "#include <stdint.h>",
        "",
        "static const uint16_t rev4_lut[%d] = {" % N,
    ]
    for i in range(0, N, 16):
        lines.append("    " + ",".join(str(v) for v in vals[i:i + 16]) + ",")
    lines += ["};", "", "#endif /* REV4_LUT_H */"]
    with open(os.path.abspath(OUT), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote %s (%d entries)" % (os.path.abspath(OUT), N))


if __name__ == "__main__":
    main()
