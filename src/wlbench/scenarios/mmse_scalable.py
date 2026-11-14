"""可参数化 MIMO 规模的工作负载（NR PUSCH 风格的 MMSE 硬判）。

layers / rx 由 `wlbench sweep --set layers=... --set rx=...` 注入；
参考模型按同一组参数生成激励与黄金值，不依赖任何既有 2x2 场景。
"""
from wlbench.hll import scenario

S = scenario("mmse_scalable", rat="nr", direction="dl")

S.cfg(bw_mhz=20, scs_khz=15, layers=2, rx=2, qm=2, a_bits=720,
      snr_db=25, seed=1)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

wave = S.received(form="td", length=2 * 2 * (288 + 4096))
ref = S.dmrs(type="mimo_csi", n_id=1, l=0)
y = S.ofdm_demodulate(wave)
h = S.estimate(y, ref)
hard = S.mimo_equalise(y, h, qm=2, layers=2, rx=2, bits=720)

S.expect(hard_bits="model.mmse", coeff="model.h", data_re=180)
S.budget(slot_ms=1.0, on_time=None)
