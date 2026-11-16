"""nr_pusch_tp 的物理层参照（tools/models/nr_pusch.py 的 tp=True 口径）。"""
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

import nr_pusch as P                                     # noqa: E402

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
        ("1040 个硬判决", 10, "hard", "与 nr_pusch tp=True 黄金码字逐位"),
    ],
    "ref_scalars": [],
    "ref_bits": [],
}


def stimulus_bytes():
    fd, path = tempfile.mkstemp(suffix=".td")
    os.close(fd)
    try:
        P.dump_td_bin(path, RV, SNR, SEED, tp=True)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def golden():
    g = P.pusch_golden(SNR, SEED, rv=RV, tp=True)
    return {
        "scenario": "nr_pusch_tp",
        "anchors": {
            "crc_ok": 1,
            "k0": RV * (P.USABLE // 4),
            "harq_tx": 1,
            "tp": 1,
            "dmrs_c_init0": P.dmrs_cinit_pusch(0),
            "dmrs_c_init1": P.dmrs_cinit_pusch(1),
            "hre0": 0, "him0": 0, "hre1": 0, "him1": 0,
        },
        "bits": {"hard": [int(v) & 1 for v in g["hard"]]},
    }
