"""协议描述层（HLL）——docs/20 第 2 节动词的实现。

设计约束（必须保持）：
  * 本模块只记录"协议语义节点"，不出现任何硬件概念；
  * 所有物理决策（切块/缓冲/并行度/微码）属于 proto/lower.py 与 cgra/target.yaml；
  * 相同场景对任何 target 产生的 IR 必须逐字节相同（docs/20 §4）。

场景文件只允许：from proto.hll import scenario，然后按动词顺序调用。
"""

# 3GPP 带宽 -> 资源块（38.101-1 传输带宽配置）。未知组合直接报错，
# 逼着使用者补表，而不是猜一个数。
_N_RB = {
    (20, 15): 106, (50, 15): 270, (100, 15): 273,
    (20, 30): 51, (50, 30): 133, (100, 30): 273,
}

# 算子库当前提供的 OFDM 点数（cgra/kernels/fft_*.yaml 能覆盖的规模）
_KERNEL_FFT = (256, 1024, 4096)


def _derive(cfg):
    """把协议配置推导成波形级参数。v1 只推导"带宽决定"的那几个量；
    传输块大小/码块数等需要 38.214 表，留到阶段 1（见 docs/20 §7）。"""
    bw = cfg.get("bw_mhz")
    scs = cfg.get("scs_khz")
    d = {}
    # oversample：传输带宽配置可以按 1/2/4 倍最小 FFT 采样。这是协议参数
    # （20 MHz@15 kHz 最少 2048 点，2 倍过采样即 4096 点 / 30.72 Msps），
    # 不是"因为某套镜像只有 4096 点"——那样等于把硬件抄进协议。
    ov = int(cfg.get("oversample", 1) or 1)
    if ov not in (1, 2, 4):
        raise ValueError("cfg: oversample 只支持 1/2/4")
    # WiFi（802.11be）直接给 FFT 点数/CP/数据子载波：这些是协议 numerology，
    # 不是芯片参数；不在 3GPP 的带宽表里。
    if "fft_points" in cfg:
        n = int(cfg["fft_points"])
        d["n_fft_min"] = n
        d["cp_len"] = int(cfg.get("cp_len", int(round(n * 0.0703125))))
        d["n_subcarriers"] = int(cfg.get("n_data_subcarriers", 0))
    if bw is not None and scs is not None:
        key = (bw, scs)
        if key not in _N_RB:
            raise ValueError(
                "cfg: 带宽/子载波间隔组合 %s 未登记（38.101-1），请先补 _N_RB 表" % (key,))
        n_rb = _N_RB[key]
        need = n_rb * 12
        d["n_rb"] = n_rb
        d["n_subcarriers"] = need
        # 最小 FFT 点数（取 2 的幂）
        n = 1
        while n < need:
            n <<= 1
        d["n_fft_min"] = n * ov
        # 常规 CP ≈ 7% 符号；过采样时 CP 同步放大
        d["cp_len"] = int(round(n * 0.0703125)) * ov
    if ov != 1:
        d["oversample"] = ov
    for k in ("n_id", "n_rnti", "qm", "al", "reg", "e_bits", "a_bits"):
        if k in cfg:
            d[k] = cfg[k]
    return d


class Tensor:
    """协议层的"一段数据"。只有用途、长度、语义单位，没有地址。"""

    def __init__(self, name, kind, length, **attrs):
        self.name = name
        self.kind = kind
        self.length = length
        self.attrs = attrs

    def __repr__(self):
        return "<%s:%s %s>" % (self.kind, self.name, self.length)


class Scenario:
    def __init__(self, name, rat, direction):
        self.name = name
        self.rat = rat
        self.direction = direction
        self.cfg_kw = {}
        self.derived = {}
        self.traffic_kw = None
        self.nodes = []
        self.golden = []
        self.budget_kw = {}
        self._n = 0

    # ---------------------------------------------------------------- 记录
    def _node(self, op, ins, out, **params):
        t = Tensor(out, op, params.pop("length", 0), **params)
        self.nodes.append({
            "id": self._n,
            "op": op,
            "in": [x.name for x in ins],
            "out": out,
            "length": t.length,
            "params": params,
        })
        self._n += 1
        return t

    # ---------------------------------------------------------------- 动词
    def cfg(self, **kw):
        self.cfg_kw = dict(kw)
        self.derived = _derive(kw)
        return self

    def traffic(self, **kw):
        self.traffic_kw = dict(kw)
        return self

    def transport_block(self, bits, rnti=None):
        return self._node("transport_block", [], "tb", length=bits, rnti=rnti)

    def received(self, form="td", length=0, **attrs):
        """接收波形来自激励（外部设备已经发过），不是本程序算出来的。
        CSI/PRACH/PUCCH/PUSCH/SSB/MIMO 这些接收类基准都从这里开始。"""
        return self._node("received", [], "wave", form=form, length=length, **attrs)

    def encode(self, x, scheme, **kw):
        return self._node("encode", [x], "cw", scheme=scheme,
                          length=kw.pop("bits", self.derived.get("e_bits", 0)), **kw)

    def modulate(self, cw, qm=None, scrambler="gold"):
        return self._node("modulate", [cw], "sym", qm=qm, scrambler=scrambler,
                          length=cw.length // max(1, (qm or 2)) if cw.length else 0)

    def dmrs(self, type="type1", n_id=None, l=None, **kw):
        return self._node("dmrs", [], "ref", type=type, n_id=n_id, l=l, **kw)

    def correlate(self, src, ref, hypotheses=1, seq="zc", metric="energy"):
        """在 src 上与参考序列做相关搜索，输出每个候选假设的能量。
        用于 PRACH 的 139 个循环移位、PUCCH 的 2 个 m_CS、SSB 的 PSS/SSS 假设。"""
        return self._node("correlate", [src, ref], "corr", hypotheses=hypotheses,
                          seq=seq, metric=metric, length=hypotheses)

    def map_to_grid(self, sym, dmrs=None):
        return self._node("map_to_grid", [sym] + ([dmrs] if dmrs else []), "grid",
                          length=self.derived.get("n_subcarriers", 0))

    def extract(self, src, **kw):
        return self._node("extract", [src], "re", **kw)

    def measure(self, grid, ref, report="cqi", **kw):
        """用参考信号做信道/干扰测量并给出上报量（CSI 的宽带 CQI）。"""
        return self._node("measure", [grid, ref], "report", report=report,
                          length=1, **kw)

    def ofdm_modulate(self, grid):
        return self._node("ofdm_modulate", [grid], "wave",
                          length=2 * (self.derived.get("cp_len", 0)
                                      + self.derived.get("n_fft_min", 0)))

    def ofdm_demodulate(self, wave):
        return self._node("ofdm_demodulate", [wave], "grid")

    def channel(self, wave, model="awgn", **kw):
        return self._node("channel", [wave], "y", model=model, **kw)

    def estimate(self, y, ref, includes_fft=None):
        """信道估计。`includes_fft` 写进 IR：输入是时域样本时，本动词内部
        含一次接收 FFT。语义由场景声明，后端不再从图上推断。"""
        if includes_fft is None:
            includes_fft = not any(n["op"] == "ofdm_demodulate" for n in self.nodes)
        return self._node("estimate", [y, ref], "h",
                          includes_fft=bool(includes_fft))

    def sym_len(self, nsym=1, extra=0):
        """nsym 个 OFDM 符号的时域样本数（I/Q 交织的 32-bit 字数）。"""
        d = self.derived
        return (2 * int(nsym) * (int(d.get("cp_len", 0)) + int(d.get("n_fft_min", 0)))
                + int(extra))

    def equalise_demap(self, y, h, qm=None):
        return self._node("equalise_demap", [y, h], "llr",
                          qm=qm, length=self.derived.get("e_bits", 0))

    def equalise(self, y, h, qm=None):
        """均衡：每个 RE 出一组软符号（匹配滤波 / MMSE 求解）。"""
        return self._node("equalise", [y, h], "eq", qm=qm,
                          length=self.derived.get("e_bits", 0))

    def transform(self, x, form="dft", size=0, inverse=False):
        """显式变换： DFT/IDFT/FFT。不再藏在其他动词内部。"""
        n = int(size) if size else int(self.derived.get("n_fft_min", 0))
        return self._node("transform", [x], "spec", form=form, size=n,
                          inverse=bool(inverse), length=2 * n)

    def demap(self, x, qm=None):
        """解交织 + 解扰 + 软解调，输出 LLR。"""
        return self._node("demap", [x], "llr", qm=qm,
                          length=self.derived.get("e_bits", 0))

    def mimo_equalise(self, y, h, qm=None, layers=2, rx=2, bits=None):
        """多天线均衡（MMSE）后硬判：每层每 RE 出 Qm 个比特。"""
        n = bits if bits is not None else self.derived.get("e_bits", 0)
        return self._node("mimo_equalise", [y, h], "hard", qm=qm, layers=layers,
                          rx=rx, length=n)

    def decode(self, llr, scheme, max_iter=8):
        return self._node("decode", [llr], "hard", scheme=scheme,
                          max_iter=max_iter,
                          length=self.derived.get("a_bits", 0))

    def expect(self, **anchors):
        self.golden.append({k: (v if isinstance(v, (str, int, float, bool)) else str(v))
                            for k, v in anchors.items()})
        return self

    def budget(self, **kw):
        self.budget_kw = dict(kw)
        return self

    # ---------------------------------------------------------------- 导出
    def ir(self):
        return {
            "schema": "gpcp-proto-ir/1",
            "scenario": self.name,
            "rat": self.rat,
            "direction": self.direction,
            "cfg": self.cfg_kw,
            "derived": self.derived,
            "traffic": self.traffic_kw,
            "nodes": self.nodes,
            "golden": self.golden,
            "budget": self.budget_kw,
        }

    def ir_bytes(self):
        """确定性的序列化：同场景对任何 target 都必须得到相同字节（docs/20 §4）。"""
        import json
        return json.dumps(self.ir(), sort_keys=True, indent=1,
                          ensure_ascii=False).encode("utf-8")


def scenario(name, rat="nr", direction="dl"):
    return Scenario(name, rat, direction)
