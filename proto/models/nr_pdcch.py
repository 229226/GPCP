"""nr_pdcch 的物理层参照（只给 proto/emit.py 与 proto/proto_eval.py 用）。

分工（docs/20 §4）：
  * 激励 = 协议输入（DCI、n_ID、RNTI 候选）。**绝不含**期望符号；复用已验收的
    tools/nr_pdcch_tx.py dump-td，所以协议运行器和手写 benchmark 吃同一份字节。
  * 黄金 = tools/nr_pdcch_tx.py 的发射链（B3，已被 run-pdcch-tx 逐值验收）。
  * 参照门 = 人写的同一协议 benchmark（docs/20 §7 阶段 0 的等价判据）。
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import nr_pdcch_tx as TX                                 # noqa: E402

# 阶段 0 的等价参照：同一个协议的"人写"实现（sim/soc 的目标名 + 它的 dump 路径）
REF_GATE = "run-pdcch-loop"
REF_DUMP = "sim/soc/build/pdcch_loop_dump.txt"


def stimulus_bytes():
    """协议输入，字节等同于 tools/nr_pdcch_tx.py dump-td 的输出。"""
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
    ref = TX.pdcch_tx(TX.FIELDS, TX.VARIANTS[0])
    dci_word = 0
    for b in ref["dci"]:
        dci_word = (dci_word << 1) | (b & 1)
    return {
        "scenario": "nr_pdcch",
        "anchors": {
            "dci_tx": dci_word,        # 发射的 DCI（MSB-first 打包）
            "dci_rx": dci_word,        # 译码后必须相同
            "rnti": ref["rnti"],
            "rnti_match": ref["rnti"],  # 盲检必须命中这个
            "crc_ok": 1,
            "llr_bad": 0,              # 符号与发射比特不符的 LLR 数
            "n_id": TX.N_ID,
            "max_sym": TX.QAM,         # QPSK 幅度
        },
        # 定点增益决定的幅值窗口（不是逐值判据；符号与非空才是硬门）
        "windows": {
            "llr_amin": [TX.QAM // 2, TX.QAM * 3 // 2],
            "llr_amax": [TX.QAM, TX.QAM * 2],
        },
        "bits": {"dci_tx": ref["dci"], "e_sc": ref["e_sc"], "crc": ref["crc"]},
    }
