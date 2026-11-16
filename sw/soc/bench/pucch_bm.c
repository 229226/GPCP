/* -----------------------------------------------------------------------------
 * run-pucch: PUCCH format 0 (1-bit SR / HARQ-ACK) detection on the CGRA (P3)
 *
 * tools/models/nr_ul.py models PUCCH format 0 as one of two cyclic shifts of a ZC
 * root (m_CS = 0 -> ACK/SR present, m_CS = 6 -> NACK/absent).  The gNB decides
 * by comparing the energies of the two correlations.  No new microcode: this
 * reuses the *verified* prach_corr kernel for NCAND = 2 candidates.
 *
 * Stimulus at 0x8040_0000 (build/soc/pucch.td, tools/models/nr_pucch.py dump):
 *     word 0 .. 277            : 139 received (re, im) int32 pairs
 *     word 278 + 254*c ..      : candidate c's 127 reference taps (c = 0, 1)
 *
 * Results at 0x8007_0000 (32-bit words):
 *   [0] marker [1] runs [2] DMA [3] errors [4] 0x600D
 *   [5] detected bit (1 = ACK/SR, 0 = NACK)
 *   [6] winning candidate [7] winning m_CS [8] NCAND [9] TAPS
 *   [10] peak re [11] peak im [12] peak power lo [13] peak power hi
 *   [14] 2nd re  [15] 2nd im  [16] 2nd power lo  [17] 2nd power hi
 *   [18] partials cross-checked [19] program cycles
 *   [20 + 4c] C[c].re, C[c].im, |C[c]|^2 lo, |C[c]|^2 hi
 *   [48 + 32c + 2p] candidate c, PE p partial (re, im)
 *
 * 阶段实现（received / dmrs / correlate）在 zc_corr.inc，与 PRACH 和协议
 * 运行器共用同一份代码（docs/protocols/README.md 规则 4）。
 * ----------------------------------------------------------------------------- */
#include "../../include/gpcp_cgra.h"
#include "../../include/gpcp_cgra_rt.h"
#include "prach_corr_prog.h"   /* 编译器生成（prach_corr.yaml） */
#include "prach_corr_layout.h"

#define RESULT      ((uint32_t *)0x80070000ul)
#define NCAND       2u
#define CS_ACK      0u
#define CS_NACK     6u

#include "zc_corr.inc"

int main(void)
{
    uint32_t *out = RESULT;
    static const uint32_t mcs[NCAND] = { CS_ACK, CS_NACK };

    if (zc_setup(0) != 0) {
        out[0] = 0x0BADC0DEu;
        *(volatile uint32_t *)ZC_EXIT_ADDR = 0x600Du;
        return 0;
    }

    zc_received();                       /* 0. 装微码 + 接收序列常驻 */
    zc_dmrs();                           /* 1. 两个候选抽头随激励进来 */
    zc_search(NCAND, out + 48, 0);       /* 2. 两个 m_CS + 64 个分部导出 */

    {
        uint32_t win = zc_win;
        uint32_t los = 1u - win;
        uint32_t bit = (win == 0u) ? 1u : 0u;
        out[0] = 0xCAFEBABEu;
        out[1] = runs;
        out[2] = dmas;
        out[3] = errs;
        out[4] = 0x600Du;
        out[5] = bit;
        out[6] = win;
        out[7] = mcs[win];
        out[8] = NCAND;
        out[9] = ZC_TAPS;
        out[10] = (uint32_t)zc_re[win];
        out[11] = (uint32_t)zc_im[win];
        out[12] = (uint32_t)zc_pw[win];
        out[13] = (uint32_t)((uint64_t)zc_pw[win] >> 32);
        out[14] = (uint32_t)zc_re[los];
        out[15] = (uint32_t)zc_im[los];
        out[16] = (uint32_t)zc_pw[los];
        out[17] = (uint32_t)((uint64_t)zc_pw[los] >> 32);
        out[18] = nchk;
        out[19] = PRACH_CORR_PROG_CYCLES;
        for (uint32_t c = 0; c < NCAND; c++) {
            out[20 + 4 * c] = (uint32_t)zc_re[c];
            out[21 + 4 * c] = (uint32_t)zc_im[c];
            out[22 + 4 * c] = (uint32_t)zc_pw[c];
            out[23 + 4 * c] = (uint32_t)((uint64_t)zc_pw[c] >> 32);
        }
    }

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)ZC_EXIT_ADDR = 0x600Du;
    return 0;
}
