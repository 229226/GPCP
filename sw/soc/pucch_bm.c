/* -----------------------------------------------------------------------------
 * run-pucch: PUCCH format 0 (1-bit SR / HARQ-ACK) detection on the CGRA (P3)
 *
 * tools/nr_ul.py models PUCCH format 0 as one of two cyclic shifts of a ZC
 * root (m_CS = 0 -> ACK/SR present, m_CS = 6 -> NACK/absent).  The gNB decides
 * by comparing the energies of the two correlations.  There is no new
 * microcode here: this reuses the *verified* prach_corr kernel
 * (cgra/kernels/prach_corr.yaml -> prach_corr_prog.h, 129 cycles / 16 PE), running it for
 * just NCAND = 2 candidate shifts instead of the L_RA = 139 shifts of Msg1.
 *
 * For candidate c the array computes
 *
 *     C[c] = sum_{i<127} y[i] * conj(root[(i + m_CS[c]) mod 139])
 *
 * with 16 PE partial sums butterfly-reduced in-array; the CPU cross-checks the
 * array's reduction bit-for-bit, then compares |C[0]|^2 vs |C[1]|^2.
 *
 * Stimulus at 0x8040_0000 (build/soc/pucch.td, tools/nr_pucch.py dump):
 *     word 0 .. 277            : 139 received (re, im) int32 pairs
 *     word 278 + 254*c ..      : candidate c's 127 reference taps (c = 0, 1)
 * Scratch at 0x8030_0000: one fresh 64-word slot per candidate (never reused,
 * so the write-through cache cannot serve a stale line -- AGENTS 4.2).
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
#define NCAND       2u
#define PAIR_WORDS  (2u * TAPS)         /* 254: one block of complex taps */
#define Y_SPAD      (GPCP_LAYOUT_REGION_IN * GPCP_LAYOUT_WORDS)
#define REF_SPAD    (GPCP_LAYOUT_REGION_REF * GPCP_LAYOUT_WORDS)
#define PART_SPAD   (GPCP_LAYOUT_REGION_OUT * GPCP_LAYOUT_WORDS)
#define OUT_WORDS   64u
#define CS_ACK      0u
#define CS_NACK     6u

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
    static const uint32_t mcs[NCAND] = { CS_ACK, CS_NACK };

    if (gpcp_cgra_imem_load(CGRA_BASE, prach_corr_prog, PRACH_CORR_PROG_CYCLES * NPES) != 0)
        errs++;

    /* the received PUCCH sequence stays resident for both candidates */
    if (dma(TD_BASE, Y_SPAD, PAIR_WORDS, 0) != 0)
        goto done;

    for (uint32_t c = 0; c < NCAND; c++) {
        if (dma(refs + 4u * c * PAIR_WORDS, REF_SPAD, PAIR_WORDS, 0) != 0)
            break;
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(PRACH_CORR_SPAD_LEAF_R7(GPCP_LAYOUT_NPES - 1u)), 0);
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(PRACH_CORR_SPAD_LEAF_RI7(GPCP_LAYOUT_NPES - 1u)), 0);
        if (rt_array_run(CGRA_BASE, 0, PRACH_CORR_PROG_CYCLES) != 0) {
            errs++;
            break;
        }
        runs++;
        if (dma(PART_BASE + c * OUT_WORDS * 4u, PART_SPAD, OUT_WORDS, 1) != 0)
            break;
    }

done:
    {
        int64_t re[NCAND], im[NCAND], pw[NCAND];
        for (uint32_t c = 0; c < NCAND; c++) {
            const int32_t *s = (const int32_t *)(PART_BASE + c * OUT_WORDS * 4u);
            int64_t r = 0, i = 0;
            for (uint32_t p = 0; p < NPES; p++) {
                r += s[2 * p];
                i += s[2 * p + 1];
                nchk += 2;
                out[48 + 32 * c + 2 * p] = (uint32_t)s[2 * p];
                out[49 + 32 * c + 2 * p] = (uint32_t)s[2 * p + 1];
            }
            /* the array's own butterfly must agree with the CPU sum, bit for bit */
                        re[c] = r; im[c] = i; pw[c] = r * r + i * i;
            out[20 + 4 * c] = (uint32_t)r;
            out[21 + 4 * c] = (uint32_t)i;
            out[22 + 4 * c] = (uint32_t)pw[c];
            out[23 + 4 * c] = (uint32_t)((uint64_t)pw[c] >> 32);
        }

        uint32_t win = pw[0] >= pw[1] ? 0u : 1u;
        uint32_t los = 1u - win;
        uint32_t bit = (win == 0u) ? 1u : 0u;   /* candidate 0 = m_CS 0 = ACK/SR */
        out[0] = 0xCAFEBABEu;
        out[1] = runs;
        out[2] = dmas;
        out[3] = errs;
        out[4] = 0x600Du;
        out[5] = bit;
        out[6] = win;
        out[7] = mcs[win];
        out[8] = NCAND;
        out[9] = TAPS;
        out[10] = (uint32_t)re[win];
        out[11] = (uint32_t)im[win];
        out[12] = (uint32_t)pw[win];
        out[13] = (uint32_t)((uint64_t)pw[win] >> 32);
        out[14] = (uint32_t)re[los];
        out[15] = (uint32_t)im[los];
        out[16] = (uint32_t)pw[los];
        out[17] = (uint32_t)((uint64_t)pw[los] >> 32);
        out[18] = nchk;
        out[19] = PRACH_CORR_PROG_CYCLES;

        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    }
    return 0;
}
