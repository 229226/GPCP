"""wifi_11be 的物理层参照：802.11be EHT SU 20 MHz，MCS 0。

帧结构（7 个 256 点符号）：
  STF(1) + LTF(2) + EHT-SIG(1) + DATA(3)，CP=16，234 数据子载波。
  STF/LTF 用固定 Gold QPSK（c_init=0x57F/0x11BE）——本仓库的确定性参考，
  不是 IEEE 表（见 spec §5）。
  EHT-SIG：36 信息位 + 6 零尾，K=7 BCC（0o133/0o171），BPSK 1/2。
  DATA：324 信息位，802.11 N648 R1/2（Z=27），BPSK。
"""
import math
import os
import struct
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import wifi_ldpc                                        # noqa: E402
import pdsch_full_plan as F                             # noqa: E402
from ssb_plan import _fft                               # noqa: E402

NFFT = 256
CP = 16
NSYM = 7
NDATA = 234
QPSK = 2000
TD_SCALE = 256
SIG_INFO = 36
SIG_TAIL = 6
DATA_INFO = 324
DATA_CODED = 648
STF_INIT = 0x57F
LTF_INIT = 0x11BE

SPEC = {
    "platform": {"dma": False, "busy": False},
    "scalars": [
        ("sig_ok", 0, "d"),
        ("data_ok", 1, "d"),
        ("n_sig_bits", 2, "d"),
        ("n_data_bits", 3, "d"),
        ("ldpc_iters", 4, "d"),
        ("ldpc_syndrome", 5, "d"),
        ("sig_first", 6, "d"),
        ("data_first", 7, "d"),
    ],
    "bits": [
        ("EHT-SIG", 8, "sig", "36 信息位"),
        ("DATA", 48, "data", "324 信息位"),
    ],
    "ref_scalars": [],
    "ref_bits": [],
}


def _gold(c_init, n):
    x1 = [0] * (1600 + n + 31)
    x2 = [0] * (1600 + n + 31)
    x1[0] = 1
    for i in range(31):
        x2[i] = (c_init >> i) & 1
    for i in range(1600 + n):
        x1[i + 31] = (x1[i + 3] + x1[i]) & 1
        x2[i + 31] = (x2[i + 3] + x2[i + 2] + x2[i + 1] + x2[i]) & 1
    return [(x1[1600 + i] + x2[1600 + i]) & 1 for i in range(n)]


def _ref_qpsk(c_init):
    b = _gold(c_init, 2 * NDATA)
    return [complex((1 - 2 * b[2 * k]) * QPSK, (1 - 2 * b[2 * k + 1]) * QPSK)
            for k in range(NDATA)]


def _parity(x):
    x ^= x >> 4
    x ^= x >> 2
    x ^= x >> 1
    return x & 1


def _conv(bits):
    state = 0
    out = []
    for u in bits:
        reg = ((state << 1) | u) & 0x7F
        out += [_parity(reg & 0o133), _parity(reg & 0o171)]
        state = reg & 0x3F
    return out


def _tone(k):
    return (k + NFFT) & (NFFT - 1)


def _build_frame():
    sig = [0] * SIG_INFO
    data = [0] * DATA_INFO
    cw = wifi_ldpc.encode(data)
    sig_coded = _conv(sig + [0] * SIG_TAIL)
    syms = []
    # STF / LTF
    syms.append([_ref_qpsk(STF_INIT)[k] for k in range(NDATA)])
    ltf = _ref_qpsk(LTF_INIT)
    syms.append(ltf)
    syms.append(ltf)
    # EHT-SIG（BPSK）
    s3 = [0j] * NDATA
    for k, b in enumerate(sig_coded):
        s3[k] = complex((1 - 2 * b) * QPSK, 0)
    syms.append(s3)
    # DATA（BPSK，648 位铺到 3 个符号）
    m = 0
    for _ in range(3):
        row = [0j] * NDATA
        for k in range(NDATA):
            if m < DATA_CODED:
                row[k] = complex((1 - 2 * cw[m]) * QPSK, 0)
                m += 1
        syms.append(row)
    td = []
    for row in syms:
        freq = [0j] * NFFT
        idx = 0
        for k in list(range(-117, 0)) + list(range(1, 118)):
            freq[_tone(k)] = row[idx]
            idx += 1
        sig_td = _fft(freq, inverse=True)
        samples = [complex(int(round(c.real)), int(round(c.imag))) for c in sig_td]
        td.append(samples[-CP:] + samples)
    return sig, data, td


def stimulus_bytes():
    _sig, _data, td = _build_frame()
    out = bytearray()
    for blk in td:
        for c in blk:
            out += struct.pack("<I", (int(c.real) * TD_SCALE) & 0xFFFFFFFF)
            out += struct.pack("<I", (int(c.imag) * TD_SCALE) & 0xFFFFFFFF)
    return bytes(out)


def golden():
    sig, data, _td = _build_frame()
    return {
        "scenario": "wifi_11be",
        "anchors": {
            "sig_ok": 1,
            "data_ok": 1,
            "n_sig_bits": SIG_INFO,
            "n_data_bits": DATA_INFO,
            "ldpc_iters": 1,
            "ldpc_syndrome": 0,
            "sig_first": sig[0],
            "data_first": data[0],
        },
        "bits": {"sig": sig, "data": data},
    }
