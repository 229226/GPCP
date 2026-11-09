"""nr_ssb 的物理层参照（tools/ssb_plan_p1.py，P1 真映射）。

激励 = 四个符号的时域样本（dump）；
黄金 = 真实 N_ID1/N_ID2、i_SSB/n_hf、PBCH DMRS c_init、32 bit MIB。
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import ssb_plan_p1 as P                                  # noqa: E402

# 这个场景 70% 的周期是 4 次 FFT + 336 个 SSS 假设的 DMA 编排；函数边界与
# 代码布局会让两侧差 ~1%（实测 1.15%）。值仍逐位对账，周期门放到 2%。
CYCLES_TOL = 0.02

REF_GATE = "run-ssb-fid"
REF_DUMP = "sim/soc/build/ssb_fid_dump.txt"

SPEC = {
    "platform": {"dma": True, "busy": True},
    "scalars": [
        ("n_id2", 0, "d"),
        ("n_id1", 1, "d"),
        ("i_ssb", 2, "d"),
        ("n_hf", 3, "d"),
        ("dmrs_c_init", 4, "hex"),
        # 定点 LS 值只与参照比（手写基准的 [11] 被尾声标记覆盖，故不比）
        ("hre1", 5, "raw"), ("him1", 6, "raw"),
        ("hre3", 7, "raw"), ("him3", 8, "raw"),
        ("sbest10", 9, "raw"), ("strue10", 10, "raw"),
        ("dbg_wait", 43, "raw"), ("dbg_corr_spin", 44, "raw"),
        ("dbg_array_spin", 45, "raw"), ("dbg_nblk", 46, "raw"),
    ],
    "bits": [
        ("MIB 比特", 11, "mib", "与 ssb_plan_p1.payload_bits 逐位"),
    ],
    "ref_scalars": [
        ("n_id2", 4), ("n_id1", 6), ("dmrs_c_init", 10),
    ],
    "ref_bits": [
        ("MIB 比特", 15, "mib"),
    ],
}


def stimulus_bytes():
    fd, path = tempfile.mkstemp(suffix=".td")
    os.close(fd)
    try:
        P.dump_td_bin(path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def golden():
    return {
        "scenario": "nr_ssb",
        "anchors": {
            "n_id2": P.TRUE_NID2,
            "n_id1": P.TRUE_NID1,
            "i_ssb": P.I_SSB,
            "n_hf": P.N_HF,
            "dmrs_c_init": P.pbch_dmrs_cinit(P.TRUE_NID1, P.TRUE_NID2,
                                             P.I_SSB, P.N_HF),
            "hre1": 0, "him1": 0, "hre3": 0, "him3": 0,
            "sbest10": 0, "strue10": 0,
        },
        "bits": {"mib": list(P.payload_bits())},
    }
