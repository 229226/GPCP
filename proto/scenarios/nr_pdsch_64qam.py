"""NR PDSCH 64QAM 变体（Qm=6，Rel-19 条文口径）。纯接收，口径同 16QAM。"""
from proto.hll import scenario

S = scenario("nr_pdsch_64qam", rat="nr", direction="dl")

S.cfg(oversample=2, bw_mhz=20, scs_khz=15, n_id=1, n_rnti=0x1234,
      a_bits=80, e_bits=720, qm=6, layers=1, nsym=2)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

wave = S.received(form="td", length=S.sym_len(2, 7))
ref = S.dmrs(type="type1", n_id=1, l=2)
h = S.estimate(wave, ref)
llr = S.equalise_demap(wave, h, qm=6)
hard = S.decode(llr, scheme="ldpc", max_iter=8)

S.expect(tb_eq="tx.tb", crc="crc24a", hard_eq="tx.codeword", llr_sign="all")
S.budget(slot_ms=1.0, on_time=None)
