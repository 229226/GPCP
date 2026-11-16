/* -----------------------------------------------------------------------------
 * G2 / B5: PDSCH self-loopback in ONE SoC program (docs/project/README.md G2 验收的后半).
 *
 *   params (0x8060_0000, nr_dl_mod.dump_td: magic, nvar, a_tb, tb_words, TB[3],
 *           pad, variants{qm, rv, rnti})
 *     -> B2 transmit chain: TB -> CRC24A -> LDPC BG2/Zc20 -> rate match (rv=0)
 *        -> scramble (c_init = n_RNTI<<15) -> bit interleave (Qm=2) -> QPSK
 *     -> 38.211 7.4.1.1 Gold DMRS (c_init = 0x120002 / 0x180002)
 *     -> 2 OFDM symbols: 240 REs each, DMRS every 4th RE (60 pilots/180 data)
 *        -> conjugate spectrum -> CGRA IFFT (fft4096) + CP -> TD at 0x8040_0000
 *     -> A4/B4 receive chain: FFT -> RE -> per-symbol LS -> equalise -> 720 LLR
 *        -> de-interleave -> descramble -> rate de-match -> 8 min-sum LDPC
 *        iterations on the CGRA -> CRC24A -> 80-bit TB
 *
 * Nothing is loaded from the host except the stimulus (TB + rv + RNTI): the
 * waveform, the DMRS and the whole receive path are produced on this SoC.
 *
 * Scale chain (same numbers as run-pdcch-loop / run-pdcch):
 *   grid amplitude 2000 -> TD = conj(IFFT_hw) * 256 -> RX FFT gives RE/16,
 *   RE << 4 -> |LLR| ~ 2000; LS over 60 pilots gives h = 16 = H_SCALE, so the
 *   equalised LLR is ~32000 and the >>8 scale puts it at ~125 (well under
 *   SENTINEL=32767).
 *
 * Result area (0x8007_0000; .bss 越过 0x8001_0000，按 run-pusch 的先例上移):
 *   [0] marker 0xCAFEBD51  [1] errors  [2] cycles  [3] array runs  [4] DMAs
 *   [5] CRC24A ok  [6] 0x600D  [7] TB bits decoded (80/80)
 *   [8..1047] 1040 LDPC hard decisions (0/1, block-major)
 *   [1048..1051] channel estimate hre/him per symbol
 *   [1052..1059] first 8 raw LLRs       [1060..1067] first 8 descrambled LLRs
 *   [1081] DMRS c_init sym0  [1082] DMRS c_init sym1  [1083] rv transmitted
 *   [1084] Qm  [1085] QAM amplitude  [1086] E/Qm  [1087] TB mismatch count
 *   [1090..1129] transmitted TB bits   [1130..1169] decoded TB bits
 *   [1170] max |symbol|  [1171] constellation REs placed  [1172] zeros check
 *   [1173] LLR sign mismatches  [1174] first bad index  [1175] its value
 *
 * 阶段实现（transport_block .. decode）在 pdsch_stages.inc，与协议运行器
 * sw/soc/bench/proto_runner.c 共用同一份代码（docs/protocols/README.md 规则 4）。
 * ----------------------------------------------------------------------------- */
#include "../../include/gpcp_cgra.h"
#include "../../include/soc_io.h"
#include "../../include/gpcp_cgra_rt.h"
#include "../../include/ldpc_bg2.h"
#include "../../include/ldpc_enc_tables.h"
#include "ldpc_enc.inc"
#include "pdsch_cn_prog.h"
#include "pdsch_bn_prog.h"

#include "pdsch_stages.inc"

int main(void)
{
    uint32_t *out = RESULT_BASE;
    const uint32_t *par = (const uint32_t *)PARAM_BASE;
    uint32_t t0, i, b, cyc;

    if (qs_setup(par) != 0) {
        out[0] = (par[0] == TD_MAGIC) ? 0x0BADBD52u : 0x0BADBD51u;
        out[6] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        mmio_w(EXIT_ADDR, 0x600Du);
        return 0;
    }

    t0 = mtime();
    qs_transport_block();      /* 0. TB + CRC24A + filler */
    qs_encode();               /* 1. LDPC 编码（标量）+ syndrome */
    qs_modulate();             /* 2. 速率匹配 + 加扰 + 交织 + QPSK */
    qs_dmrs();                 /* 3. Gold DMRS（2 个符号） */
    qs_map_to_grid();          /* 4. 240 RE/符号（每 4 个一个导频） */
    qs_ofdm_modulate();        /* 5. IFFT [array] + CP -> 2 符号 TD */
    qs_channel();              /* 6. 信道（自环 = 理想） */
    qs_transform();            /* 7a. 接收 FFT [array]（节点拆分后独立成阶段） */
    qs_estimate();             /* 7b. LS 信道估计 */
    qs_equalise_demap();       /* 8. 均衡 + LLR + 符号锚点 + 解扰 + 解速率匹配 */
    qs_decode();               /* 9. 8 次 LDPC 迭代 [array] + CRC24A */
    cyc = mtime() - t0;

    for (b = 0; b < BG2_COLS; b++)
        for (i = 0; i < ZC; i++)
            out[8 + b * ZC + i] = (qs_hard[b][i] < 0) ? 1u : 0u;
    for (i = 0; i < A_TB; i++) {
        out[1090 + i] = tb_bits[i];
        out[1180 + i] = qs_rx_bits[i];
    }
    out[5] = qs_crc_ok;
    out[7] = A_TB - qs_tb_bad;

    out[0] = 0xCAFEBD51u;
    out[1] = errs;
    out[2] = cyc;
    out[3] = runs;
    out[4] = dmas;
    out[6] = 0x600Du;
    out[1048] = (uint32_t)qs_hre[0];
    out[1049] = (uint32_t)qs_him[0];
    out[1050] = (uint32_t)qs_hre[1];
    out[1051] = (uint32_t)qs_him[1];
    for (i = 0; i < 8; i++)
        out[1052 + i] = (uint32_t)llr_e[i];
    for (i = 0; i < 8; i++)
        out[1060 + i] = (uint32_t)llr_i[i];
    out[1081] = dmrs_cinit(0);
    out[1082] = dmrs_cinit(1);
    out[1083] = qs_rv;
    out[1084] = qs_qm;
    out[1085] = QPSK;
    out[1086] = qs_max_sym;
    out[1087] = qs_tb_bad;
    out[1170] = qs_sym_max;
    out[1171] = 2 * qs_max_sym;
    out[1172] = qs_syn;
    out[1173] = qs_llr_bad;
    out[1174] = qs_llr_first;
    out[1175] = qs_llr_val;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    mmio_w(EXIT_ADDR, 0x600Du);
    return 0;
}
