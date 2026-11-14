"""G2 场景 2：NR 下行共享信道（PDSCH），与 run-pdsch-loop 同口径。

写法遵守 docs/20：只出现协议量与 §2 的动词；带宽决定 FFT 点数，码块大小、
提升长度、资源数都由 cfg 或动词参数推导/给定，这里没有任何芯片词汇。
"""
from proto.hll import scenario

S = scenario("nr_pdsch", rat="nr", direction="dl")

# 20 MHz / 15 kHz 与现有接收链同口径；n_id=1 是 38.211 7.4.1.1 的 DMRS 种子，
# RNTI 0x1234 与 tools/nr_dl_mod.py 的默认激励一致。
S.cfg(oversample=2, bw_mhz=20, scs_khz=15, n_id=1, n_rnti=0x1234,
      a_bits=80, e_bits=720, qm=2, layers=1, nsym=2)

# 阶段 1 的负载：每格 1 个 TB（L1 轻档），跑 40 格。
# 按时门槛暂不设（on_time=None）：原型 100 MHz 上一次整链就远超 1 ms 预算，
# 负载模型先把差距量出来，阶段 2 再谈达标。
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

tb = S.transport_block(bits=80, rnti=0x1234)
cw = S.encode(tb, scheme="ldpc", bits=1040)
sym = S.modulate(cw, qm=2, scrambler="gold")
ref = S.dmrs(type="type1", n_id=1, l=2)
grid = S.map_to_grid(sym, ref)
wave = S.ofdm_modulate(grid)

y = S.channel(wave, model="awgn", snr_db=20)
h = S.estimate(y, ref)
llr = S.equalise_demap(y, h, qm=2)
hard = S.decode(llr, scheme="ldpc", max_iter=8)

S.expect(tb_eq="tx.tb", crc="crc24a", hard_eq="tx.codeword", llr_sign="all",
         dmrs_c_init="38.211 7.4.1.1")
S.budget(slot_ms=1.0, on_time=None)
