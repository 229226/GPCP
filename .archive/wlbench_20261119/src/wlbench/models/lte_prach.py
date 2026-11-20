"""lte_prach 的物理层参照（nr_rach.py 的 Msg1 路径，前导 4 / TA 5）。"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import nr_rach as R                                      # noqa: E402
import nr_sib1 as S1                                     # noqa: E402

PREAMBLE = 4
TA = 5
ZCZC = 0

SPEC = {
    "platform": {"transfer": True, "busy": True},
    "ref_stats": {"cycles": None, "runs": 1},
    "scalars": [
        ("preamble", 0, "d"),
        ("ta", 1, "d"),
        ("best_d", 2, "d"),
        ("second_d", 3, "d"),
        ("n_cs", 4, "d"),
        ("l_ra", 5, "d"),
        ("taps", 6, "d"),
        ("nchk", 7, "d"),
    ],
    "bits": [
        ("139 个相关值（re,im）", 8, "corr", "与 msg1_correlations 逐值"),
    ],
    "ref_scalars": [],
    "ref_bits": [],
}


def _build():
    chips = R.zc_chips()
    n_cs = R.n_cs_of(dict(S1.default_sib1(), zero_corr_zone=ZCZC))
    y = R.msg1_tx(PREAMBLE, TA, n_cs, chips, R.PRACH_SNR, R.DEFAULT_SEED)
    return chips, n_cs, y


def stimulus_bytes():
    chips, _n_cs, y = _build()
    fd, path = tempfile.mkstemp(suffix=".td")
    os.close(fd)
    try:
        R.dump_prach(path, y, chips)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def golden():
    chips, n_cs, y = _build()
    exp = R.msg1_correlations(y, chips)
    det = R.msg1_detect(y, chips, n_cs)
    flat = []
    for re, im in exp:
        flat += [re & 0xFFFFFFFF, im & 0xFFFFFFFF]
    return {
        "scenario": "lte_prach",
        "anchors": {
            "preamble": det["preamble"],
            "ta": det["ta"],
            "best_d": det["delay"],
            "second_d": det["second_delay"],
            "n_cs": n_cs,
            "l_ra": R.L_RA,
            "taps": R.PRACH_TAPS,
            "nchk": R.L_RA * 32,
        },
        "bits": {"corr": flat},
    }
