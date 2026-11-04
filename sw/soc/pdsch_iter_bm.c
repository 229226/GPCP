/* -----------------------------------------------------------------------------
 * P1: 真实 BG2 的完整 LDPC 迭代（52 变量块 × ITERS 次）—— 硬件整链，不再是模型
 *
 * 每次迭代：
 *   1) CN：42 个检查节点，M1 运行时双页 ping-pong（cn_prog，172 拍/行）；
 *   2) 逐边重构（CPU，纯算术）：m' = sign·mag，mag 取 min1/min2；
 *   3) 每个变量块 b：gather（24 槽 + LLR）→ DMA 进 → BN（bn_prog，155 拍）→ DMA 出 → scatter；
 *   4) 最后一次迭代后把每块的硬判决（BN 的 bank 25）收进结果区。
 *
 * 判据：tools/check_pdsch_iter.py（同一份 LLR、同一个基图、纯 min-sum、同一 gather/scatter 约定）
 *
 * 数据流（CPU 与引擎的交接都在 RAM 里，所以要处理 D$ 一致性）：
 *   msg —— CPU 写（重构/散射）→ 引擎读（CN 的 DMA 进）。WT 写穿，RAM 天然是新的；
 *   res —— 引擎写（CN 的 DMA 出）→ CPU 读（重构）。每轮读之前必须 rt_dcache_flush()；
 *   bout —— 引擎写（BN 的 DMA 出）→ CPU 读（散射/硬判决）。每块独占一段 + 每轮刷新。
 *
 * 结果区（0x8001_0000）：[0] marker [1] blocks [2] runs [3] dma [4] cycles [5] errs [6] 0x600D
 *                        [8..] 52 × 20 个硬判决（块优先）
 *                        [1048] CN 累计 [1049] BN 累计 [1050] D$失效+重构 累计（分项计时，判据不读）
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/gpcp_cgra_rt.h"
#include "pdsch_cn_prog.h"
#include "pdsch_bn_prog.h"
#include "../include/ldpc_bg2.h"

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80010000ul)
#define ZC            20
#define NPES          16
#define MAXD          10
#define SENTINEL      32767
#define SENTINEL_BANK 30
#ifndef ITERS
#define ITERS         8           /* 可用 -DITERS=n 覆盖（调试时 n=1） */
#endif
#define LCG_SEED      7u          /* 与 tools/check_pdsch_iter.py 同一份 LLR */

/* 固定缓冲（都在 0x8003_0000 以上，避开 .bss/栈与结果区）。
 * 关键约束：**引擎写的缓冲不能和 CPU 写过的缓冲重叠**——CVA6 的 D$ 是 WT
 * （写穿、写时分配），引擎直接写 RAM 不会让 D$ 失效；一旦两块缓冲落在同一
 * 条 cache line，CPU 读到的就是自己以前写进去的旧值（本项目在 sss_bm.c 里
 * 踩过同一个坑）。所以 res / bout 各自独占一段地址，互不重叠。 */
#define MSG_BASE   0x80030000ul   /* 42*10*20 = 8400 words -> 0x80038340 */
#define RES_BASE   0x80039000ul   /* 42*3*20  = 2520 words -> 0x8003B760 */
#define LLR_BASE   0x8003C000ul   /* 52*20    = 1040 words -> 0x8003D040 */
#define GATH_BASE  0x8003E000ul   /* 26*20    = 520  words -> 0x8003E820 */
/* bout 每个变量块独占一段：块与块之间也会"引擎写→CPU 读"，共用一个缓冲
 * 会让块 b 读到块 b-1 的旧值（同一坑）。52 x 520 字 = 108 KiB。 */
#define BOUT_BASE  0x80040000ul
#define BOUT_STRIDE (26 * ZC)     /* 每块 520 字 -> 52 块到 0x80056900 */

static int32_t (*msg)[MAXD][ZC] = (int32_t (*)[MAXD][ZC])MSG_BASE;
static int32_t (*res)[3][ZC] = (int32_t (*)[3][ZC])RES_BASE;
static int32_t (*llr)[ZC] = (int32_t (*)[ZC])LLR_BASE;
static int32_t (*gath)[ZC] = (int32_t (*)[ZC])GATH_BASE;
static int32_t *bout_all = (int32_t *)BOUT_BASE;   /* 每块一段：bout_all + b*520 */

static inline uint32_t mmio_r(uintptr_t a) { return *(volatile uint32_t *)a; }
static inline void mmio_w(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }
#define SPAD_IDX(bank, word) GPCP_SPAD_IDX((bank), (word))

static uint32_t runs, dmas, errs;

/* --------------------------------------------------------- D$ 一致性
 * 阵列/拷贝引擎写 RAM，不会让 CVA6 的 D$ 失效（D$ 是 WT：CPU 读命中就返回
 * 缓存里的旧值）。所以每轮读"引擎刚写的 res / bout"之前，必须先把 D$ 整片
 * 失效：`rt_dcache_flush()`（CSR_DCACHE 关闭→重新使能，WT missunit 会走
 * FLUSH 流程，同时排空写缓冲）。实测每轮约 4k 拍。 */

/* 变量块 → 自己的边表，启动时建一次（避免每个块扫 42×10 条边） */
static uint8_t blk_n[BG2_COLS];
static uint8_t blk_r[BG2_COLS][24], blk_k[BG2_COLS][24], blk_sh[BG2_COLS][24];
/* 预旋转索引表：消掉 gather/scatter 里每个元素的 % ZC */
static uint8_t rotm[ZC][ZC], rotp[ZC][ZC];

static void build_tables(void)
{
    for (uint32_t sh = 0; sh < ZC; sh++)
        for (uint32_t i = 0; i < ZC; i++) {
            rotm[sh][i] = (uint8_t)((i + ZC - sh) % ZC);   /* gather: (i - sh) */
            rotp[sh][i] = (uint8_t)((i + sh) % ZC);        /* scatter: (i + sh) */
        }
    for (uint32_t b = 0; b < BG2_COLS; b++) {
        uint8_t n = 0;
        for (uint32_t r = 0; r < BG2_ROWS; r++)
            for (uint32_t k = 0; k < bg2_deg[r]; k++)
                if (bg2_col[r][k] == b) {
                    blk_r[b][n] = (uint8_t)r;
                    blk_k[b][n] = (uint8_t)k;
                    blk_sh[b][n] = (uint8_t)(bg2_sh[r][k] % ZC);
                    n++;
                }
        blk_n[b] = n;
    }
}

static void cn_row_in(uint32_t r, uint32_t page)
{
    if (rt_dma_begin(CGRA_BASE, (uintptr_t)&msg[r][0][0], 0, MAXD * ZC, page, 0, 32, ZC))
        errs++;
    dmas++;
}
static void cn_row_out(uint32_t r, uint32_t page)
{
    if (rt_dma_begin(CGRA_BASE, (uintptr_t)&res[r][0][0], 10 * 32, 3 * ZC, page, 1, 32, ZC))
        errs++;
    dmas++;
}

/* 逐边重构：m' = sign(a)·mag，mag = (|a| == min1) ? min2 : min1 */
static void reconstruct(void)
{
    for (uint32_t r = 0; r < BG2_ROWS; r++)
        for (uint32_t k = 0; k < bg2_deg[r]; k++)
            for (uint32_t i = 0; i < ZC; i++) {
                int32_t a = msg[r][k][i];
                int32_t ab = a < 0 ? -a : a;
                int32_t mag = (ab == res[r][0][i]) ? res[r][1][i] : res[r][0][i];
                int32_t neg = (a < 0) ^ (res[r][2][i] != 0);
                msg[r][k][i] = neg ? -mag : mag;
            }
}

/* 一个变量块：gather → BN → 搬回 → scatter；返回硬判决写进 hard[b] */
static void bn_block(uint32_t b, int32_t *hard)
{
    uint32_t n = blk_n[b];
    int32_t (*bout)[ZC] = (int32_t (*)[ZC])(bout_all + b * BOUT_STRIDE);
    for (uint32_t e = 0; e < n; e++) {
        uint32_t r = blk_r[b][e], k = blk_k[b][e];
        const uint8_t *rm = rotm[blk_sh[b][e]];
        int32_t *dst = gath[e];
        const int32_t *src = msg[r][k];
        for (uint32_t i = 0; i < ZC; i++)
            dst[i] = src[rm[i]];
    }
    for (uint32_t k = n; k < 24; k++)
        for (uint32_t i = 0; i < ZC; i++)
            gath[k][i] = 0;
    for (uint32_t i = 0; i < ZC; i++)
        gath[24][i] = llr[b][i];

    if (rt_dma_run(CGRA_BASE, (uintptr_t)&gath[0][0], 0, 26 * ZC, 0, 0, 32, ZC))
        errs++;
    dmas++;
    if (rt_array_run(CGRA_BASE, 0, BN_PROG_CYCLES))
        errs++;
    runs++;
    if (rt_dma_run(CGRA_BASE, (uintptr_t)&bout[0][0], 0, 26 * ZC, 0, 1, 32, ZC))
        errs++;
    dmas++;

    /* scatter：msg[r][k][i] = out_k[(i + sh) % Zc]（gather 用 (i-sh)，逆映射是 (i+sh)） */
    for (uint32_t e = 0; e < n; e++) {
        uint32_t r = blk_r[b][e], k = blk_k[b][e];
        const uint8_t *rp = rotp[blk_sh[b][e]];
        const int32_t *src = bout[e];
        int32_t *dst = msg[r][k];
        for (uint32_t i = 0; i < ZC; i++)
            dst[i] = src[rp[i]];
    }
    for (uint32_t i = 0; i < ZC; i++)
        hard[i] = bout[25][i];
}

static uint32_t lcg = LCG_SEED;
static int32_t next_llr(void)
{
    lcg = lcg * 1664525u + 1013904223u;
    return (int32_t)((lcg >> 16) & 0x3FFu) - 512;
}

int main(void)
{
    uint32_t *out = RESULT_BASE;
    static int32_t hard[BG2_COLS][ZC] __attribute__((aligned(8)));

    build_tables();

    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t i = 0; i < ZC; i++)
            llr[b][i] = next_llr();

    for (uint32_t r = 0; r < BG2_ROWS; r++)
        for (uint32_t k = 0; k < MAXD; k++)
            for (uint32_t i = 0; i < ZC; i++)
                msg[r][k][i] = (k < bg2_deg[r])
                               ? llr[bg2_col[r][k]][(i + bg2_sh[r][k]) % ZC]
                               : SENTINEL;

    /* 两个页都放哨兵；CN 与 BN 各装一次程序 */
    for (uint32_t i = 0; i < ZC; i++) {
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK, i), (uint32_t)SENTINEL);
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK + 32, i), (uint32_t)SENTINEL);
    }
    /* IMEM 只有一份：CN 与 BN 程序必须按阶段重装（这里先装 CN） */
    if (gpcp_cgra_imem_load(CGRA_BASE, cn_prog, CN_PROG_CYCLES * NPES))
        errs++;

    uint32_t t0 = mtime();
    uint32_t acc_cn = 0, acc_bn = 0, acc_cpu = 0;
    for (uint32_t it = 0; it < ITERS; it++) {
        uint32_t ta = mtime();
        /* ---- CN：42 行，双页 ping-pong（每轮重装 CN 程序，因为上一轮末尾装的是 BN） ---- */
        if (gpcp_cgra_imem_load(CGRA_BASE, cn_prog, CN_PROG_CYCLES * NPES))
            errs++;
        cn_row_in(0, 0);
        if (rt_dma_wait(CGRA_BASE)) errs++;
        for (uint32_t r = 0; r < BG2_ROWS; r++) {
            if (r + 1 < BG2_ROWS) cn_row_in(r + 1, (r + 1) & 1u);
            if (rt_array_run(CGRA_BASE, r & 1u, CN_PROG_CYCLES)) errs++;
            runs++;
            if (r + 1 < BG2_ROWS) { if (rt_dma_wait(CGRA_BASE)) errs++; }
            cn_row_out(r, r & 1u);
            if (rt_dma_wait(CGRA_BASE)) errs++;
        }
        acc_cn += mtime() - ta;
        out[1048] = acc_cn;

        ta = mtime();
        /* 引擎刚写完 res：先整片失效 D$，再让 CPU 读（见上面的注释） */
        rt_dcache_flush();
        reconstruct();
        acc_cpu += mtime() - ta;
        out[1050] = acc_cpu;

        ta = mtime();
        /* ---- BN：52 个变量块（先换装 BN 程序） ---- */
        if (gpcp_cgra_imem_load(CGRA_BASE, bn_prog, BN_PROG_CYCLES * NPES))
            errs++;
        for (uint32_t b = 0; b < BG2_COLS; b++)
            bn_block(b, hard[b]);
        acc_bn += mtime() - ta;
        out[1049] = acc_bn;
    }
    uint32_t cyc = mtime() - t0;

    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t i = 0; i < ZC; i++)
            out[8 + b * ZC + i] = (uint32_t)hard[b][i];

    out[0] = 0xCAFEBABEu;
    out[1] = BG2_COLS;
    out[2] = runs;
    out[3] = dmas;
    out[4] = cyc;
    out[5] = errs;
    out[6] = 0x600Du;
    out[1048] = acc_cn;      /* 分项计时（结果区之后，判据不读） */
    out[1049] = acc_bn;
    out[1050] = acc_cpu;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
