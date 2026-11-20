"""nr_pdsch 的物理层参照（只给 proto/emit.py 与 proto/proto_eval.py 用）。

分工（docs/20 §4）：
  * 激励 = 协议输入（80 bit TB、rv、RNTI、Qm）。复用已验收的
    tools/nr_dl_mod.py dump-td，所以协议运行器和手写 benchmark 吃同一份字节。
  * 黄金 = tools/nr_dl_mod.py 的发射链（B2，已被 run-dl-mod 逐值验收）。
  * 参照门 = 人写的同一协议 benchmark（run-pdsch-loop）。
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import nr_dl_mod as TX                                   # noqa: E402
import pdsch_plan as PLAN                                # noqa: E402

REF_GATE = "run-pdsch-loop"
REF_DUMP = "reference/pdsch_loop_dump.txt"

# 锚点块布局（与 sw/soc/nr_pdsch_ops.inc 的 write_anchors 一一对应）
SPEC = {
    "scalars": [
        ("tb_bad", 0, "d"),
        ("crc_ok", 1, "d"),
        ("llr_bad", 2, "d"),
        ("llr_first", 3, "hex"),
        ("hre0", 4, "d"),
        ("him0", 5, "d"),
        ("hre1", 6, "d"),
        ("him1", 7, "d"),
        ("dmrs_c_init0", 8, "hex"),
        ("dmrs_c_init1", 9, "hex"),
        ("rv", 10, "d"),
        ("qm", 11, "d"),
        ("max_sym", 12, "d"),
        ("n_llr", 13, "d"),
    ],
    "bits": [
        ("TB 比特", 14, "tb", ""),
        ("码字硬判", 94, "cw", "与 nr_dl_mod.encode 逐位"),
    ],
    # 手写基准 dump 里的对应下标（run-pdsch-loop 的结果区布局）
    "ref_scalars": [
        ("tb_bad", 1087), ("crc_ok", 5), ("llr_bad", 1173),
        ("llr_first", 1174), ("hre0", 1048), ("him0", 1049),
        ("hre1", 1050), ("him1", 1051), ("dmrs_c_init0", 1081),
        ("dmrs_c_init1", 1082), ("rv", 1083), ("qm", 1084),
        ("max_sym", 1170), ("n_llr", 1171),
    ],
    "ref_bits": [
        ("TB 比特", 1090, "tb"),
        ("码字硬判", 8, "cw"),
    ],
}


def stimulus_bytes():
    """协议输入，字节等同于 tools/nr_dl_mod.py dump-td 的输出。"""
    fd, path = tempfile.mkstemp(suffix=".td")
    os.close(fd)
    try:
        TX.dump_td(path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def golden():
    """判据锚点。只写"协议上应该是什么"，不写任何实现细节。"""
    tb = PLAN.payload_bits()
    qm, rv, rnti = TX.VARIANTS[0]          # dump_td 里的第一行 = 硬件跑的那一档
    ref = TX.dl_mod(tb, qm, rv, rnti)
    return {
        "scenario": "nr_pdsch",
        "anchors": {
            "tb_bad": 0,
            "crc_ok": 1,
            "llr_bad": 0,
            "llr_first": 0xFFFFFFFF,   # 没有坏符号
            "hre0": 16, "him0": 0,     # LS 估计 = H_SCALE（±1 容差）
            "hre1": 16, "him1": 0,
            "dmrs_c_init0": 0x120002,  # 38.211 7.4.1.1, n_ID=1, l=2/3
            "dmrs_c_init1": 0x180002,
            "rv": rv,
            "qm": qm,
            "max_sym": TX.QAM_AMP[qm],
            "n_llr": TX.E_BITS,
        },
        "tol": {"hre0": 1, "hre1": 1, "him0": 0, "him1": 0},
        "bits": {"tb": list(tb), "cw": ref["cw"]},
    }
