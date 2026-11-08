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
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"
#include "pdsch_cn_prog.h"
#include "../include/ldpc_bg2.h"

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#define ZC               20
#define NPES             16
#define NBANKS_IN        10        /* 10 edges, sentinel-padded */
#define SENTINEL         32767
#define SENTINEL_BANK    GPCP_LAYOUT_REGION_SENTINEL   /* outside the BN slots (0..23) and results (10..12) */
#define CYC_HALF         (CN_PROG_CYCLES / 2)
#define CYC_TOTAL        CN_PROG_CYCLES
#define LCG_SEED         1u

/* fixed buffers outside .bss (the linker would otherwise put them on top of
 * the result area at 0x8001_0000) */
#define MSG_BASE         0x80030000ul   /* 42*10*20 words -> 0x80038340 */
#define RES_BASE         (0x80010000ul + 8 * 4)   /* 42*3*20 words, right after the summary */
#define LLR_BASE         0x8003C000ul   /* 52*20    words */

static int32_t (*msg)[BG2_MAXD][ZC] = (int32_t (*)[BG2_MAXD][ZC])MSG_BASE;
static int32_t (*res)[3][ZC] = (int32_t (*)[3][ZC])RES_BASE;
static int32_t (*llr)[ZC] = (int32_t (*)[ZC])LLR_BASE;

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

#define SPAD_IDX(bank, word) GPCP_SPAD_IDX((bank), (word))

static uint32_t errs, runs, dmas;

/* 微码不再手写：`cgra/tools/gen_cn_prog.py`（规则映射器）生成 pdsch_cn_prog.h。
 * 装载走拷贝引擎（DMA 直接写 IMEM，8B/beat），不再逐字 AXI-Lite 写。 */
static void load_prog(void)
{
    if (gpcp_cgra_imem_load(CGRA_BASE, cn_prog, CYC_TOTAL * NPES) != 0)
        errs++;
}

static void array_run(uint32_t page)
{
    if (rt_array_run(CGRA_BASE, page, CYC_TOTAL) != 0)
        errs++;
    runs++;
}

/* M1 运行时（sw/include/gpcp_cgra_rt.h）：发起与等待分开，让拷贝引擎与阵列重叠 */
static void dma_begin(uintptr_t src, uint32_t dst, uint32_t len, uint32_t dir,
                      uint32_t page, uint32_t stride, uint32_t row)
{
    if (rt_dma_begin(CGRA_BASE, src, dst, len, page, dir, stride, row) != 0)
        errs++;
    dmas++;
}

static void dma_wait(void)
{
    if (rt_dma_wait(CGRA_BASE) != 0)
        errs++;
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

    load_prog();          /* 两个半镜像合成一个程序，只装一次 */

    /* the sentinel constant must exist in BOTH pages: the array indexes it
     * page-relative (bank 30 of buf_page = absolute bank 30 or 62) */
    for (uint32_t i = 0; i < ZC; i++) {
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK, i), (uint32_t)SENTINEL);
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK + GPCP_LAYOUT_PAGE_BANKS, i), (uint32_t)SENTINEL);
    }

    /* -------- M1 运行时：双页 ping-pong，DMA 与阵列重叠 --------
     * 行 r 的阵列用页 r&1，同时把行 r+1 预取到另一页；
     * 阵列结束后再从页 r&1 把结果搬出（此时阵列空闲，页可以复用给 r+2）。 */
    uint32_t t0 = mtime();
    uint32_t acc_in = 0, acc_arr = 0, acc_out = 0;
    dma_begin((uintptr_t)&msg[0][0][0], 0, NBANKS_IN * ZC, 0, 0, GPCP_LAYOUT_WORDS, ZC);
    dma_wait();
    for (uint32_t r = 0; r < BG2_ROWS; r++) {
        uint32_t ta = mtime();
        if (r + 1 < BG2_ROWS)
            dma_begin((uintptr_t)&msg[r + 1][0][0], 0, NBANKS_IN * ZC, 0,
                      (r + 1) & 1u, GPCP_LAYOUT_WORDS, ZC);
        array_run(r & 1u);                 /* 与预取并行 */
        acc_arr += mtime() - ta;
        ta = mtime();
        if (r + 1 < BG2_ROWS)
            dma_wait();                    /* 预取还没完就等它 */
        acc_in += mtime() - ta;
        ta = mtime();
        dma_begin((uintptr_t)&res[r][0][0], (GPCP_LAYOUT_REGION_MIN1 * GPCP_LAYOUT_WORDS), 3 * ZC, 1, r & 1u, GPCP_LAYOUT_WORDS, ZC);
        dma_wait();
        acc_out += mtime() - ta;
    }
    uint32_t cyc = mtime() - t0;

    /* 分项计时（放在结果区之后的空位，判据不读这里）：DMA 进 / 阵列 / DMA 出 */
    out[2528] = acc_in;
    out[2529] = acc_arr;
    out[2530] = acc_out;

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
