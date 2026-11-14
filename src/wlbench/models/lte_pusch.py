"""lte_pusch 的物理层参照（36.212 Turbo + SC-FDMA）。

发射链与硬件共享同一组参数：K=104、E=800、2 符号 × 240 RE、40 Gold 导频/符号、
200 数据 RE/符号、200 点 DFT。接收侧硬件做 IDFT + Turbo 译码；本模型只提供
激励（TD）与黄金（TB/信息位/加扰码字），接收结果由硬件锚点对账。
"""
import math
import os
import struct
import sys
import importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

# 用文件路径加载同目录的 lte_pdsch 模型，避免把 models/ 加进 sys.path 后
# 遮蔽 tools/ 下的同名模块（emit.py --all 会顺序加载所有模型）。
_spec = importlib.util.spec_from_file_location(
    "_lte_pdsch_model", os.path.join(_HERE, "lte_pdsch.py"))
DL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(DL)

N_RNTI = DL.N_RNTI
NRE = DL.NRE
NPILOT = DL.NPILOT
CRS_STEP = DL.CRS_STEP
NFFT = DL.NFFT
BIN0 = DL.BIN0
TD_SCALE = DL.TD_SCALE
QPSK = DL.QPSK
NDATA = DL.NDATA
RV = 0

SPEC = {
    "platform": {"transfer": False, "busy": False},
    "scalars": [
        ("crc_ok", 0, "d"),
        ("tb_ok", 1, "d"),
        ("rv", 2, "d"),
    ],
    "bits": [
        ("译码信息位", 4, "hard", "104 位（TB+CRC24A）"),
        ("TB 比特", 112, "tb", ""),
        ("e_sc（加扰码字）", 192, "e_sc", "与模型发射链逐位"),
    ],
    "ref_scalars": [],
    "ref_bits": [],
}


def _pilot(l):
    c = DL.gold(((N_RNTI << 14) + 1 + l) & 0x7FFFFFFF, 2 * NPILOT)
    return [complex((1 - 2 * c[2 * p]) * QPSK, (1 - 2 * c[2 * p + 1]) * QPSK)
            for p in range(NPILOT)]


def _dft(x):
    import math
    out = []
    n = len(x)
    for k in range(n):
        acc = 0j
        for m in range(n):
            acc += x[m] * complex(math.cos(-2 * math.pi * k * m / n),
                                  math.sin(-2 * math.pi * k * m / n))
        out.append(acc / n)
    return out


def build():
    tx = DL.transmit()
    grid = [[0j] * NRE for _ in range(DL.NSYM)]
    for s in range(DL.NSYM):
        data = tx["sym"][s * NDATA:(s + 1) * NDATA]
        spec = _dft(data)
        pil = _pilot(s)
        p = 0
        m = 0
        for r in range(NRE):
            if r % CRS_STEP == 0:
                grid[s][r] = pil[p]
                p += 1
            else:
                grid[s][r] = spec[m]
                m += 1
    td = DL.td_from_grid(grid)
    payload = bytearray()
    for s in range(DL.NSYM):
        for c in td[s]:
            payload += struct.pack("<I", (int(c.real) * TD_SCALE) & 0xFFFFFFFF)
            payload += struct.pack("<I", (int(c.imag) * TD_SCALE) & 0xFFFFFFFF)
    words = [0, 0, 0, RV]
    for i, b in enumerate(tx["tb"]):
        words[i // 32] |= (b & 1) << (i % 32)
    for w in words:
        payload += struct.pack("<I", w & 0xFFFFFFFF)
    return bytes(payload), tx


def stimulus_bytes():
    data, _tx = build()
    return data


def golden():
    _data, tx = build()
    return {
        "scenario": "lte_pusch",
        "anchors": {"crc_ok": 1, "tb_ok": 1, "rv": RV},
        "bits": {"hard": list(tx["info"]), "tb": list(tx["tb"]),
                 "e_sc": list(tx["e_sc"])},
    }
