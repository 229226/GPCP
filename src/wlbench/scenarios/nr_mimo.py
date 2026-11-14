"""G2 场景 6：2x2 MIMO PDSCH 接收（MMSE），与 run-mimo 同口径。"""
from wlbench.hll import scenario

S = scenario("nr_mimo", rat="nr", direction="dl")

S.cfg(oversample=2, bw_mhz=20, scs_khz=15, n_id=1, layers=2, rx=2, qm=2, a_bits=720,
      snr_db=25, seed=1)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

wave = S.received(form="td2", length=S.sym_len(2))
ref = S.dmrs(type="mimo_csi", n_id=1, l=0)
y = S.ofdm_demodulate(wave)
h = S.estimate(y, ref)
hard = S.mimo_equalise(y, h, qm=2, layers=2, rx=2, bits=720)

S.expect(hard_bits="model.mmse", coeff="model.h", data_re=180)
S.budget(slot_ms=1.0, on_time=None)
