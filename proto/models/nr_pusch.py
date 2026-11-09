"""nr_pusch 的物理层参照（tools/nr_pusch.py，阶段 A 黄金模型）。

激励 = 2 符号时域样本 + 尾部 rv 字（dump_td_bin）；
黄金 = pusch_golden 的 1040 个硬判比特与 80 bit TB。
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import nr_pusch as P                                     # noqa: E402

REF_GATE = "run-pusch"
REF_DUMP = "sim/soc/build/pusch_dump.txt"

RV = 0
SNR = 20.0
SEED = 1

SPEC = {
    "platform": {"dma": True, "busy": True},
    "scalars": [
        ("crc_ok", 0, "d"),
        ("k0", 1, "d"),
        ("harq_tx", 2, "d"),
        ("tp", 3, "d"),
        ("hre0", 4, "raw"), ("him0", 5, "raw"),
        ("hre1", 6, "raw"), ("him1", 7, "raw"),
        ("dmrs_c_init0", 8, "hex"),
        ("dmrs_c_init1", 9, "hex"),
    ],
    "bits": [
        ("1040 个硬判决", 10, "hard", "与 nr_pusch 黄金码字逐位"),
    ],
    "ref_scalars": [
        ("crc_ok", 5), ("k0", 1087), ("harq_tx", 1083),
        ("hre0", 1048), ("him0", 1049), ("hre1", 1050), ("him1", 1051),
        ("dmrs_c_init0", 1081), ("dmrs_c_init1", 1082),
    ],
    "ref_bits": [
        ("1040 个硬判决", 8, "hard"),
    ],
}


def stimulus_bytes():
    fd, path = tempfile.mkstemp(suffix=".td")
    os.close(fd)
    try:
        P.dump_td_bin(path, RV, SNR, SEED)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def golden():
    g = P.pusch_golden(SNR, SEED, rv=RV)
    return {
        "scenario": "nr_pusch",
        "anchors": {
            "crc_ok": 1,
            "k0": RV * (P.USABLE // 4),
            "harq_tx": 1,
            "tp": 0,
            "dmrs_c_init0": P.dmrs_cinit_pusch(0),
            "dmrs_c_init1": P.dmrs_cinit_pusch(1),
            "hre0": 0, "him0": 0, "hre1": 0, "him1": 0,   # 只与参照比
        },
        "bits": {"hard": [int(v) & 1 for v in g["hard"]]},
    }
