/* -----------------------------------------------------------------------------
 * P1 real-mapping SSB receiver (4 GSCN symbols -> N_ID + MIB).
 *
 *   four symbols on RAM: [CP 288 | 4096] each
 *     -> 4096-point FFT per symbol (CGRA, ssb_fft.inc)
 *     -> PSS search over 3 N_ID2 hypotheses (pss_corr microcode)
 *     -> SSS search over 336 N_ID1 hypotheses (same microcode)
 *     -> PBCH DMRS channel estimate (38.211 7.4.1.3, c_init with i_SSB/n_hf)
 *     -> QPSK demod + PBCH descrambling + inverse rate matching (720 -> 512)
 *     -> polar SC decode on the CGRA -> 32 MIB bits
 *
 * Result area (0x8001_0000, 32-bit words):
 *   [0] 0x600D  [1] errors  [2] cycles  [3] array runs  [4] N_ID2  [5] true
 *   [6] N_ID1   [7] true    [8] i_SSB   [9] n_hf  [10] DMRS c_init
 *   [11] hre[sym1] [12] him[sym1] [13] hre[sym3] [14] him[sym3]
 *   [15..46] decoded MIB bits   [47..50] debug counters
 *   [942..945] SSS best/true (>>10)   [954..985] MIB again
 *
 * 阶段实现（received .. decode）在 ssb_stages.inc，与协议运行器
 * sw/soc/proto_runner.c 共用同一份代码（docs/20 §8 规则 4）。
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "pss_corr_prog.h"      /* 编译器生成的 PSS/SSS 复数相关 */
#include "pss_corr_layout.h"
#include "polar_node_prog.h"    /* 编译器生成的 polar SC 节点微码 */
#include "ssb_fid_luts.h"

#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#include "ssb_stages.inc"

int main(void)
{
    uint32_t t0;

    if (ss_setup(0) != 0) {
        RESULT_BASE[0] = 0x0BADC0DEu;
        *(volatile uint32_t *)EXIT_ADDR = 0;
        return 0;
    }

    t0 = mtime();
    ss_received();            /* 0. 四个符号的时域样本已由宿主放进 RAM */
    ss_ofdm_demodulate();     /* 1. 4 x 4096 FFT [array] */
    ss_correlate_pss();       /* 2. PSS：3 个 N_ID2 假设 [array] */
    ss_correlate_sss();       /* 3. SSS：336 个 N_ID1 假设 [array] */
    ss_dmrs();                /* 4. PBCH DMRS 种子（n_ID, i_SSB, n_hf） */
    ss_estimate();            /* 5. 网格抽取 + 符号 1/3 的 LS */
    ss_equalise_demap();      /* 6. QPSK + 解扰 + 逆速率匹配 720 -> 512 */
    ss_decode();              /* 7. polar SC [array] -> 32 bit MIB */

    RESULT_BASE[942] = ss_nid1;
    RESULT_BASE[943] = TRUE_NID1;
    RESULT_BASE[944] = ss_sbest10;
    RESULT_BASE[945] = ss_strue10;

    RESULT_BASE[0] = 0x600D;
    RESULT_BASE[1] = errs;
    RESULT_BASE[2] = mtime() - t0;
    RESULT_BASE[3] = runs;
    RESULT_BASE[4] = ss_nid2;
    RESULT_BASE[5] = TRUE_NID2;
    RESULT_BASE[6] = ss_nid1;
    RESULT_BASE[7] = TRUE_NID1;
    RESULT_BASE[8] = I_SSB;
    RESULT_BASE[9] = N_HF;
    RESULT_BASE[10] = ss_dmrs_ci;
    RESULT_BASE[11] = (uint32_t)ss_hre[1];
    RESULT_BASE[12] = (uint32_t)ss_him[1];
    RESULT_BASE[13] = (uint32_t)ss_hre[3];
    RESULT_BASE[14] = (uint32_t)ss_him[3];
    for (uint32_t k = 0; k < KINFO; k++)
        RESULT_BASE[15 + k] = ss_mib[k];
    RESULT_BASE[47] = dbg_corr_spin;
    RESULT_BASE[48] = dbg_array_spin;
    RESULT_BASE[49] = dbg_wait;
    RESULT_BASE[50] = nblk;

    RESULT_BASE[11] = 0x600D;      /* epilogue marker (same as the old finish()) */
    *(volatile uint32_t *)EXIT_ADDR = 0;
    while (1) { }
}
