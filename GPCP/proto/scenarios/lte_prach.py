"""LTE PRACH（format 4，139 长 ZC），Rel-18 条文口径。

复用 NR PRACH 的 128 抽头 ZC 相关实现（zc_corr.inc）；本场景只改前导/TA。
"""
from proto.hll import scenario

S = scenario("lte_prach", rat="lte", direction="ul")

S.cfg(bw_mhz=20, scs_khz=15, n_id=1, l_ra=139, taps=127, n_cs=15,
      preamble=4, timing_advance=5, snr_db=6, seed=1)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

chips = S.received(form="zc", length=2 * 139)
ref = S.dmrs(type="prach_root", n_id=1, l=0)
corr = S.correlate(chips, ref, hypotheses=139, seq="zc")

S.expect(preamble="tx.preamble", timing_advance="tx.ta",
         correlations="model.exact")
S.budget(slot_ms=1.0, on_time=None)
