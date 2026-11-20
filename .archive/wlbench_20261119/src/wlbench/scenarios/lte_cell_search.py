"""LTE 小区搜索（PSS + SSS），Rel-18 条文口径。"""
from wlbench.hll import scenario

S = scenario("lte_cell_search", rat="lte", direction="dl")

S.cfg(bw_mhz=20, scs_khz=15, n_id_1=100, n_id_2=1, snr_db=10, seed=1)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

pss = S.received(form="pss", length=62)
ref_pss = S.dmrs(type="lte_pss", n_id=301, l=0)
c_pss = S.correlate(pss, ref_pss, hypotheses=3, seq="lte_pss")

sss = S.received(form="sss", length=62)
ref_sss = S.dmrs(type="lte_sss", n_id=301, l=0)
c_sss = S.correlate(sss, ref_sss, hypotheses=504, seq="lte_sss")

S.expect(n_id_2="rx.n_id_2", n_id_1="rx.n_id_1", n_id="rx.n_id")
S.budget(slot_ms=1.0, on_time=None)
