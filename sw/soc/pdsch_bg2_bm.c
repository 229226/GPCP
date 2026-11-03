/* -----------------------------------------------------------------------------
 * Real 38.212 BG2 code block: all 42 check nodes, DMA-blocked (P2 step 2b)
 *
 * The full block cannot live in the 8 KiB scratchpad (52*Zc LLRs + 197*Zc
 * messages ~ 5000 words at Zc=20), so the parity-check work is *blocked*: one
 * check node at a time is streamed in, processed, and streamed out.
 *
 *   RAM: llr[52][20] (channel LLRs), msg[42][10][20] (messages per edge),
 *        res[42][3][20] (min1, min2, sign) -- see the fixed buffers below.
 *   Per check node r (degree d = bg2_deg[r]):
 *     1. DMA in 200 words (always 10 banks; edges >= d carry a +32767
 *        sentinel, so they cannot become min1/min2 and contribute sign 0).
 *        The 2D walk (row = 20 words, stride = 32) puts edge k in bank k,
 *        lifted row i in word i.
 *     2. Two array runs: PEs 0..15 handle lifted rows 0..15, PEs 0..3 the rows
 *        16..19 (Zc = 20 > 16 PEs).
 *     3. DMA out 60 words (banks 10..12 = min1, min2, sign).
 *
 * The degree is handled without per-degree microcode: the sentinel padding
 * makes one 10-input min-sum image valid for every check node.
 *
 * Results at 0x8001_0000:
 *   [0] 0xCAFEBABE  [1] check nodes  [2] array runs  [3] DMA transfers
 *   [4] cycles      [5] errors/timeouts  [6] 0x600D
 * The per-row min1/min2/sign values are checked by tools/check_pdsch_bg2.py
 * against the Python model (same LLR stream, same base graph).
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/ldpc_bg2.h"

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#define ZC               20
#define NPES             16
#define NBANKS_IN        10        /* 10 edges, sentinel-padded */
#define SENTINEL         32767
#define CYC_HALF         90
#define LCG_SEED         1u

/* fixed buffers outside .bss (the linker would otherwise put them on top of
 * the result area at 0x8001_0000) */
#define MSG_BASE         0x80030000ul   /* 42*10*20 words -> 0x80038340 */
#define RES_BASE         (0x80010000ul + 8 * 4)   /* 42*3*20 words, right after the summary */
#define LLR_BASE         0x8003C000ul   /* 52*20    words */

static int32_t (*msg)[BG2_MAXD][ZC] = (int32_t (*)[BG2_MAXD][ZC])MSG_BASE;
static int32_t (*res)[3][ZC] = (int32_t (*)[3][ZC])RES_BASE;
static int32_t (*llr)[ZC] = (int32_t (*)[ZC])LLR_BASE;

static inline void mmio_w(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline uint32_t mmio_r(uintptr_t a) { return *(volatile uint32_t *)a; }
static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

#define SPAD_IDX(bank, word) GPCP_SPAD_IDX((bank), (word))

static uint32_t prog_half[2][CYC_HALF * NPES];
static uint32_t errs, runs, dmas;

static void emit(uint32_t which, uint32_t cyc, uint32_t pe, uint32_t op,
                 uint32_t a, uint32_t b, uint32_t dst, uint32_t imm)
{
    prog_half[which][cyc * NPES + pe] = gpcp_cgra_inst(op, a, b, dst, imm);
}

/* One min-sum check node over 10 (sentinel-padded) edges.
 * word_base = 0 for lifted rows 0..15, 16 for rows 16..19 (4 PEs only). */
static void build_half(uint32_t which, uint32_t word_base)
{
    for (uint32_t pe = 0; pe < NPES; pe++) {
        uint32_t w = word_base + pe;
        if (w >= ZC)
            continue;
        uint32_t cyc = 0;
        /* k = 0: |x| -> min1, min2 = +inf (bank 13 holds the sentinel) */
        emit(which, cyc++, pe, GPCP_OP_LD, 0, 0, 0, SPAD_IDX(0, w));
        emit(which, cyc++, pe, GPCP_OP_SHR, 0, 0, 1, 31);
        emit(which, cyc++, pe, GPCP_OP_XOR, 0, 1, 2, 0);
        emit(which, cyc++, pe, GPCP_OP_SUB, 2, 1, 3, 0);
        emit(which, cyc++, pe, GPCP_OP_MOV, 3, 0, 5, 0);          /* min1 */
        emit(which, cyc++, pe, GPCP_OP_LD, 0, 0, 6, SPAD_IDX(13, w)); /* +inf */
        emit(which, cyc++, pe, GPCP_OP_SHR, 1, 0, 7, 0);          /* sign = x0>>31 */
        for (uint32_t k = 1; k < NBANKS_IN; k++) {
            emit(which, cyc++, pe, GPCP_OP_LD, 0, 0, 0, SPAD_IDX(k, w));
            emit(which, cyc++, pe, GPCP_OP_SHR, 0, 0, 1, 31);
            emit(which, cyc++, pe, GPCP_OP_XOR, 0, 1, 2, 0);
            emit(which, cyc++, pe, GPCP_OP_SUB, 2, 1, 3, 0);      /* |x| */
            emit(which, cyc++, pe, GPCP_OP_XOR, 7, 1, 7, 0);      /* sign ^= */
            /* min2' = max(min1, min(min2, |x|)); min1' = min(min1, |x|) */
            emit(which, cyc++, pe, GPCP_OP_MIN, 6, 3, 4, 0);
            emit(which, cyc++, pe, GPCP_OP_MAX, 5, 4, 6, 0);
            emit(which, cyc++, pe, GPCP_OP_MIN, 5, 3, 5, 0);
        }
        emit(which, cyc++, pe, GPCP_OP_ST, 5, 0, 0, SPAD_IDX(10, w));   /* min1 */
        emit(which, cyc++, pe, GPCP_OP_ST, 6, 0, 0, SPAD_IDX(11, w));   /* min2 */
        emit(which, cyc++, pe, GPCP_OP_ST, 7, 0, 0, SPAD_IDX(12, w));   /* sign */
    }
}

static void load_prog(uint32_t which)
{
    for (uint32_t i = 0; i < CYC_HALF * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, prog_half[which][i]);
}

static void array_run(void)
{
    gpcp_cgra_array_start(CGRA_BASE, 0, CYC_HALF);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY) {
        if (++spins > 200000u) { errs++; break; }
    }
    runs++;
}

static int dma_idle(void)
{
    uint32_t guard = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_BUSY)
        if (++guard > 200000u) { errs++; return -1; }
    return 0;
}

static int dma_wait(void)
{
    uint32_t guard = 0;
    while (!(mmio_r(CGRA_BASE + GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_DONE))
        if (++guard > 200000u) { errs++; return -1; }
    return 0;
}

static void dma_2d(uintptr_t src, uint32_t dst, uint32_t len, uint32_t dir,
                   uint32_t stride, uint32_t row)
{
    if (dma_idle())
        return;
    gpcp_cgra_dma_start(CGRA_BASE, src, dst, len, 0, dir, stride, row);
    dma_wait();
    dmas++;
}

/* --------------------------------------------------------------- payload */
static uint32_t lcg = LCG_SEED;
static int32_t next_llr(void)
{
    lcg = lcg * 1664525u + 1013904223u;
    return (int32_t)((lcg >> 16) & 0x3FFu) - 512;
}

int main(void)
{
    uint32_t *out = RESULT_BASE;

    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t i = 0; i < ZC; i++)
            llr[b][i] = next_llr();

    /* initial messages = channel LLR with the lift shift of that edge */
    for (uint32_t r = 0; r < BG2_ROWS; r++)
        for (uint32_t k = 0; k < BG2_MAXD; k++)
            for (uint32_t i = 0; i < ZC; i++)
                msg[r][k][i] = (k < bg2_deg[r])
                               ? llr[bg2_col[r][k]][(i + bg2_sh[r][k]) % ZC]
                               : SENTINEL;

    build_half(0, 0);
    build_half(1, 16);

    /* the sentinel constant lives in bank 13 (one word per lifted row) */
    for (uint32_t i = 0; i < ZC; i++)
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(13, i), (uint32_t)SENTINEL);

    uint32_t t0 = mtime();
    for (uint32_t r = 0; r < BG2_ROWS; r++) {
        dma_2d((uintptr_t)&msg[r][0][0], 0, NBANKS_IN * ZC, 0, 32, ZC);
        load_prog(0);
        array_run();
        load_prog(1);
        array_run();
        dma_2d((uintptr_t)&res[r][0][0], 10 * 32, 3 * ZC, 1, 32, ZC);
    }
    uint32_t cyc = mtime() - t0;

    out[0] = 0xCAFEBABEu;
    out[1] = BG2_ROWS;
    out[2] = runs;
    out[3] = dmas;
    out[4] = cyc;
    out[5] = errs;
    out[6] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
