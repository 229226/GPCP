"""G2 场景 7：PUSCH 接收链（上行，样本进 → TB 出），与 run-pusch 同口径。"""
from wlbench.hll import scenario

S = scenario("nr_pusch", rat="nr", direction="ul")

S.cfg(oversample=2, bw_mhz=20, scs_khz=15, n_id=0x1234, n_rnti=0x1234, a_bits=80, e_bits=720,
      qm=2, rv=0, nsym=2, snr_db=20, seed=1)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

wave = S.received(form="td", length=S.sym_len(2, 1))
ref = S.dmrs(type="pusch", n_id=0x1234, l=0)
grid = S.ofdm_demodulate(wave)
h = S.estimate(grid, ref)
eq = S.equalise(grid, h, qm=2)
llr = S.demap(eq, qm=2)
hard = S.decode(llr, scheme="ldpc", max_iter=8)

S.expect(tb_eq="tx.tb", crc="crc24a", hard_eq="tx.codeword", k0=0)
S.budget(slot_ms=1.0, on_time=None)
