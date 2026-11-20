"""nr_pdsch_64qam 的物理层参照：Qm=6，纯接收，口径同 16QAM。"""
import os
import struct
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
_MODELS = os.path.join(_TOOLS, "models")
if _MODELS not in sys.path:
    sys.path.insert(0, _MODELS)
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import nr_dl_mod as TX                                   # noqa: E402
import nr_pdsch_amc as AMC                               # noqa: E402
import pdsch_full_plan as F                              # noqa: E402

QM = 6
MCS = 20
MAX_SYM = 436
N_RNTI = 0x1234
MAGIC = 0x51414D31
SNR = 25.0
SEED = 1

SPEC = {
    "scalars": [
        ("tb_bad", 0, "d"),
        ("crc_ok", 1, "d"),
        ("llr_bad", 2, "raw"),
        ("llr_first", 3, "raw"),
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
        ("已传码字硬判", 94, "cw", "E=720 只覆盖前 984 个可用码字位"),
    ],
    "ref_scalars": [],
    "ref_bits": [],
}


def _build():
    tb = AMC.M.payload_bits()
    grid, _k0, qm, _n = AMC.build_grid(MCS, 0, tb)
    assert qm == QM
    y = AMC.add_channel(grid, qm, SNR, SEED)
    td = F.td_from_y(y)
    out = bytearray()
    for s in range(F.NSYM):
        for c in td[s]:
            out += struct.pack("<I", (int(c.real) * F.TD_SCALE) & 0xFFFFFFFF)
            out += struct.pack("<I", (int(c.imag) * F.TD_SCALE) & 0xFFFFFFFF)
    words = [0, 0, 0, QM, 0, N_RNTI, MAGIC]
    for i, b in enumerate(tb):
        words[i // 32] |= (b & 1) << (i % 32)
    for w in words:
        out += struct.pack("<I", w & 0xFFFFFFFF)
    return bytes(out), tb


def stimulus_bytes():
    data, _tb = _build()
    return data


def golden():
    tb = AMC.M.payload_bits()
    g = AMC.golden(MCS, 0, SNR, SEED)
    h = g["h"]
    return {
        "scenario": "nr_pdsch_64qam",
        "anchors": {
            "tb_bad": sum(a != b for a, b in zip(g["tb"], tb)),
            "crc_ok": 1 if g["crc_ok"] else 0,
            "llr_bad": 0,
            "llr_first": 0xFFFFFFFF,
            "hre0": int(h[0].real), "him0": int(h[0].imag),
            "hre1": int(h[1].real), "him1": int(h[1].imag),
            "dmrs_c_init0": 0x120002,
            "dmrs_c_init1": 0x180002,
            "rv": 0,
            "qm": QM,
            "max_sym": MAX_SYM,
            "n_llr": TX.E_BITS,
        },
        "tol": {"hre0": 2, "hre1": 2, "him0": 2, "him1": 2},
        "bits": {"tb": list(g["tb"]), "cw": list(g["hard"][:984])},
    }
