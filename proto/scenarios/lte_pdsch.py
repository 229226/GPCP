"""G2 场景 9（阶段 3 跨制式）：LTE PDSCH（36.212 Turbo），只写 HLL。

实现库：sw/soc/lte_stages.inc（Turbo 编/译码、36.212 速率匹配、36.211 CRS）。
口径与 proto/models/lte_pdsch.py 一致（K=104、2 符号、CRS 每 6 RE）。
"""
from proto.hll import scenario

S = scenario("lte_pdsch", rat="lte", direction="dl")

S.cfg(bw_mhz=20, scs_khz=15, n_id=1, n_rnti=0x1234,
      a_bits=80, e_bits=800, qm=2, nsym=2)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

tb = S.transport_block(bits=80, rnti=0x1234)
info = S.encode(tb, scheme="turbo", bits=104)
sym = S.modulate(info, qm=2, scrambler="gold")
ref = S.dmrs(type="crs", n_id=1, l=0)
grid = S.map_to_grid(sym, ref)
wave = S.ofdm_modulate(grid)

y = S.channel(wave, model="awgn", snr_db=20)
g = S.ofdm_demodulate(y)
h = S.estimate(g, ref)
llr = S.equalise_demap(g, h, qm=2)
hard = S.decode(llr, scheme="turbo", max_iter=8)

S.expect(tb_eq="tx.tb", crc="crc24a", hard_eq="tx.info")
S.budget(slot_ms=1.0, on_time=None)
