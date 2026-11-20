"""nr_pucch 的物理层参照（tools/models/nr_pucch.py）。

激励 = 139 个接收 chip + 2 组 127 抽头；黄金 = detect()（bit / winner / m_CS
/ 两个复数相关值）。
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
_MODELS = os.path.join(_TOOLS, "models")
if _MODELS not in sys.path:
    sys.path.insert(0, _MODELS)
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import nr_pucch as P                                     # noqa: E402

REF_GATE = "run-pucch"
REF_DUMP = "sim/soc/build/pucch_dump.txt"

ACK = 1
SNR = 6.0
SEED = 1

SPEC = {
    "platform": {"dma": True, "busy": True},
    # run-pucch 的结果区没有周期字段（[1]=runs [2]=DMA [3]=errors）
    "ref_stats": {"cycles": None, "runs": 1},
    "scalars": [
        ("bit", 0, "d"),
        ("win", 1, "d"),
        ("m_cs", 2, "d"),
        ("ncand", 3, "d"),
        ("taps", 4, "d"),
        ("nchk", 5, "d"),
        ("c0re", 6, "d"), ("c0im", 7, "d"),
        ("c0pw_lo", 8, "d"), ("c0pw_hi", 9, "d"),
        ("c1re", 10, "d"), ("c1im", 11, "d"),
        ("c1pw_lo", 12, "d"), ("c1pw_hi", 13, "d"),
    ],
    "bits": [
        ("64 个 PE 分部", 14, None, "与 run-pucch 逐值", 64),
    ],
    "ref_scalars": [
        ("bit", 5), ("win", 6), ("m_cs", 7), ("ncand", 8), ("taps", 9),
        ("nchk", 18),
        ("c0re", 20), ("c0im", 21), ("c0pw_lo", 22), ("c0pw_hi", 23),
        ("c1re", 24), ("c1im", 25), ("c1pw_lo", 26), ("c1pw_hi", 27),
    ],
    "ref_bits": [
        ("64 个 PE 分部", 48, None, 64),
    ],
}


def stimulus_bytes():
    fd, path = tempfile.mkstemp(suffix=".td")
    os.close(fd)
    try:
        P.dump_td_bin(path, ACK, SNR, SEED)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def golden():
    g = P.golden(ACK, SNR, SEED)
    d = g["detect"]
    (r0, i0), (r1, i1) = d["corr"]
    p0, p1 = r0 * r0 + i0 * i0, r1 * r1 + i1 * i1
    return {
        "scenario": "nr_pucch",
        "anchors": {
            "bit": d["bit"],
            "win": d["cand"],
            "m_cs": d["cs"],
            "ncand": len(d["corr"]),
            "taps": P.TAPS,
            "nchk": 64,
            "c0re": r0 & 0xFFFFFFFF, "c0im": i0 & 0xFFFFFFFF,
            "c0pw_lo": p0 & 0xFFFFFFFF, "c0pw_hi": (p0 >> 32) & 0xFFFFFFFF,
            "c1re": r1 & 0xFFFFFFFF, "c1im": i1 & 0xFFFFFFFF,
            "c1pw_lo": p1 & 0xFFFFFFFF, "c1pw_hi": (p1 >> 32) & 0xFFFFFFFF,
        },
        "bits": {},                    # 分部只与 run-pucch 对账，模型不给
    }
