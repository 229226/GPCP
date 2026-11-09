/* -----------------------------------------------------------------------------
 * run-pusch: PUSCH receive chain (uplink, samples in -> TB out).
 *
 *   TD samples (2 symbols) -> 4096 FFT -> 240 RE -> per-symbol LS against the
 *   38.211 6.4.1.1 Gold DMRS -> equalise -> 720 QPSK LLR -> de-interleave
 *   -> descramble -> rate de-match -> 8 min-sum LDPC iterations on the CGRA
 *   -> CRC24A -> 80-bit TB.
 *
 *   -DTP      : DFT-s-OFDM (360-point IDFT transform deprecoding)
 *   -DHARQ_TX : two transmissions, Chase combining
 *
 * Result area (0x8007_0000):
 *   [0] marker [1] errors [2] cycles [3] array runs [4] DMA
 *   [5] CRC24A ok [6] 0x600D [7] transform-precoding build flag
 *   [8..1047] 1040 hard decisions
 *   [1048..1051] channel estimate per symbol  [1052..1059] first raw LLRs
 *   [1060..1067] first descrambled LLRs  [1068..1075] first de-matched LLRs
 *   [1076] CRC ok  [1077..1080] FFT gain probes  [1081/1082] DMRS c_init
 *   [1083] HARQ transmissions
 *
 * 阶段实现（received .. decode）在 pusch_stages.inc，与协议运行器
 * sw/soc/proto_runner.c 共用同一份代码（docs/20 §8 规则 4）。
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"
#include "../include/ldpc_bg2.h"
#include "pdsch_cn_prog.h"
#include "pdsch_bn_prog.h"

#include "pusch_idft_tab.h"     /* -DTP 时的 360 点 IDFT 表 */

#include "pusch_stages.inc"

int main(void)
{
    uint32_t *out = RESULT_BASE;
    uint32_t t0;

    if (us_setup(0) != 0) {
        out[0] = 0x0BADC0DEu;
        out[6] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 0;
    }

    t0 = mtime();
    us_received();          /* 0. 时域样本已由宿主放进 0x8040_0000 */
    us_dmrs();              /* 1. 38.211 6.4.1.1 Gold DMRS */
    us_ofdm_demodulate();   /* 2. 每个符号一次 FFT [array] + RE 抽取 */
    us_estimate();          /* 3. 每符号 60 导频的 LS */
    us_equalise_demap();    /* 4. 均衡 + LLR + 解交织 + 解扰 + Chase + 解速率匹配 */
    us_decode();            /* 5. 8 次 LDPC 迭代 [array] + CRC24A */

    out[0] = 0xCAFEBABEu;
    out[1] = errs;
    out[2] = mtime() - t0;
    out[3] = runs;
    out[4] = dmas;
    out[5] = us_crc_ok;
    out[6] = 0x600Du;
#ifdef TP
    out[7] = 1u;      /* DFT-s-OFDM (transform precoding) build */
#else
    out[7] = 0u;      /* CP-OFDM build */
#endif
    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t i = 0; i < ZC; i++)
            out[8 + b * ZC + i] = (us_hard[b][i] < 0) ? 1u : 0u;

    out[1048] = (uint32_t)us_hre[0][0];
    out[1049] = (uint32_t)us_him[0][0];
    out[1050] = (uint32_t)us_hre[0][1];
    out[1051] = (uint32_t)us_him[0][1];
    for (int i = 0; i < 8; i++) out[1052 + i] = (uint32_t)us_raw0[i];
    for (int i = 0; i < 8; i++) out[1060 + i] = (uint32_t)us_descr0[i];
    for (int i = 0; i < 8; i++) out[1068 + i] = (uint32_t)(dem[PUNCT + i] >> LLR_SHIFT);
    out[1076] = us_crc_ok;
    out[1077] = (uint32_t)us_dbg_spec_re;
    out[1078] = (uint32_t)us_dbg_spec_im;
    out[1079] = (uint32_t)us_dbg_yw_re;
    out[1080] = (uint32_t)us_dbg_yw_im;
    out[1081] = dmrs_cinit(0);
    out[1082] = dmrs_cinit(1);
    out[1083] = (uint32_t)HARQ_TX;
    out[1087] = us_k0;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
