"""nr_pdsch_harq 的物理层参照：两传输 Chase 合并（pdsch_full_plan 口径）。"""
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

import pdsch_full_plan as F                              # noqa: E402
import pdsch_plan as M                                   # noqa: E402

SNR = 20.0
SEED = 1

SPEC = {
    "scalars": [
        ("crc_ok", 0, "d"),
        ("rv0", 5, "d"),
        ("rv1", 6, "d"),
        ("harq_tx", 7, "d"),
    ],
    "bits": [
        ("1040 个硬判决", 8, "hard", "两传输 Chase 合并后的黄金码字"),
    ],
    "ref_scalars": [],
    "ref_bits": [],
}


def stimulus_bytes():
    fd, path = tempfile.mkstemp(suffix=".td")
    os.close(fd)
    try:
        F.dump_td_bin_harq(path, SNR, SEED)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def golden():
    dem, hs, _raws, _descrs, _yq0 = F.soft_combine(SNR, SEED)
    hard, _scaled = F.hw_decode(dem)
    tb = hard[:M.A_TB]
    crc = hard[M.A_TB:M.A_TB + M.CRC_LEN]
    return {
        "scenario": "nr_pdsch_harq",
        "anchors": {
            "crc_ok": 1 if M.crc24a(tb) == crc else 0,
            "rv0": 0,
            "rv1": 2,
            "harq_tx": 2,
        },
        "bits": {"hard": [int(v) & 1 for v in hard]},
    }
