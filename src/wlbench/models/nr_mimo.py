"""nr_mimo 的物理层参照（tools/nr_mimo_ce.py + nr_mimo_link.py）。

激励 = 双天线的两块时域样本（nr_mimo_ce.dump）；
黄金 = 同一信道上的浮点 MMSE 硬判（nr_mimo_link.link 的 raw LLR 符号）。
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import nr_mimo_ce as CE                                  # noqa: E402
import nr_mimo_link as ML                                # noqa: E402

REF_GATE = "run-mimo"
REF_DUMP = "reference/mimo_dump.txt"

SNR = 25.0
SEED = 1

SPEC = {
    # y 现在由 CPU 直接写进 local memory（见 reference stages）：本阶段不再调用
    # transfer API，FFT 的描述符本来就不计入，所以平台锚点不统计 transfer。
    "platform": {"transfer": False, "busy": True},
    # run-mimo 只报 CGRA MMSE 运行数（[7]），没有总阵列运行数字段
    "ref_stats": {"cycles": 2, "runs": None},
    "scalars": [
        ("mmse_runs", 0, "d"),
        ("ndata", 1, "d"),
        ("lam", 2, "nonzero"),
        ("nbits", 3, "d"),
        # 量化后的信道系数：模型给的是浮点，这里只与 run-mimo 逐值对账
        ("h00r", 4, "raw"), ("h00i", 5, "raw"),
        ("h01r", 6, "raw"), ("h01i", 7, "raw"),
        ("h10r", 8, "raw"), ("h10i", 9, "raw"),
        ("h11r", 10, "raw"), ("h11i", 11, "raw"),
    ],
    "bits": [
        ("720 个硬判比特", 12, "packed", "每 RE 4 bit，与浮点 MMSE 同号"),
    ],
    "ref_scalars": [
        ("mmse_runs", 7), ("ndata", 16), ("lam", 17), ("nbits", 19),
        ("h00r", 8), ("h00i", 9), ("h01r", 10), ("h01i", 11),
        ("h10r", 12), ("h10i", 13), ("h11r", 14), ("h11i", 15),
    ],
    "ref_bits": [
        ("720 个硬判比特", 20, "packed"),
    ],
}


def stimulus_bytes():
    fd, path = tempfile.mkstemp(suffix=".td")
    os.close(fd)
    try:
        CE.dump_td_bin(path, snr_db=SNR, seed=SEED)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def golden():
    h = CE.channel_matrix(SEED)
    ref = ML.link(SNR, SEED, h=h)
    bits = [1 if v < 0 else 0 for v in ref["raw"]]
    packed = []
    for i in range(CE.NDATA):
        w = 0
        for k in range(4):
            w |= bits[4 * i + k] << k
        packed.append(w)
    return {
        "scenario": "nr_mimo",
        "anchors": {
            "mmse_runs": 12,          # ceil(180/16) 趟，一层算两层
            "ndata": CE.NDATA,
            "lam": 1,                 # 只要求非零（随信道量化变）
            "nbits": len(bits),
        },
        "bits": {"packed": packed},
    }
