"""LTE PDSCH 的物理层参照（36.212 / 36.211），阶段 3「跨制式」的基准。

口径（刻意保留的简化，报告里会写明）：
  * 单码块 K=104（80 bit TB + CRC24A），Turbo 1/3、QPP (f1,f2)=(7,26)
    —— 参数取自 36.212 Table 5.1.3-3；
  * 尾比特用**零终止**（3 拍冲洗 + 1 个 0），不是 36.212 的 exact tail packing；
  * 2 个 OFDM 符号、240 RE/符号、CRS 每 6 个 RE 一个（单端口，40 导频/符号），
    数据 RE 200/符号，E = 800 bit；无 DC 空置、无层映射/预编码；
  * 加扰 c_init = n_RNTI << 14（36.211 7.3.1.1），CRS c_init 按 6.10.1.1。
"""
import math
import random
import struct
import sys
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import pdsch_full_plan as F                              # noqa: E402  (float FFT/IFFT)
import nr_pbch as NR                                     # noqa: E402  (add_cp)

# ------------------------------------------------------------------ 参数
A_TB = 80
CRC_LEN = 24
K = A_TB + CRC_LEN               # 104
TAIL = 4                         # 每个流 K+4
NPOL = K + TAIL
F1, F2 = 7, 26                   # 36.212 Table 5.1.3-3, K=104
E_BITS = 800
NSYM = 2
NRE = 240
CRS_STEP = 6
NPILOT = NRE // CRS_STEP         # 40
NDATA = NRE - NPILOT             # 200
NFFT = 4096
CP_LEN = 288
BIN0 = NFFT // 2 + 1
QPSK = 2000
TD_SCALE = 256
N_RNTI = 0x1234
N_ID = 1
ITERS = 8
DEFAULT_SEED = 1

RV = 0


def crc24a(bits):
    reg = 0
    for b in bits:
        reg ^= (b & 1) << 23
        for _ in range(24):
            reg = ((reg << 1) ^ 0x864CFB) & 0xFFFFFF if reg & 0x800000 \
                else (reg << 1) & 0xFFFFFF
    return [(reg >> (23 - i)) & 1 for i in range(24)]


def qpp(n=K):
    return [(F1 * i + F2 * i * i) % n for i in range(n)]


def rsc(state, u):
    a1 = (state >> 2) & 1
    a2 = (state >> 1) & 1
    a3 = state & 1
    ak = u ^ a2 ^ a3
    return ((ak << 2) | (a1 << 1) | a2), ak, (ak ^ a1 ^ a3)


def tail_bits(bits):
    """按 36.212 的口径：用 3 拍把第一个 RSC 冲洗到零态，取这 3 个输入。"""
    st = 0
    for u in bits:
        st, ak, pk = rsc(st, u)
    out = []
    for _ in range(3):
        a2, a3 = (st >> 1) & 1, st & 1
        u = a2 ^ a3
        st, ak, pk = rsc(st, u)
        out.append(u)
    return out


def rsc_encode(bits, tails):
    """RSC 编码 + 尾部 4 位（3 个冲洗输入 + 1 个 0），返回 (D, P)。"""
    d, p, st = [], [], 0
    for u in list(bits) + list(tails):
        st, ak, pk = rsc(st, u)
        d.append(u)
        p.append(pk)
    d.append(0)                        # 第 4 个尾比特
    p.append(0)
    return d, p


def turbo_encode(c):
    perm = qpp()
    c_int = [c[perm[i]] for i in range(K)]
    t = tail_bits(c)
    d0, p1 = rsc_encode(c, t)
    d1, p2 = rsc_encode(c_int, t)
    return d0, p1, p2                  # 两个编码器共用同一组尾比特


# ---------------------------------------------------------- 速率匹配 36.212 5.1.4.1
SUB_P = [0, 16, 8, 24, 4, 20, 12, 28, 2, 18, 10, 26, 6, 22, 14, 30,
         1, 17, 9, 25, 5, 21, 13, 29, 3, 19, 11, 27, 7, 23, 15, 31]


def subblock(stream):
    """32 列、行写入、列置换、列读出；返回去掉 NULL 后的序列与位置表。"""
    n = len(stream)
    rows = (n + 31) // 32
    mat = [[None] * 32 for _ in range(rows)]
    for i, v in enumerate(stream):
        mat[i // 32][i % 32] = v
    out = []
    for p in SUB_P:
        for r in range(rows):
            v = mat[r][p]
            if v is not None:
                out.append(v)
    return out


def rate_match(d0, p1, p2, rv=RV):
    v0 = subblock(d0)
    v1 = subblock(p1)
    v2 = subblock(p2)
    w = list(v0)
    for i in range(len(v1)):
        w.append(v1[i])
        w.append(v2[i])
    kw = len(w)
    k0 = rv * (kw // 4)
    return [w[(k0 + j) % kw] for j in range(E_BITS)]


def scrambling(n):
    """36.211 7.3.1.1, c_init = n_RNTI << 14, length-31 Gold."""
    cinit = (N_RNTI << 14) & 0x7FFFFFFF
    nc = 1600
    x1 = [0] * (nc + n + 31)
    x2 = [0] * (nc + n + 31)
    x1[0] = 1
    for i in range(31):
        x2[i] = (cinit >> i) & 1
    for i in range(nc + n):
        x1[i + 31] = (x1[i + 3] + x1[i]) & 1
        x2[i + 31] = (x2[i + 3] + x2[i + 2] + x2[i + 1] + x2[i]) & 1
    return [(x1[nc + i] + x2[nc + i]) & 1 for i in range(n)]


def crs_cinit(l):
    """36.211 6.10.1.1, normal CP -> N_CP=1, n_s=0."""
    return ((7 * (0 + 1) + l + 1) * (2 * N_ID + 1) + 2 * N_ID + 1) & 0x7FFFFFFF


def gold(cinit, n):
    nc = 1600
    x1 = [0] * (nc + n + 31)
    x2 = [0] * (nc + n + 31)
    x1[0] = 1
    for i in range(31):
        x2[i] = (cinit >> i) & 1
    for i in range(nc + n):
        x1[i + 31] = (x1[i + 3] + x1[i]) & 1
        x2[i + 31] = (x2[i + 3] + x2[i + 2] + x2[i + 1] + x2[i]) & 1
    return [(x1[nc + i] + x2[nc + i]) & 1 for i in range(n)]


def crs_symbols(l):
    b = gold(crs_cinit(l), 2 * NPILOT)
    return [complex((1 - 2 * b[2 * p]) * QPSK, (1 - 2 * b[2 * p + 1]) * QPSK)
            for p in range(NPILOT)]


# ------------------------------------------------------------------ 发射链
def build_tb(seed=0x1234):
    rnd = random.Random(seed)
    return [rnd.randrange(2) for _ in range(A_TB)]


def transmit(tb=None, rv=RV):
    tb = build_tb() if tb is None else list(tb)
    info = tb + crc24a(tb)
    d0, p1, p2 = turbo_encode(info)
    e = rate_match(d0, p1, p2, rv)
    scr = scrambling(E_BITS)
    e_sc = [a ^ b for a, b in zip(e, scr)]
    sym = [complex((1 - 2 * e_sc[2 * i]) * QPSK, (1 - 2 * e_sc[2 * i + 1]) * QPSK)
           for i in range(E_BITS // 2)]
    return dict(tb=tb, info=info, d0=d0, p1=p1, p2=p2, e=e, e_sc=e_sc, sym=sym)


def build_grid(tx):
    """2 符号网格：CRS 每 6 个 RE 一个，其余按顺序放数据符号。"""
    grid = [[0j] * NRE for _ in range(NSYM)]
    m = 0
    for s in range(NSYM):
        crs = crs_symbols(s)
        p = 0
        for r in range(NRE):
            if r % CRS_STEP == 0:
                grid[s][r] = crs[p]
                p += 1
            else:
                grid[s][r] = tx["sym"][m]
                m += 1
    return grid, m


def td_from_grid(grid):
    out = []
    for s in range(NSYM):
        freq = [0j] * NFFT
        for r in range(NRE):
            freq[BIN0 + r] = grid[s][r]
        sig = F._fft(freq, inverse=True)
        samples = [complex(int(round(c.real)), int(round(c.imag))) for c in sig]
        out.append(NR.add_cp(samples, CP_LEN))
    return out


# ------------------------------------------------------------------ 接收链
def receive(td, rv=RV, iters=ITERS):
    """FFT -> CRS LS -> 均衡 -> LLR -> 解扰 -> 解速率匹配 -> Turbo 译码。"""
    h = complex(2, 1) if False else complex(1, 0)
    y = []
    for s in range(NSYM):
        blk = td[s][CP_LEN:]
        freq = F._fft(list(blk), inverse=False)
        # FFT 输出按自然序；取 240 个 RE
        y.append([freq[BIN0 + r] for r in range(NRE)])
    # CRS LS（已知 CRS 序列）
    hh = []
    for s in range(NSYM):
        crs = crs_symbols(s)
        acc = 0j
        for p in range(NPILOT):
            acc += y[s][p * CRS_STEP] * crs[p].conjugate()
        hh.append(acc / (NPILOT * QPSK * QPSK))
    # 均衡 + QPSK 软 LLR（交织顺序即 E 顺序）
    llr = []
    for s in range(NSYM):
        for r in range(NRE):
            if r % CRS_STEP == 0:
                continue
            eq = y[s][r] * hh[s].conjugate()
            llr.append(eq.real)
            llr.append(eq.imag)
    assert len(llr) == E_BITS, len(llr)
    scr = scrambling(E_BITS)
    llr = [-v if scr[k] else v for k, v in enumerate(llr)]
    e = de_rate_match(llr)
    hard = turbo_decode(e, iters)
    tb = [int(v) for v in hard[:A_TB]]
    crc_ok = crc24a(tb) == [int(v) for v in hard[A_TB:A_TB + CRC_LEN]]
    return dict(hard=[int(v) for v in hard], tb=tb, crc_ok=crc_ok, h=hh,
                llr=llr)


def de_rate_match(llr):
    """E 个 LLR 按循环缓冲坐标合并回 Kw 长的 d0/p1/p2 序列。"""
    # 建一张 位置 -> 序列号 的表，与 rate_match 的读出顺序一致
    d0 = [0] * NPOL
    p1 = [0] * NPOL
    p2 = [0] * NPOL
    # 反查：w 的每个位置属于哪个流、哪个下标
    idx_map = []
    v0 = subblock(list(range(NPOL)))
    v1 = subblock(list(range(NPOL)))
    v2 = subblock(list(range(NPOL)))
    # subblock 返回的是"输出位置上的原下标"，反查需要 原下标->输出位置
    pos0 = [0] * NPOL
    for out_i, orig in enumerate(v0):
        pos0[orig] = out_i
    pos1 = [0] * NPOL
    for out_i, orig in enumerate(v1):
        pos1[orig] = out_i
    pos2 = [0] * NPOL
    for out_i, orig in enumerate(v2):
        pos2[orig] = out_i
    kw = 3 * NPOL
    acc = [0.0] * kw
    for k in range(E_BITS):
        acc[(RV * (kw // 4) + k) % kw] += llr[k]
    for i in range(NPOL):
        d0[i] = acc[pos0[i]]
        p1[i] = acc[NPOL + 2 * pos1[i]]
        p2[i] = acc[NPOL + 2 * pos2[i] + 1]
    return d0, p1, p2


# ------------------------------------------------------------- Turbo 译码（max-log-MAP）
def _siso(sys_l, par_l, apr, n, end_zero=True):
    """max-log-MAP SISO；end_zero=True 时终点强制状态 0。返回 (外信息, 硬判)。"""
    NEG = -1e18
    alpha = [[NEG] * 8 for _ in range(n + 1)]
    beta = [[NEG] * 8 for _ in range(n + 1)]
    alpha[0][0] = 0.0
    for k in range(n):
        for s in range(8):
            if alpha[k][s] == NEG:
                continue
            for u in (0, 1):
                ns, ak, pk = rsc(s, u)
                v = (alpha[k][s] + (1 if u else -1) * (sys_l[k] + apr[k])
                     + (1 if pk else -1) * par_l[k])
                if v > alpha[k + 1][ns]:
                    alpha[k + 1][ns] = v
    if end_zero:
        beta[n][0] = 0.0
    else:
        beta[n] = [0.0] * 8
    for k in range(n - 1, -1, -1):
        for s in range(8):
            best = NEG
            for u in (0, 1):
                ns, ak, pk = rsc(s, u)
                v = (beta[k + 1][ns] + (1 if u else -1) * (sys_l[k] + apr[k])
                     + (1 if pk else -1) * par_l[k])
                if v > best:
                    best = v
            beta[k][s] = best
    ext = [0.0] * n
    hard = [0] * n
    for k in range(n):
        b = [NEG, NEG]
        for s in range(8):
            if alpha[k][s] == NEG:
                continue
            for u in (0, 1):
                ns, ak, pk = rsc(s, u)
                v = (alpha[k][s] + beta[k + 1][ns]
                     + (1 if u else -1) * (sys_l[k] + apr[k])
                     + (1 if pk else -1) * par_l[k])
                if v > b[u]:
                    b[u] = v
        post = b[1] - b[0]
        hard[k] = 1 if post > 0 else 0
        ext[k] = post - (sys_l[k] + apr[k])
    return ext, hard


def turbo_decode(e, iters=ITERS):
    """e = (d0, p1, p2)，各 NPOL 个 LLR；只译前 K 个信息位。"""
    d0, p1, p2 = e
    perm = qpp()
    n = K + 3                       # 含 3 拍零终止，第 4 个尾比特不参与网格
    # 与全仓库统一的 LLR 约定（负 = 1）→ 译码器内部用正 = 1
    L = [-float(v) for v in d0[:n]]
    par1 = [-float(v) for v in p1[:n]]
    par2 = [-float(v) for v in p2[:n]]
    e1 = [0.0] * n
    for _ in range(iters):
        e1, _h1 = _siso(L, par1, [0.0] * n, n)
        apr2 = [e1[perm[i]] for i in range(K)] + [0.0] * 3
        sys2 = [L[perm[i]] for i in range(K)] + L[K:n]
        e2, _h2 = _siso(sys2, par2, apr2, n, end_zero=False)
        e2d = [0.0] * (K + 3)
        for i in range(K):
            e2d[perm[i]] = e2[i]
        L = [L[i] + e1[i] + e2d[i] for i in range(K)] + L[K:n]
    hard = [1 if L[i] > 0 else 0 for i in range(K)]
    hard += [1 if L[i] > 0 else 0 for i in range(K, n)]
    hard += [0]                     # 第 4 个尾比特
    return hard


# ------------------------------------------------------------------ 激励
def dump_td_bin(path, seed=DEFAULT_SEED, snr_db=None):
    tx = transmit()
    grid, used = build_grid(tx)
    assert used == E_BITS // 2, used
    td = td_from_grid(grid)
    if snr_db is not None:
        rnd = random.Random(seed)
        p_sig = sum(abs(v) ** 2 for row in grid for v in row) / (NSYM * NRE)
        sigma = math.sqrt(p_sig / (10 ** (snr_db / 10)) / 2)
        for s in range(NSYM):
            td[s] = [c + complex(int(round(rnd.gauss(0, sigma))),
                                 int(round(rnd.gauss(0, sigma)))) for c in td[s]]
    with open(path, "wb") as f:
        for s in range(NSYM):
            for c in td[s]:
                f.write(struct.pack("<I", (int(c.real) * TD_SCALE) & 0xFFFFFFFF))
                f.write(struct.pack("<I", (int(c.imag) * TD_SCALE) & 0xFFFFFFFF))
    return tx, td


def golden():
    tx = transmit()
    td = td_from_grid(build_grid(tx)[0])
    rx = receive([[complex(int(round(c.real)), int(round(c.imag))) for c in blk]
                  for blk in td])
    return tx, rx


if __name__ == "__main__":
    tx, rx = golden()
    print("TB ok:", rx["tb"] == tx["tb"], "CRC:", rx["crc_ok"])
    print("hard ok:", rx["hard"] == tx["info"], sum(
        1 for a, b in zip(rx["hard"], tx["info"]) if a != b), "bits differ")
    print("h:", rx["h"])


# ------------------------------------------------------------------ 基准接口
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


def build():
    """激励（2 符号 TD + 尾部 4 字）与黄金（TB / 信息位 / e_sc）。"""
    tx = transmit()
    grid, used = build_grid(tx)
    assert used == E_BITS // 2
    td = td_from_grid(grid)
    payload = bytearray()
    for s in range(NSYM):
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
        "scenario": "lte_pdsch",
        "anchors": {"crc_ok": 1, "tb_ok": 1, "rv": RV},
        "bits": {"hard": list(tx["info"]), "tb": list(tx["tb"]),
                 "e_sc": list(tx["e_sc"])},
    }
