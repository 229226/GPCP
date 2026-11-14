"""IEEE 802.11be EHT SU 20 MHz（MCS 0）接收场景。

帧：HE-STF + HE-LTF×2 + EHT-SIG + DATA×3；256 点 FFT、CP=16、234 数据子载波。
EHT-SIG 用 BPSK 1/2 + K=7 BCC；DATA 用 BPSK 1/2 + 802.11 N648/Z=27 LDPC。
"""
from wlbench.hll import scenario

S = scenario("wifi_11be", rat="wifi", direction="dl")

S.cfg(bw_mhz=20, fft_points=256, cp_len=16, n_data_subcarriers=234,
      a_bits=324, e_bits=648, qm=2, nsym=7, mcs=0)
S.traffic(arrival="periodic", jobs_per_slot=1, slots=40, util=1.0)

wave = S.received(form="td", length=7 * 2 * (256 + 16))
ref = S.dmrs(type="wifi_ltf", n_id=0, l=0)
grid = S.ofdm_demodulate(wave)
h = S.estimate(grid, ref)
llr = S.equalise_demap(grid, h, qm=2)
hard = S.decode(llr, scheme="ldpc_wifi", max_iter=10)

S.expect(signal_bits="tx.sig", data_bits="tx.data", ldpc="N648_Z27_R12")
S.budget(slot_ms=1.0, on_time=None)
