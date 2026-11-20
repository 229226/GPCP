"""G2 场景 4：PRACH Msg1（ZC 循环移位相关搜索），与 run-prach 同口径。"""
from wlbench.hll import scenario

S = scenario("nr_prach", rat="nr", direction="ul")

S.cfg(bw_mhz=20, scs_khz=15, n_id=1, l_ra=139, taps=127, n_cs=15,
      preamble=2, timing_advance=9, snr_db=6, seed=1)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

chips = S.received(form="zc", length=2 * 139)
ref = S.dmrs(type="prach_root", n_id=1, l=0)
corr = S.correlate(chips, ref, hypotheses=139, seq="zc")

S.expect(preamble="tx.preamble", timing_advance="tx.ta",
         correlations="model.exact", peak_margin_db=16.6)
S.budget(slot_ms=1.0, on_time=None)
