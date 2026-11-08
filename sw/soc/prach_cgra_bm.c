/* -----------------------------------------------------------------------------
 * run-prach: Msg1 PRACH detection on the CGRA (38.211 6.3.3 cyclic-shift search)
 *
 * The gNB knows one ZC root sequence (u = 25, L_RA = 139) and must recover the
 * cyclic shift d = preamble*N_CS + TA of the received preamble.  One array run
 * computes, for one candidate shift d,
 *
 *     C[d] = sum_{i<127} y[i] * conj(root[(i+d) mod 139])
 *
 * with the real part accumulated in `acc` and the imaginary part in a register,
 * 微码由 L1/L2 编译器生成（`cgra/kernels/prach_corr.yaml` ->
 * `prach_corr_prog.h`，`cgra/tools/gen_kernel_prog.py`）：16 PE 各算 8 个 tap 的
 * 部分和，**不再在阵列内做 16->1 蝶形**；CPU 读回 16 对部分和并在标量侧求和。
 * 每个候选：拷贝引擎重装 254 个参考字，阵列跑 129 拍，读回 64 个 spad 字。
 *
 * 编译器版每个 PE 统一 8 tap（t = 8p..8p+7），第 128 个 tap（t=127）手写版是跳过
 * 的；这里把 ref[127] 那一对（spad 510/511）清零，使两种形式等价。
 *
 * The CPU sums the 16 partials itself, finds the peak and the runner-up, and
 * splits the peak position into preamble index and timing advance with N_CS.
 *
 * Stimulus at 0x8040_0000 (build/rach/prach.td, tools/nr_rach.py dump):
 *     word 0        : 139 received chips (re, im) int32 pairs
 *     word 278+254d : candidate d's 127 reference taps
 * Scratch at 0x8030_0000: one fresh 64-word slot per candidate (never reused,
 * so the write-through cache cannot serve a stale line -- AGENTS 4.2).
 *
 * Results at 0x8007_0000:
 *   [0] marker [1] runs [2] DMA [3] errors [4] 0x600D
 *   [5] peak delay [6] preamble [7] TA [8] peak re [9] peak im
 *   [10] 2nd delay [11] 2nd re [12] 2nd im [13] N_CS [14] L_RA [15] taps
 *   [16] partials cross-checked [17] program cycles
 *   [20 + 2d] C[d].re, [21 + 2d] C[d].im   (d = 0..138)
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/gpcp_cgra_rt.h"
#include "prach_corr_prog.h"   /* 编译器生成（prach_corr.yaml） */
#include "prach_corr_layout.h"

#define CGRA_BASE   0x50000000ul
#define EXIT_ADDR   0x40010000ul
#define RESULT      ((uint32_t *)0x80070000ul)
#define TD_BASE     0x80400000ul
#define PART_BASE   0x80300000ul

#define L_RA        139u
#define TAPS        127u
#define NPES        16u
#define PAIR_WORDS  (2u * TAPS)         /* 254: one block of complex taps */
#define Y_SPAD      (GPCP_LAYOUT_REGION_IN * GPCP_LAYOUT_WORDS)                  /* rx chips   -> banks 0..7       */
#define REF_SPAD    (GPCP_LAYOUT_REGION_REF * GPCP_LAYOUT_WORDS)                /* reference  -> banks 8..15      */
#define PART_SPAD   (GPCP_LAYOUT_REGION_OUT * GPCP_LAYOUT_WORDS)                /* partials at 512+2p (re) / 513+2p (im) */
#define OUT_WORDS   64u
#define N_CS        15u                 /* zero-correlation zone (SIB1 zczc 0) */

static uint32_t runs, dmas, errs, nchk;

static int dma(uintptr_t ram, uint32_t spad, uint32_t words, uint32_t dir)
{
    if (rt_dma_run(CGRA_BASE, ram, spad, words, 0, dir, 0, 0)) {
        errs++;
        return -1;
    }
    dmas++;
    return 0;
}

int main(void)
{
    uint32_t *out = RESULT;
    uintptr_t refs = TD_BASE + 4u * (2u * L_RA);        /* image word 278 */

    if (gpcp_cgra_imem_load(CGRA_BASE, prach_corr_prog, PRACH_CORR_PROG_CYCLES * NPES) != 0)
        errs++;

    /* the received preamble stays resident for the whole shift search */
    if (dma(TD_BASE, Y_SPAD, PAIR_WORDS, 0) != 0)
        goto done;

    for (uint32_t d = 0; d < L_RA; d++) {
        if (dma(refs + 4u * d * PAIR_WORDS, REF_SPAD, PAIR_WORDS, 0) != 0)
            break;
        /* 编译器版每 PE 统一 8 tap：t=127 的参考对被清零（手写版直接跳过它） */
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(PRACH_CORR_SPAD_LEAF_R7(GPCP_LAYOUT_NPES - 1u)), 0);
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(PRACH_CORR_SPAD_LEAF_RI7(GPCP_LAYOUT_NPES - 1u)), 0);
        if (rt_array_run(CGRA_BASE, 0, PRACH_CORR_PROG_CYCLES) != 0) {
            errs++;
            break;
        }
        runs++;
        if (dma(PART_BASE + d * OUT_WORDS * 4u, PART_SPAD, OUT_WORDS, 1) != 0)
            break;
    }

done:
    {
        int64_t best = -1, second = -1;
        uint32_t bd = 0, sd = 0;
        int64_t bre = 0, bim = 0, sre = 0, sim = 0;

        for (uint32_t d = 0; d < L_RA; d++) {
            const int32_t *s = (const int32_t *)(PART_BASE + d * OUT_WORDS * 4u);
            int64_t re = 0, im = 0;
            for (uint32_t p = 0; p < NPES; p++) {
                re += s[2 * p];
                im += s[2 * p + 1];
                nchk += 2;
            }
            int64_t pw = re * re + im * im;
            if (pw > best) {
                second = best; sre = bre; sim = bim; sd = bd;
                best = pw; bre = re; bim = im; bd = d;
            } else if (pw > second) {
                second = pw; sre = re; sim = im; sd = d;
            }
            out[20 + 2 * d] = (uint32_t)re;
            out[21 + 2 * d] = (uint32_t)im;
        }

        out[0] = 0xCAFEBABEu;
        out[1] = runs;
        out[2] = dmas;
        out[3] = errs;
        out[4] = 0x600Du;
        out[5] = bd;
        out[6] = bd / N_CS;
        out[7] = bd % N_CS;
        out[8] = (uint32_t)bre;
        out[9] = (uint32_t)bim;
        out[10] = sd;
        out[11] = (uint32_t)sre;
        out[12] = (uint32_t)sim;
        out[13] = N_CS;
        out[14] = L_RA;
        out[15] = TAPS;
        out[16] = nchk;
        out[17] = PRACH_CORR_PROG_CYCLES;
        out[18] = runs == L_RA ? 1u : 0u;
        out[19] = 0u;

        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    }
    return 0;
}
