#!/usr/bin/env python3
"""IEEE 802.11 N=648, rate 1/2 LDPC（Z=27）的基图、编码与 C 头生成。

基图取自 IEEE 802.11-2020 的 N648_R12 表（12 x 24，移位值 0..26），
与公开实现 YairMZ/LDPC 的 ldpc/code_specs/ieee802.11/N648_R12.qc 一致。
硬件只做解码；模型用本文件的 GF(2) 消元生成合法码字。

    python3 tools/wifi_ldpc.py selftest
    python3 tools/wifi_ldpc.py dump-h sw/soc/wifi_ldpc_648_r12.h
"""
import argparse
import os
import sys

Z = 27
MB = 12
NB = 24
N = NB * Z          # 648
K = (NB - MB) * Z   # 324

# 12 x 24 基图：-1 = 全零块，0..26 = 单位阵循环移位。
BASE = [
    [0, -1, -1, -1, 0, 0, -1, -1, 0, -1, -1, 0, 1, 0, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1],
    [22, 0, -1, -1, 17, -1, 0, 0, 12, -1, -1, -1, -1, 0, 0, -1, -1, -1, -1, -1, -1, -1, -1, -1],
    [6, -1, 0, -1, 10, -1, -1, -1, 24, -1, 0, -1, -1, -1, 0, 0, -1, -1, -1, -1, -1, -1, -1, -1],
    [2, -1, -1, 0, 20, -1, -1, -1, 25, 0, -1, -1, -1, -1, -1, 0, 0, -1, -1, -1, -1, -1, -1, -1],
    [23, -1, -1, -1, 3, -1, -1, -1, 0, -1, 9, 11, -1, -1, -1, -1, 0, 0, -1, -1, -1, -1, -1, -1],
    [24, -1, 23, 1, 17, -1, 3, -1, 10, -1, -1, -1, -1, -1, -1, -1, -1, 0, 0, -1, -1, -1, -1, -1],
    [25, -1, -1, -1, 8, -1, -1, -1, 7, 18, -1, -1, 0, -1, -1, -1, -1, -1, 0, 0, -1, -1, -1, -1],
    [13, 24, -1, -1, 0, -1, 8, -1, 6, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, 0, 0, -1, -1, -1],
    [7, 20, -1, 16, 22, 10, -1, -1, 23, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, 0, 0, -1, -1],
    [11, -1, -1, -1, 19, -1, -1, -1, 13, -1, 3, 17, -1, -1, -1, -1, -1, -1, -1, -1, -1, 0, 0, -1],
    [25, -1, 8, -1, 23, 18, -1, 14, 9, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, 0, 0],
    [3, -1, -1, -1, 16, -1, -1, 2, 25, 5, -1, -1, 1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, 0],
]

assert len(BASE) == MB and all(len(r) == NB for r in BASE)


def h_rows():
    """324 个校验行，每行是 648 位的 Python int（bit v = 列 v）。"""
    rows = []
    for r in range(MB):
        for i in range(Z):
            bits = 0
            for c in range(NB):
                s = BASE[r][c]
                if s < 0:
                    continue
                j = (i + s) % Z
                bits |= 1 << (c * Z + j)
            rows.append(bits)
    return rows


H_ROWS = h_rows()


def syndrome(codeword):
    c = 0
    for i, b in enumerate(codeword):
        if b:
            c |= 1 << i
    s = 0
    for r, row in enumerate(H_ROWS):
        if (row & c).bit_count() & 1:
            s |= 1 << r
    return s


def encode(info):
    """信息位 324 -> 合法码字 648（GF(2) 消元求 324 个奇偶位）。"""
    info = [int(b) & 1 for b in info]
    assert len(info) == K
    m = 0
    for i, b in enumerate(info):
        if b:
            m |= 1 << i

    pbits = N - K
    mask = (1 << pbits) - 1
    rows = []
    for r, row in enumerate(H_ROWS):
        rhs = ((row & m).bit_count() & 1)
        rows.append([(row >> K) & mask, rhs])

    # 高斯消元：每列找一个主元，消去其它行。
    piv = [-1] * pbits
    for col in range(pbits):
        bit = 1 << col
        sel = -1
        for i in range(rows.__len__()):
            if piv[i] == -1 and (rows[i][0] & bit):
                sel = i
                break
        if sel < 0:
            raise RuntimeError("parity matrix not invertible at col %d" % col)
        piv[sel] = col
        rows[sel][0] ^= bit
        for i in range(len(rows)):
            if i != sel and (rows[i][0] & bit):
                rows[i][0] ^= rows[sel][0]
                rows[i][1] ^= rows[sel][1]

    parity = [0] * pbits
    for i, col in enumerate(piv):
        if col >= 0:
            parity[col] = rows[i][1] & 1
    cw = info + parity
    assert syndrome(cw) == 0
    return cw


def dump_h(path):
    lines = [
        "/* 生成物：tools/wifi_ldpc.py dump-h，IEEE 802.11-2020 N648_R12。",
        " * 12 x 24 基图，Z=27，rate 1/2；-1 = 全零块，否则为单位阵循环移位。 */",
        "#ifndef WIFI_LDPC_648_R12_H",
        "#define WIFI_LDPC_648_R12_H",
        "",
        "#define WIFI_LDPC_Z   27",
        "#define WIFI_LDPC_N   648",
        "#define WIFI_LDPC_K   324",
        "#define WIFI_LDPC_MB  12",
        "#define WIFI_LDPC_NB  24",
        "",
        "static const int8_t wifi_ldpc_base[12][24] = {",
    ]
    for row in BASE:
        lines.append("    { " + ", ".join("%3d" % v for v in row) + " },")
    lines += ["};", "", "#endif /* WIFI_LDPC_648_R12_H */", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def selftest():
    import random
    ok = True
    rnd = random.Random(0x11BE)
    for _ in range(3):
        info = [rnd.randrange(2) for _ in range(K)]
        cw = encode(info)
        ok &= syndrome(cw) == 0
        bad = list(cw)
        bad[rnd.randrange(N)] ^= 1
        ok &= syndrome(bad) != 0
    # 基图结构：每行 24 块，第一块列有 3 个非零
    ok &= all(len(r) == NB for r in BASE)
    print("802.11 N648 R1/2 (Z=27): encode/syndrome %s"
          % ("OK" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("selftest")
    d = sub.add_parser("dump-h")
    d.add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "dump-h":
        print("wrote", dump_h(a.out))
        return 0
    return selftest()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
