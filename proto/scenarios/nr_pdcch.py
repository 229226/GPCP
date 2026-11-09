"""G2 场景 1：NR 下行控制信道（PDCCH / DCI），与 run-pdcch-loop 同口径。

写法遵守 docs/20：只出现协议量与 docs/20 §2 的动词；带宽、子载波间隔、FFT 点数、
码块大小等一律由 cfg 推导或被编译器决定。这里没有任何芯片词汇。
"""
from proto.hll import scenario

S = scenario("nr_pdcch", rat="nr", direction="dl")

# 20 MHz / 15 kHz 是现有接收链的口径；n_id=200、RNTI=0x1234 与
# tools/nr_pdcch.py 的默认激励一致（E=432 → 216 个 QPSK 符号）。
S.cfg(bw_mhz=20, scs_khz=15, n_id=200, n_rnti=0x1234,
      aggregation_level=4, reg_per_cce=6, e_bits=432, a_bits=32)
S.traffic(arrival="single", util=1.0)

dci = S.transport_block(bits=32, rnti=0x1234)
cw = S.encode(dci, scheme="polar", bits=512)
sym = S.modulate(cw, qm=2, scrambler="gold")
ref = S.dmrs(type="pdcch", n_id=200, l=0)
grid = S.map_to_grid(sym, ref)
wave = S.ofdm_modulate(grid)

y = S.channel(wave, model="awgn", snr_db=12)
h = S.estimate(y, ref)
llr = S.equalise_demap(y, h, qm=2)
hard = S.decode(llr, scheme="polar", max_iter=1)

S.expect(hard_eq="tx.codeword", dci_bits=32, crc="crc24c",
         rnti="blind_detect", llr_sign="all")
S.budget(slot_ms=1.0, on_time=1.0)
