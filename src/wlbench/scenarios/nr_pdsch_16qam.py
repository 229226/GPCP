"""NR PDSCH 16QAM 变体（Qm=4，Rel-19 条文口径）。

纯接收：激励是 2 符号时域样本（工具生成的 QPSK DMRS + 16QAM 数据），
尾部 7 字带 TB/qm/rv/RNTI；硬件做 FFT -> LS -> 16QAM max-log -> LDPC。
"""
from wlbench.hll import scenario

S = scenario("nr_pdsch_16qam", rat="nr", direction="dl")

S.cfg(oversample=2, bw_mhz=20, scs_khz=15, n_id=1, n_rnti=0x1234,
      a_bits=80, e_bits=720, qm=4, layers=1, nsym=2)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

wave = S.received(form="td", length=S.sym_len(2, 7))
ref = S.dmrs(type="type1", n_id=1, l=2)
h = S.estimate(wave, ref)
llr = S.equalise_demap(wave, h, qm=4)
hard = S.decode(llr, scheme="ldpc", max_iter=8)

S.expect(tb_eq="tx.tb", crc="crc24a", hard_eq="tx.codeword", llr_sign="all")
S.budget(slot_ms=1.0, on_time=None)
