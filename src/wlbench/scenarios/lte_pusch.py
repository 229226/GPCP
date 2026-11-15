"""LTE PUSCH（上行 Turbo + SC-FDMA），Rel-18 条文口径。

与 lte_pdsch 同构，但使用上行 Gold 导频与 200 点 DFT/IDFT（DFT-s-OFDM）。
"""
from wlbench.hll import scenario

S = scenario("lte_pusch", rat="lte", direction="ul")

S.cfg(oversample=2, bw_mhz=20, scs_khz=15, n_id=1, n_rnti=0x1234,
      a_bits=80, e_bits=800, qm=2, nsym=2, tp=1)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

tb = S.transport_block(bits=80, rnti=0x1234)
info = S.encode(tb, scheme="turbo", bits=104)
sym = S.modulate(info, qm=2, scrambler="gold")
dft = S.transform(sym, form="dft", size=200, count=2)
ref = S.dmrs(type="pusch", n_id=1, l=0)
grid = S.map_to_grid(dft, ref)
wave = S.ofdm_modulate(grid)

y = S.channel(wave, model="awgn", snr_db=20)
g = S.ofdm_demodulate(y)
h = S.estimate(g, ref)
eq = S.equalise(g, h, qm=2)
soft = S.transform(eq, form="idft", size=200, inverse=True, count=2)
llr = S.demap(soft, qm=2)
hard = S.decode(llr, scheme="turbo", max_iter=8)

S.expect(tb_eq="tx.tb", crc="crc24a", hard_eq="tx.info", tp=1)
S.budget(slot_ms=1.0, on_time=None)
