/* GPCP SoC - bare-metal DMA + double-buffering test
 *
 * The CVA6 core drives the CGRA copy engine to stream per-slot channel data
 * into the scratchpad pages while the array is busy with the previous slot:
 *
 *   page 0: even slots, page 1: odd slots
 *   the array runs on page p while the copy engine fills page p^1,
 *   results are copied back (spad -> RAM) while the array processes the
 *   other page
 *
 * Kernel (14 cycles, 16 PEs): per-bank complex correlation, i.e. the shape of
 * the channel-estimate post-processing stage
 *
 *   c0..c3 : LD  r0..r3 <- spad[p][0..3]      (yr, yi, xr, xi)
 *   c4     : LACC zero                        (acc = 0)
 *   c5,c6  : MAC r0,r2 ; MAC r1,r3             (yr*xr + yi*xi)
 *   c7     : SHR r4 <- acc >> 15
 *   c8     : ST  spad[16+p][0] <- r4           (H.re)
 *   c9,c10 : MUL r5 <- r0*r3 ; MUL r6 <- r1*r2
 *   c11    : SUB r7 <- r6 - r5                 (yi*xr - yr*xi)
 *   c12    : SHR r7 <- r7 >> 15
 *   c13    : ST  spad[16+p][1] <- r7           (H.im)
 *
 * Result area (RESULT_BASE, 32-bit words) for the testbench / checker:
 *   [0..31]   slot 0 H[p].re, H[p].im (p = 0..15)
 *   [32..63]  slot 1
 *   [64..95]  slot 2
 *   [96]      marker 0xCAFEBABE
 *   [97]      on-chip error count (DMA timeouts + software self-check)
 *   [98]      slot count
 *   [99]      epilogue marker 0x600D
 */

#include "../include/gpcp_cgra.h"

#define CGRA_BASE    0x50000000ul
#define RESULT_BASE  ((uint32_t *)0x80010000ul)
#define EXIT_ADDR    0x40010000ul

#define SLOTS   3
#define CYCLES  14

/* The copy engine moves 64-bit beats, so both ends must be 8-byte aligned */
static int32_t slots[SLOTS][64] __attribute__((aligned(8)));
static int32_t out[SLOTS][32] __attribute__((aligned(8)));

static uint32_t errs;

/* --------------------------------------------------------------- software */
static void fill_slot(int s)
{
    for (int i = 0; i < 64; i++) {
        int v = ((s * 31 + i * 17 + 11) % 20001) - 10000;
        int k = i & 3;
        /* k = 0,1 -> received symbols (int16), k = 2,3 -> QPSK pilots at
         * full scale (+-0.7071 -> +-23170 in Q15) */
        slots[s][i] = (k >= 2) ? (v >= 0 ? 23170 : -23170) : v;
    }
}

static void load_program(void)
{
    if (GPCP_CGRA_MAX_CYCLES < CYCLES) { errs++; return; }

    for (uint32_t p = 0; p < 16; p++) {
        uint32_t prog[CYCLES];
        prog[0]  = gpcp_cgra_inst(GPCP_OP_LD, 0, 0, 0, GPCP_SPAD_IDX(p, 0));
        prog[1]  = gpcp_cgra_inst(GPCP_OP_LD, 0, 0, 1, GPCP_SPAD_IDX(p, 1));
        prog[2]  = gpcp_cgra_inst(GPCP_OP_LD, 0, 0, 2, GPCP_SPAD_IDX(p, 2));
        prog[3]  = gpcp_cgra_inst(GPCP_OP_LD, 0, 0, 3, GPCP_SPAD_IDX(p, 3));
        prog[4]  = gpcp_cgra_inst(GPCP_OP_LACC, GPCP_SRC_ZERO, 0, 0, 0);
        prog[5]  = gpcp_cgra_inst(GPCP_OP_MAC, 0, 2, 0, 0);
        prog[6]  = gpcp_cgra_inst(GPCP_OP_MAC, 1, 3, 0, 0);
        prog[7]  = gpcp_cgra_inst(GPCP_OP_SHR, GPCP_SRC_ACC, 0, 4, 15);
        prog[8]  = gpcp_cgra_inst(GPCP_OP_ST, 4, 0, 0, GPCP_SPAD_IDX(16 + p, 0));
        prog[9]  = gpcp_cgra_inst(GPCP_OP_MUL, 0, 3, 5, 0);
        prog[10] = gpcp_cgra_inst(GPCP_OP_MUL, 1, 2, 6, 0);
        prog[11] = gpcp_cgra_inst(GPCP_OP_SUB, 6, 5, 7, 0);
        prog[12] = gpcp_cgra_inst(GPCP_OP_SHR, 7, 0, 7, 15);
        prog[13] = gpcp_cgra_inst(GPCP_OP_ST, 7, 0, 0, GPCP_SPAD_IDX(16 + p, 1));

        for (uint32_t c = 0; c < CYCLES; c++)
            gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + (c * 16 + p) * 4u,
                            prog[c]);
    }
}

/* ------------------------------------------------------------------ DMA */
/* Wait for the engine to become idle: a start written while busy is ignored
 * (there is no descriptor queue yet). */
static int dma_idle(uint32_t *spins)
{
    uint32_t guard = 0;
    while (gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_BUSY) {
        if (++guard > 200000u) { errs++; return -1; }
    }
    *spins += guard;
    return 0;
}

/* Wait for the current transfer to complete (the start clears `done`). */
static int dma_wait(uint32_t *spins)
{
    uint32_t guard = 0;
    while (!(gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_DONE)) {
        if (++guard > 200000u) { errs++; return -1; }
    }
    *spins += guard;
    return 0;
}

/* 2D copy: src RAM address, page-relative spad index, words, page, direction,
 * stride between rows, words per row (0 = linear). */
static void dma_start(uintptr_t src, uint32_t dst, uint32_t len, uint32_t page,
                      uint32_t dir, uint32_t stride, uint32_t row, uint32_t *spins)
{
    if (dma_idle(spins))
        return;
    gpcp_cgra_dma_start(CGRA_BASE, src, dst, len, page, dir, stride, row);
}

/* One slot = 64 words (16 banks x 4 words): 2D walk, row = 4, stride = 32. */
static void dma_in(int s, uint32_t page, uint32_t *spins)
{
    dma_start((uintptr_t)&slots[s][0], 0, 64, page, 0, 32, 4, spins);
}

/* 32 results = 16 banks x 2 words taken from the upper half of the page. */
static void dma_out(int s, uint32_t page, uint32_t *spins)
{
    dma_start((uintptr_t)&out[s][0], GPCP_SPAD_IDX(16, 0), 32, page, 1, 32, 2, spins);
}

/* --------------------------------------------------------------- array */
static void array_start(uint32_t page)
{
    gpcp_cgra_array_start(CGRA_BASE, page, CYCLES);
}

static void array_run(uint32_t page, uint32_t *spins)
{
    array_start(page);

    uint32_t guard = 0;
    while (!(gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE)) {
        if (++guard > 200000u) { errs++; return; }
    }
    *spins += guard;
}

/* --------------------------------------------------------------- verify */
static int32_t sx16_s(int32_t v)
{
    v &= 0xFFFF;
    return (v & 0x8000) ? v - 0x10000 : v;
}

static uint32_t self_check(void)
{
    uint32_t bad = 0;
    for (int s = 0; s < SLOTS; s++) {
        for (int p = 0; p < 16; p++) {
            int32_t yr = sx16_s(slots[s][p * 4 + 0]);
            int32_t yi = sx16_s(slots[s][p * 4 + 1]);
            int32_t xr = sx16_s(slots[s][p * 4 + 2]);
            int32_t xi = sx16_s(slots[s][p * 4 + 3]);
            int32_t hre = (yr * xr + yi * xi) >> 15;
            int32_t him = (yi * xr - yr * xi) >> 15;
            if ((uint32_t)out[s][2 * p + 0] != ((uint32_t)hre & 0xFFFFFFFFu))
                bad++;
            if ((uint32_t)out[s][2 * p + 1] != ((uint32_t)him & 0xFFFFFFFFu))
                bad++;
        }
    }
    return bad;
}

/* ----------------------------------------------------------------- main */
int main(void)
{
    uint32_t *res = RESULT_BASE;
    uint32_t spins = 0;

    for (int s = 0; s < SLOTS; s++) {
        fill_slot(s);
        for (int i = 0; i < 32; i++)
            out[s][i] = 0;
    }

    load_program();

    /* warm-up: the first slot has to be in place before the pipeline starts */
    dma_in(0, 0, &spins);
    dma_wait(&spins);

    /* ---- slot 0 (page 0): prefetch slot 1 into page 1, then compute ---- */
    dma_in(1, 1, &spins);
    array_run(0, &spins);            /* array and prefetch overlap */
    dma_wait(&spins);

    /* copy the results of page 0 back while the array starts on page 1 */
    dma_out(0, 0, &spins);
    array_run(1, &spins);            /* copy-out and array overlap */
    dma_wait(&spins);

    /* ---- drain: page 1 results, then slot 2 into the freed page 0 ---- */
    dma_out(1, 1, &spins);
    dma_wait(&spins);
    dma_in(2, 0, &spins);
    dma_wait(&spins);

    array_run(0, &spins);
    dma_out(2, 0, &spins);
    dma_wait(&spins);

    /* ---- export results for the testbench ---- */
    for (int s = 0; s < SLOTS; s++)
        for (int i = 0; i < 32; i++)
            res[s * 32 + i] = (uint32_t)out[s][i];

    res[96] = 0xCAFEBABEu;
    res[97] = errs + self_check();
    res[98] = (uint32_t)SLOTS;
    res[99] = 0x600Du;

    /* drain the posted stores before telling the testbench to stop */
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
