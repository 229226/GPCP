"""G2 场景 5：PUCCH 格式 0（1 bit SR/HARQ-ACK），与 run-pucch 同口径。"""
from wlbench.hll import scenario

S = scenario("nr_pucch", rat="nr", direction="ul")

S.cfg(bw_mhz=20, scs_khz=15, n_id=1, l_ra=139, taps=127, m_cs=2,
      ack=1, snr_db=6, seed=1)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

chips = S.received(form="zc", length=2 * 139)
ref = S.dmrs(type="pucch_zc", n_id=1, l=0)
corr = S.correlate(chips, ref, hypotheses=2, seq="zc")

S.expect(ack="tx.bit", winner="model.cand", energies="model.exact")
S.budget(slot_ms=1.0, on_time=None)
