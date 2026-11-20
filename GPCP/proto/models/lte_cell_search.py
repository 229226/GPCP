"""lte_cell_search 的物理层参照（36.211 6.11.1.1 / 6.11.2.1）。

激励 = 62 点 PSS + 62 点 SSS 复数（理想序列 × 固定相位/幅度）；
黄金 = N_ID2=1、N_ID1=100、N_ID=301。
"""
import math
import os
import struct
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

N_ID1 = 100
N_ID2 = 1
GAIN = complex(0.6, 0.8)

SPEC = {
    "platform": {"dma": False, "busy": False},
    "scalars": [
        ("n_id_2", 0, "d"),
        ("n_id_1", 1, "d"),
        ("n_id", 2, "d"),
        ("pss_best", 3, "raw"),
        ("pss_second", 4, "raw"),
        ("sss_best", 5, "raw"),
        ("sss_second", 6, "raw"),
    ],
    "bits": [],
    "ref_scalars": [],
    "ref_bits": [],
}


def pss(u):
    return [complex(math.cos(-math.pi * u * n * (n + 1) / 63.0),
                    math.sin(-math.pi * u * n * (n + 1) / 63.0))
            for n in range(62)]


def _ms():
    x = [[0] * 31 for _ in range(6)]
    for i in range(6):
        x[i][4] = 1
    for i in range(26):
        x[0][i + 5] = (x[0][i + 2] + x[0][i]) & 1
        x[1][i + 5] = (x[1][i + 3] + x[1][i]) & 1
        x[2][i + 5] = (x[2][i + 4] + x[2][i + 2] + x[2][i + 1] + x[2][i]) & 1
        x[3][i + 5] = (x[3][i + 4] + x[3][i + 2] + x[3][i + 1] + x[3][i]) & 1
        x[4][i + 5] = (x[4][i + 4] + x[4][i + 3] + x[4][i + 1] + x[4][i]) & 1
        x[5][i + 5] = (x[5][i + 4] + x[5][i + 3] + x[5][i + 1] + x[5][i]) & 1
    return x


def sss(nid1, nid2):
    x = _ms()
    m0 = nid1 % 31
    m1 = (nid1 // 31 + m0 + 1) % 31
    out = []
    for n in range(31):
        s0 = 1 - 2 * x[0][(n + m0) % 31]
        s1 = 1 - 2 * x[1][(n + m1) % 31]
        c0 = 1 - 2 * x[2][(n + nid2) % 31]
        c1 = 1 - 2 * x[3][(n + nid2 + 3) % 31]
        z0 = 1 - 2 * x[4][(n + m0 % 8) % 31]
        out += [s0 * c0, s1 * c1 * z0]
    return out


def _pack_i(v):
    return struct.pack("<i", int(v))


def stimulus_bytes():
    p = pss([25, 29, 34][N_ID2])
    s = sss(N_ID1, N_ID2)
    out = bytearray()
    for c in p:
        out += _pack_i(round(c.real * 16 * GAIN.real - c.imag * 16 * GAIN.imag))
        out += _pack_i(round(c.real * 16 * GAIN.imag + c.imag * 16 * GAIN.real))
    for v in s:
        out += _pack_i(round(v * 1000 * GAIN.real))
        out += _pack_i(round(v * 1000 * GAIN.imag))
    for w in (N_ID1, N_ID2, 0):
        out += struct.pack("<I", w & 0xFFFFFFFF)
    return bytes(out)


def golden():
    return {
        "scenario": "lte_cell_search",
        "anchors": {"n_id_2": N_ID2, "n_id_1": N_ID1,
                    "n_id": 3 * N_ID1 + N_ID2,
                    "pss_best": 0, "pss_second": 0,
                    "sss_best": 0, "sss_second": 0},
        "bits": {},
    }
