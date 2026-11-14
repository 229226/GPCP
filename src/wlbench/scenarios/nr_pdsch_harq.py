"""NR PDSCH HARQ：rv=0 / rv=2 两次传输，Chase 合并后 LDPC。

纯接收场景：两次传输的时域样本已在激励里（tools/pdsch_full_plan.py
dump_td_bin_harq 的 transmission-major 格式），本程序只做接收与合并。
"""
from wlbench.hll import scenario

S = scenario("nr_pdsch_harq", rat="nr", direction="dl")

S.cfg(bw_mhz=20, scs_khz=15, n_id=1, n_rnti=0x1234,
      a_bits=80, e_bits=720, qm=2, rv=(0, 2), harq_tx=2, nsym=2)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

w0 = S.received(form="td", length=2 * (288 + 4096) * 2)
ref = S.dmrs(type="type1", n_id=1, l=2)
g0 = S.ofdm_demodulate(w0)
h0 = S.estimate(g0, ref)
l0 = S.equalise_demap(g0, h0, qm=2)

w1 = S.received(form="td", length=2 * (288 + 4096) * 2)
g1 = S.ofdm_demodulate(w1)
h1 = S.estimate(g1, ref)
l1 = S.equalise_demap(g1, h1, qm=2)

hard = S.decode(l1, scheme="ldpc", max_iter=8)

S.expect(crc="crc24a", hard_eq="tx.codeword", rv_seq=(0, 2))
S.budget(slot_ms=1.0, on_time=None)
