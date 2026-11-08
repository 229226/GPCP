/* -----------------------------------------------------------------------------
 * P1: 真实 BG2 的完整 LDPC 迭代（52 变量块 × ITERS 次）—— 硬件整链，不再是模型
 *
 * 每次迭代：
 *   1) CN：42 个检查节点，M1 运行时双页 ping-pong（cn_prog，172 拍/行）；
 *   2) 重构 + gather 合并（CPU）：m' = sign·mag 按 (i-sh)%Zc 直接落进该边的块缓冲；
 *   3) 每个变量块 b：DMA 进 → BN（bn_prog，155 拍）→ DMA 出 → scatter 回 msg；
 *   4) 最后一次迭代把每块的硬判决（BN 的 bank 25）收进结果区。
 *
 * 判据：tools/check_pdsch_iter.py（同一份 LLR、同一个基图、纯 min-sum、同一 gather/scatter 约定）
 *
 * 数据流（CPU 与引擎的交接都在 RAM 里，所以要处理 D$ 一致性）：
 *   gath —— CPU 写（重构落位）→ 引擎读（BN 搬入）。WT 写穿，RAM 天然是新的；
 *   msg  —— CPU 写（散射）→ 引擎读（CN 搬入）。同上；
 *   res  —— 引擎写（CN 搬出）→ CPU 读（重构）。每轮读之前必须 rt_dcache_flush()；
 *   bout —— 引擎写（BN 搬出）→ CPU 读（散射/硬判决）。每块独占一段 + 每轮刷新。
 *
 * 结果区（0x8001_0000）：[0] marker [1] blocks [2] runs [3] dma [4] cycles [5] errs [6] 0x600D
 *                        [8..] 52 × 20 个硬判决（块优先）
 *                        [1048] CN 累计 [1049] BN 累计 [1050] D$失效+重构 累计（分项计时，判据不读）
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
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
#define SENTINEL_BANK GPCP_LAYOUT_REGION_SENTINEL
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
/* gath / bout 每个变量块各独占一段：
 *   - bout 必须独占（引擎写→CPU 读，同一坑）；
 *   - gath 独占是为了"只写一遍"：每块的零填充槽位和 LLR 槽在启动时就位，
 *     之后每个块只写自己 n 条边（原来每块要重写 ~270 个零 + 20 个 LLR）。 */
#define GATH_BASE  0x8003E000ul   /* 52 x 520 字 = 108 KiB -> 0x80058680 */
#define BOUT_BASE  0x80059000ul   /* 52 x 520 字 = 108 KiB -> 0x80073680 */
#define GSPAD      ((GPCP_LAYOUT_REGION_HARD + 1) * ZC)      /* 每块 26 bank x 20 字（bank 25 不参与搬入） */

static int32_t (*msg)[MAXD][ZC] = (int32_t (*)[MAXD][ZC])MSG_BASE;
static int32_t (*res)[3][ZC] = (int32_t (*)[3][ZC])RES_BASE;
static int32_t (*llr)[ZC] = (int32_t (*)[ZC])LLR_BASE;
static int32_t *g_all = (int32_t *)GATH_BASE;               /* 每块一段：g_all + b*520 */
static int32_t *b_all = (int32_t *)BOUT_BASE;               /* 每块一段：b_all + b*520 */

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
/* 反向查表：(r,k) 属于哪个变量块、在块内的槽号（重构时直接落位到 gather） */
static uint8_t edge_b[BG2_ROWS][MAXD], edge_e[BG2_ROWS][MAXD];

static void build_tables(void)
{
    for (uint32_t b = 0; b < BG2_COLS; b++) {
        uint8_t n = 0;
        for (uint32_t r = 0; r < BG2_ROWS; r++)
            for (uint32_t k = 0; k < bg2_deg[r]; k++)
                if (bg2_col[r][k] == b) {
                    blk_r[b][n] = (uint8_t)r;
                    blk_k[b][n] = (uint8_t)k;
                    blk_sh[b][n] = (uint8_t)(bg2_sh[r][k] % ZC);
                    edge_b[r][k] = (uint8_t)b;
                    edge_e[r][k] = n;
                    n++;
                }
        blk_n[b] = n;
    }
}

static void cn_row_in(uint32_t r, uint32_t page)
{
    if (rt_dma_begin(CGRA_BASE, (uintptr_t)&msg[r][0][0], 0, MAXD * ZC, page, 0, GPCP_LAYOUT_WORDS, ZC))
        errs++;
    dmas++;
}
static void cn_row_out(uint32_t r, uint32_t page)
{
    if (rt_dma_begin(CGRA_BASE, (uintptr_t)&res[r][0][0], (GPCP_LAYOUT_REGION_MIN1 * GPCP_LAYOUT_WORDS), 3 * ZC, page, 1, GPCP_LAYOUT_WORDS, ZC))
        errs++;
    dmas++;
}

/* 重构 + gather 合并：m' = sign(a)·mag 直接写进**该边所属块**的 gather 缓冲，
 * 并按 (i-sh)%Zc 落位（gather 那一整趟就不用再做）。
 *
 * 为什么合并：本项目 CPU 侧的瓶颈是 "WT store 排队 → AXI 单笔 write"（实测
 * 约 20 拍/8 字节）。原来每条边要写两次（先 m' 写回 msg，再 gather 搬一遍），
 * 合并后只写一次；msg 也不必再存 m'（下一轮 CN 读的是 scatter 写回的 BN 输出）。
 *
 * 落位用两段连续写（src 顺序 j=0..Zc-1）：
 *   dst[sh..Zc-1] <- m'(0..Zc-1-sh)，dst[0..sh-1] <- m'(Zc-sh..Zc-1) */
static void reconstruct_gather(void)
{
    for (uint32_t r = 0; r < BG2_ROWS; r++) {
        uint32_t d = bg2_deg[r];
        const int32_t *m1p = res[r][0];
        const int32_t *m2p = res[r][1];
        const int32_t *sgp = res[r][2];
        for (uint32_t k = 0; k < d; k++) {
            uint32_t b = edge_b[r][k], e = edge_e[r][k];
            uint32_t sh = bg2_sh[r][k] % ZC;
            const int32_t *src = msg[r][k];
            int32_t *dst = (int32_t *)(g_all + b * GSPAD) + e * ZC;
            for (uint32_t j = 0; j < ZC - sh; j++) {
                int32_t a = src[j];
                int32_t ab = a < 0 ? -a : a;
                int32_t mag = (ab == m1p[j]) ? m2p[j] : m1p[j];
                dst[sh + j] = ((a < 0) ^ (sgp[j] != 0)) ? -mag : mag;
            }
            for (uint32_t j = 0; j < sh; j++) {
                uint32_t i = ZC - sh + j;
                int32_t a = src[i];
                int32_t ab = a < 0 ? -a : a;
                int32_t mag = (ab == m1p[i]) ? m2p[i] : m1p[i];
                dst[j] = ((a < 0) ^ (sgp[i] != 0)) ? -mag : mag;
            }
        }
    }
}

/* ---- 变量块的两段：搬入 / 搬出+scatter，供流水重叠 ---- */
/* 搬入：banks 0..24（24 个消息槽 + LLR），bank 25 不参与 */
static void bn_dma_in(uint32_t b)
{
    int32_t *gath = g_all + b * GSPAD;
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    if (rt_dma_begin(CGRA_BASE, (uintptr_t)gath, 0, 25 * ZC, 0, 0, GPCP_LAYOUT_WORDS, ZC))
        errs++;
    dmas++;
}

/* 搬出：只搬用得上的 bank 0..n-1（散射）+ bank 25（硬判决，只有最后一轮要） */
static void bn_dma_out(uint32_t b, int want_hard)
{
    uint32_t n = blk_n[b];
    int32_t *bout = b_all + b * GSPAD;
    if (n) {
        if (rt_dma_begin(CGRA_BASE, (uintptr_t)bout, 0, n * ZC, 0, 1, GPCP_LAYOUT_WORDS, ZC))
            errs++;
        dmas++;
    }
    if (want_hard) {
        if (rt_dma_begin(CGRA_BASE, (uintptr_t)(bout + n * ZC),
                         SPAD_IDX(25, 0), ZC, 0, 1, 0, 0))
            errs++;
        dmas++;
    }
}

/* scatter：msg[r][k][i] = out_k[(i+sh)%Zc]，同样两段连续写回：
 *   dst[0..Zc-1-sh] <- src[sh..Zc-1]，dst[Zc-sh..Zc-1] <- src[0..sh-1] */
static void bn_scatter(uint32_t b, int want_hard, int32_t *hard)
{
    uint32_t n = blk_n[b];
    int32_t *bout = b_all + b * GSPAD;
    for (uint32_t e = 0; e < n; e++) {
        uint32_t r = blk_r[b][e], k = blk_k[b][e], sh = blk_sh[b][e];
        const int32_t *src = bout + e * ZC;
        int32_t *dst = msg[r][k];
        for (uint32_t i = 0; i < ZC - sh; i++)
            dst[i] = src[sh + i];
        for (uint32_t i = 0; i < sh; i++)
            dst[ZC - sh + i] = src[i];
    }
    if (want_hard) {
        const int32_t *hb = bout + n * ZC;
        for (uint32_t i = 0; i < ZC; i++)
            hard[i] = hb[i];
    }
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

    /* 每块自己的 gather 缓冲：零槽位（n..23）和 LLR 槽（24）只在这里写一次；
     * 之后每个块只覆盖自己 n 条边所在的槽，所以每轮迭代省掉 ~270 字/块的写。 */
    for (uint32_t b = 0; b < BG2_COLS; b++) {
        int32_t (*gath)[ZC] = (int32_t (*)[ZC])(g_all + b * GSPAD);
        for (uint32_t k = 0; k < 24; k++)
            for (uint32_t i = 0; i < ZC; i++)
                gath[k][i] = 0;
        for (uint32_t i = 0; i < ZC; i++)
            gath[24][i] = llr[b][i];
    }

    /* 两个页都放哨兵；CN 与 BN 各装一次程序 */
    for (uint32_t i = 0; i < ZC; i++) {
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK, i), (uint32_t)SENTINEL);
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK + GPCP_LAYOUT_PAGE_BANKS, i), (uint32_t)SENTINEL);
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
        reconstruct_gather();
        acc_cpu += mtime() - ta;
        out[1050] = acc_cpu;

        ta = mtime();
        /* ---- BN：52 个变量块，两段流水（先换装 BN 程序） ----
         *  阵列(b) 之后发"搬出(b)"；"搬入(b+1)"与 scatter(b) 并行。
         *  单一 outstanding 的引擎同一时刻只跑一个传输，所以搬出完了再发搬入。 */
        if (gpcp_cgra_imem_load(CGRA_BASE, bn_prog, BN_PROG_CYCLES * NPES))
            errs++;
        int want_hard = (it + 1u == ITERS);      /* 硬判决只有最后一轮要搬 */
        bn_dma_in(0);
        if (rt_dma_wait(CGRA_BASE)) errs++;
        for (uint32_t b = 0; b < BG2_COLS; b++) {
            if (rt_array_run(CGRA_BASE, 0, BN_PROG_CYCLES)) errs++;
            runs++;
            bn_dma_out(b, want_hard);
            if (rt_dma_wait(CGRA_BASE)) errs++;
            if (b + 1 < BG2_COLS) bn_dma_in(b + 1);
            bn_scatter(b, want_hard, hard[b]);
            if (b + 1 < BG2_COLS) { if (rt_dma_wait(CGRA_BASE)) errs++; }
        }
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
