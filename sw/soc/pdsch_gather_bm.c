/* -----------------------------------------------------------------------------
 * Debug: per-edge reconstruction + gather for ONE variable block (P2)
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
 *   [0] 0xCAFEBABE  [1] edges of block 0  [2] array runs  [3] DMA
 *   [4] cycles      [5] errors  [6] 0x600D
 *   [8..]     per-edge reconstructed messages of block 0 (n edges x 20)
 *   [500..]   the 24 x 20 BN input gathered for block 0
 * The per-row min1/min2/sign values are checked by tools/check_pdsch_bg2.py
 * against the Python model (same LLR stream, same base graph).
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/ldpc_bg2.h"
/* 编译器产物（cgra/kernels/cn10.yaml、bn24.yaml）：Zc=20 > 16 PE，
 * word_base 0 / 16 两份镜像各跑一次。所有指令字由 rule_map.schedule 生成。 */
#include "cn10_w0_prog.h"
#include "cn10_w16_prog.h"
#include "bn24_w0_prog.h"
#include "bn24_w16_prog.h"

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#define ZC               20
#define NPES             16
#define NBANKS_IN        10        /* 10 edges, sentinel-padded */
#define SENTINEL         32767
#define LCG_SEED         7u
#define BN_SLOTS         24
#define SENTINEL_BANK    30   /* outside the BN slots (0..23) and results (10..12) */

/* fixed buffers outside .bss (the linker would otherwise put them on top of
 * the result area at 0x8001_0000) */
#define MSG_BASE         0x80030000ul   /* 42*10*20 words -> 0x80038340 */
#define RES_BASE         (0x80010000ul + 8 * 4)   /* 42*3*20 words, right after the summary */
#define LLR_BASE         0x8003C000ul   /* 52*20    words */

static int32_t (*msg)[BG2_MAXD][ZC] = (int32_t (*)[BG2_MAXD][ZC])MSG_BASE;
static int32_t (*res)[3][ZC] = (int32_t (*)[3][ZC])RES_BASE;
static int32_t (*llr)[ZC] = (int32_t (*)[ZC])LLR_BASE;

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static int32_t bout[26 * ZC] __attribute__((aligned(8)));   /* BN output buffer (separate from the input) */
static uint32_t errs, runs, dmas;

static void load_prog(uint32_t which)
{
    const uint32_t *p = which ? cn10_w16_prog : cn10_w0_prog;
    uint32_t n = which ? CN10_W16_PROG_CYCLES : CN10_W0_PROG_CYCLES;
    for (uint32_t i = 0; i < n * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, p[i]);
}

static void array_run(uint32_t which)
{
    gpcp_cgra_array_start(CGRA_BASE, 0,
                          which ? CN10_W16_PROG_CYCLES : CN10_W0_PROG_CYCLES);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY) {
        if (++spins > 200000u) { errs++; break; }
    }
    runs++;
}

static void load_prog_bn(uint32_t which)
{
    const uint32_t *p = which ? bn24_w16_prog : bn24_w0_prog;
    uint32_t n = which ? BN24_W16_PROG_CYCLES : BN24_W0_PROG_CYCLES;
    for (uint32_t i = 0; i < n * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, p[i]);
}

static void array_run_bn(uint32_t which)
{
    gpcp_cgra_array_start(CGRA_BASE, 0,
                          which ? BN24_W16_PROG_CYCLES : BN24_W0_PROG_CYCLES);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY)
        if (++spins > 2000000u) { errs++; break; }
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
    const uint32_t TARGET = 0;          /* the variable block under test */

    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t i = 0; i < ZC; i++)
            llr[b][i] = next_llr();

    for (uint32_t r = 0; r < BG2_ROWS; r++)
        for (uint32_t k = 0; k < BG2_MAXD; k++)
            for (uint32_t i = 0; i < ZC; i++)
                msg[r][k][i] = (k < bg2_deg[r])
                               ? llr[bg2_col[r][k]][(i + bg2_sh[r][k]) % ZC]
                               : SENTINEL;

    for (uint32_t i = 0; i < ZC; i++)
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK, i), (uint32_t)SENTINEL);

    uint32_t t0 = mtime();
    for (uint32_t r = 0; r < BG2_ROWS; r++) {
        dma_2d((uintptr_t)&msg[r][0][0], 0, NBANKS_IN * ZC, 0, 32, ZC);
        load_prog(0);
        array_run(0);
        load_prog(1);
        array_run(1);
        dma_2d((uintptr_t)&res[r][0][0], 10 * 32, 3 * ZC, 1, 32, ZC);
    }
    uint32_t cyc = mtime() - t0;

    /* per-edge reconstruction (same rule as the parked decode bench) */
    for (uint32_t r = 0; r < BG2_ROWS; r++)
        for (uint32_t k = 0; k < bg2_deg[r]; k++)
            for (uint32_t i = 0; i < ZC; i++) {
                int32_t a = msg[r][k][i];
                int32_t ab = a < 0 ? -a : a;
                int32_t mag = (ab == res[r][0][i]) ? res[r][1][i] : res[r][0][i];
                int32_t neg = (a < 0) ^ (res[r][2][i] != 0);
                msg[r][k][i] = neg ? -mag : mag;
            }

    /* gather block TARGET's edges: n x 20 raw values, then the 24-slot input */
    uint32_t n = 0;
    static int32_t gath[26 * ZC] __attribute__((aligned(8)));
    for (uint32_t r = 0; r < BG2_ROWS; r++)
        for (uint32_t k = 0; k < bg2_deg[r]; k++)
            if (bg2_col[r][k] == TARGET) {
                uint32_t sh = bg2_sh[r][k] % ZC;   /* the table holds V up to 175 */
                for (uint32_t i = 0; i < ZC; i++) {
                    out[8 + n * ZC + i] = (uint32_t)msg[r][k][i];
                    gath[n * ZC + i] = msg[r][k][(i + ZC - sh) % ZC];
                }
                n++;
            }
    for (uint32_t k = n; k < 24; k++)
        for (uint32_t i = 0; i < ZC; i++)
            gath[k * ZC + i] = 0;
    for (uint32_t i = 0; i < ZC; i++)
        gath[24 * ZC + i] = llr[TARGET][i];

    for (uint32_t k = 0; k < 26; k++)
        for (uint32_t i = 0; i < ZC; i++)
            out[500 + k * ZC + i] = (uint32_t)gath[k * ZC + i];

    /* BN with a separate output buffer: no in-place aliasing at all */
    dma_2d((uintptr_t)gath, 0, 26 * ZC, 0, 32, ZC);
    /* dump what actually landed in the scratchpad, before the BN runs */
    for (uint32_t k = 0; k < 26; k++)
        for (uint32_t i = 0; i < ZC; i++)
            out[1540 + k * ZC + i] =
                (uint32_t)mmio_r(CGRA_BASE + GPCP_SPAD_ADDR(k, i));
    load_prog_bn(0);
    array_run_bn(0);
    load_prog_bn(1);
    array_run_bn(1);
    /* BN 结果用 host 窗口逐字读回：spad 里的值是对的，DMA out 的 2D
     * 搬回在这块 26-bank 布局的首尾边界会漏字（见 spad 输入 dump 的
     * 同一条读法）。这是 debug bench，520 次 MMIO 读的代价可接受。 */
    for (uint32_t k = 0; k < 26; k++)
        for (uint32_t i = 0; i < ZC; i++)
            bout[k * ZC + i] =
                (int32_t)mmio_r(CGRA_BASE + GPCP_SPAD_ADDR(k, i));
    for (uint32_t k = 0; k < 26; k++)
        for (uint32_t i = 0; i < ZC; i++)
            out[1020 + k * ZC + i] = (uint32_t)bout[k * ZC + i];

    out[0] = 0xCAFEBABEu;
    out[1] = n;
    out[2] = runs;
    out[3] = dmas;
    out[4] = cyc;
    out[5] = errs;
    out[6] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
