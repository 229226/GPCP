"""G2 场景 3：CSI-RS + 宽带 CQI（与 run-csi 同口径）。

纯接收场景：激励是一段时域样本（tools/nr_csi.py dump 的格式），
本程序只做 FFT -> 80 个 CSI-RS RE 的 LS -> 差值噪声 -> SNR -> CQI。
"""
from wlbench.hll import scenario

S = scenario("nr_csi", rat="nr", direction="dl")

S.cfg(oversample=2, bw_mhz=20, scs_khz=15, n_id=1, csi_density=3, n_csi_rs=80,
      snr_db=20, seed=1)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

wave = S.received(form="td", length=S.sym_len(1))
ref = S.dmrs(type="csi_rs", n_id=1, l=0, density=3)
grid = S.ofdm_demodulate(wave)
report = S.measure(grid, ref, report="cqi")

S.expect(cqi="model.cqi", snr_db="model.snr_db", ls_sign="all",
         ps_pn_nonzero=True)
S.budget(slot_ms=1.0, on_time=None)
