"""nr_csi 的物理层参照（只给 proto/emit.py 与 proto/proto_eval.py 用）。

激励 = tools/nr_csi.py dump 的裸时域样本（20 dB / seed 1）；
黄金 = tools/nr_csi.py 的模型（CSI-RS 序列 + LS + 差值噪声 + CQI 门限）。
"""
import math
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import nr_csi as CSI                                     # noqa: E402

REF_GATE = "run-csi"
REF_DUMP = "sim/soc/build/csi_dump.txt"

# 这个场景 80% 的周期是一次 4096 点 FFT（112 次阵列运行 + 描述符编排），
# 函数边界/代码布局会让两侧差 ~1%（实测 1.1%）。值仍是逐位对账，
# 周期门因此放宽到 2%，并在报告里写明。
CYCLES_TOL = 0.02

# 锚点块布局（与 sw/soc/nr_csi_ops.inc 的 write_anchors 一一对应）
SPEC = {
    "platform": {"dma": False, "busy": False},
    "scalars": [
        ("cqi", 0, "d"),
        ("snr_q8", 1, "window"),
        ("ps40", 2, "nonzero"),
        ("pn10", 3, "nonzero"),
        ("a0r", 4, "sign"), ("a0i", 5, "sign"),
        ("a1r", 6, "sign"), ("a1i", 7, "sign"),
        ("a2r", 8, "sign"), ("a2i", 9, "sign"),
        ("a3r", 10, "sign"), ("a3i", 11, "sign"),
        ("ncsi", 12, "d"),
        ("c_init", 13, "hex"),
    ],
    "bits": [],
    "ref_scalars": [
        ("cqi", 5), ("snr_q8", 7), ("ps40", 8), ("pn10", 9),
        ("a0r", 10), ("a0i", 11), ("a1r", 12), ("a1i", 13),
        ("a2r", 14), ("a2i", 15), ("a3r", 16), ("a3i", 17),
        ("ncsi", 18), ("c_init", 19),
    ],
    "ref_bits": [],
}


def stimulus_bytes():
    fd, path = tempfile.mkstemp(suffix=".td")
    os.close(fd)
    try:
        CSI.dump_td_bin(path, CSI.DEFAULT_SNR, CSI.DEFAULT_SEED)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def _sign(v):
    return 1 if v > 0 else (-1 if v < 0 else 0)


def golden():
    g = CSI.golden(CSI.DEFAULT_SNR, CSI.DEFAULT_SEED)
    snr = g["snr_db"]
    # 判据用 2 dB 窗口（check_csi.py 的口径）：80 点估计器有固有偏差。
    lo = int(256.0 * (10.0 ** ((snr - 2.0) / 10.0)))
    hi = int(256.0 * (10.0 ** ((snr + 2.0) / 10.0)))
    anchors = {
        "cqi": g["cqi"],
        "ncsi": CSI.NCSI,
        "c_init": CSI.csi_cinit(),
        "ps40": 1,          # 只要求非零（真实值随前端增益变化）
        "pn10": 1,
    }
    for i in range(4):
        a = g["a"][i]
        anchors["a%dr" % i] = _sign(a.real)
        anchors["a%di" % i] = _sign(a.imag)
    return {
        "scenario": "nr_csi",
        "anchors": anchors,
        "tol": {"cqi": 1},
        "windows": {"snr_q8": [lo, hi]},
        "bits": {},
    }
