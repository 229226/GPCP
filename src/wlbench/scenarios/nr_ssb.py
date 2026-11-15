"""G2 场景 8：SSB 接收（PSS/SSS 采集 + PBCH 译码），与 run-ssb-fid 同口径。"""
from wlbench.hll import scenario

S = scenario("nr_ssb", rat="nr", direction="dl")

S.cfg(oversample=2, bw_mhz=20, scs_khz=15, n_id=1, n_id1=200, n_id2=1, i_ssb=3, n_hf=0,
      a_bits=32, nsym=4, snr_db=None)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

wave = S.received(form="td4", length=S.sym_len(4))
grid = S.ofdm_demodulate(wave)

pss = S.dmrs(type="pss", n_id=200, l=0)
corr_pss = S.correlate(grid, pss, hypotheses=3, seq="pss")
sss = S.dmrs(type="sss", n_id=200, l=0)
corr_sss = S.correlate(grid, sss, hypotheses=336, seq="sss")

ref = S.dmrs(type="pbch", n_id=1, l=0)
h = S.estimate(grid, ref)
eq = S.equalise(grid, h, qm=2)
llr = S.demap(eq, qm=2)
mib = S.decode(llr, scheme="polar", max_iter=1)

S.expect(n_id1=200, n_id2=1, mib="tx.mib", dmrs_c_init="38.211 7.4.1.3",
         i_ssb=3)
S.budget(slot_ms=1.0, on_time=None)
