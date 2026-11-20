/* -----------------------------------------------------------------------------
 * run-prach: PRACH Msg1 detection on the CGRA (P2).
 *
 * 139 cyclic shifts of the ZC root (L_RA=139, 127 taps) are correlated against
 * the received preamble, one array run per shift; the peak decodes to the
 * preamble index and the timing advance.
 *
 *   1. received chips -> spad (one DMA, stays resident for all shifts)
 *   2. for d in 0..138: reference taps d -> spad, one prach_corr run,
 *      16 per-PE partials -> RAM
 *   3. CPU re-sums the 16 partials (cross-check) -> |C[d]|^2 -> peak
 *
 * Result area (0x8007_0000):
 *   [0] marker [1] runs [2] DMA [3] errors [4] 0x600D
 *   [5] delay  [6] preamble  [7] TA  [8] peak re  [9] peak im
 *   [10] 2nd delay [11] 2nd re [12] 2nd im
 *   [13] N_CS [14] L_RA [15] TAPS [16] partials checked [17] program cycles
 *   [18] runs == L_RA
 *   [20 + 2d] / [21 + 2d] correlation d (re, im)
 *
 * 阶段实现（received / dmrs / correlate）在 zc_corr.inc，与 PUCCH 和协议
 * 运行器共用同一份代码（docs/protocols/README.md 规则 4）。
 * ----------------------------------------------------------------------------- */
#include "../../include/gpcp_cgra.h"
#include "../../include/gpcp_cgra_rt.h"
#include "prach_corr_prog.h"   /* 编译器生成（prach_corr.yaml） */
#include "prach_corr_layout.h"

#define RESULT      ((uint32_t *)0x80070000ul)
#define N_CS        15u        /* zero-correlation zone (SIB1 zczc 0) */

#include "zc_corr.inc"

int main(void)
{
    uint32_t *out = RESULT;

    if (zc_setup(0) != 0) {
        out[0] = 0x0BADC0DEu;
        *(volatile uint32_t *)ZC_EXIT_ADDR = 0x600Du;
        return 0;
    }

    zc_received();               /* 0. 装微码 + 接收序列常驻 */
    zc_dmrs();                   /* 1. 参考抽头随激励进来 */
    zc_search(ZC_L_RA, 0, 1);    /* 2. 139 个移位 + 归约 + 峰值 */

    for (uint32_t d = 0; d < ZC_L_RA; d++) {
        out[20 + 2 * d] = (uint32_t)zc_re[d];
        out[21 + 2 * d] = (uint32_t)zc_im[d];
    }

    out[0] = 0xCAFEBABEu;
    out[1] = runs;
    out[2] = dmas;
    out[3] = errs;
    out[4] = 0x600Du;
    out[5] = zc_best_d;
    out[6] = zc_best_d / N_CS;
    out[7] = zc_best_d % N_CS;
    out[8] = (uint32_t)zc_re[zc_best_d];
    out[9] = (uint32_t)zc_im[zc_best_d];
    out[10] = zc_second_d;
    out[11] = (uint32_t)zc_re[zc_second_d];
    out[12] = (uint32_t)zc_im[zc_second_d];
    out[13] = N_CS;
    out[14] = ZC_L_RA;
    out[15] = ZC_TAPS;
    out[16] = nchk;
    out[17] = PRACH_CORR_PROG_CYCLES;
    out[18] = runs == ZC_L_RA ? 1u : 0u;
    out[19] = 0u;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)ZC_EXIT_ADDR = 0x600Du;
    return 0;
}
