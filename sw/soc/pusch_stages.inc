/* -----------------------------------------------------------------------------
 * PUSCH 接收链的「阶段实现」——被两个程序共用：
 *   sw/soc/pusch_bm.c         人写参照基准（run-pusch，含 -DTP / -DHARQ_TX=2 变体）
 *   sw/soc/proto_runner.c     协议运行器（docs/20 阶段 2 迁入，单传输 CP-OFDM）
 *
 * 阶段与 §2 的动词对应：received -> dmrs -> ofdm_demodulate -> estimate
 * -> equalise_demap -> decode。
 *
 * 与 PDSCH 接收链的区别只在 DMRS 种子（38.211 6.4.1.1，n_ID = n_RNTI）与
 * 速率匹配的 k0 由镜像尾字驱动；其余（FFT / LS / LLR / 解扰 / LDPC）同构。
 *
 * 使用前必须 include：gpcp_cgra.h / soc_io.h / gpcp_cgra_rt.h /
 * ldpc_bg2.h / pdsch_{cn,bn}_prog.h（以及 -DTP 时的 pusch_idft_tab.h）。
 * -------------------------------------------------------------------------- */

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)

#define NPES          16
#define RING_DEPTH    1024
#define DESC_BASE     0x80020000ul
#define TD_RAM        0x80400000ul
#define PARAM_BASE    0x80600000ul

#define NSYM          2
#define NRE           240
#define NPILOT        60
#define E_BITS        720
#define NFFT          4096
#define CP_LEN        288
#define BIN0          (NFFT / 2 + 1)
#define SYM_WORDS     (2 * (CP_LEN + NFFT))
#define QPSK          2000
#define H_SCALE       16
#define LLR_SHIFT     8
#define N_RNTI        0x1234u
#define GOLD_NC       1600
#define GOLD_LEN      (E_BITS + GOLD_NC + 31)
#define USABLE        984
#define FILLER0       64
#define FILLER_N      16
#define PUNCT         40
#define NCODED        1040
#define A_TB          80
#define CRC_LEN       24

#define PUSCH_NID         0x1234u   /* n_ID = n_RNTI (simplified) */
#define PUSCH_NSCID       0
#define PUSCH_NSLOT       0
#define PUSCH_L0          0
#define PUSCH_N_DMRS_SYMB NSYM      /* both symbols carry a pilot */
#define QM                2

#ifndef HARQ_TX
#define HARQ_TX       1
#endif
#if HARQ_TX == 2
static const uint32_t rv_k0[HARQ_TX] = { 0u, (uint32_t)(USABLE / 2) };
#else
static const uint32_t rv_k0[HARQ_TX] = { 0u };
#endif

#define ZC            20
#define MAXD          10
#define SENTINEL      32767
#define SENTINEL_BANK GPCP_LAYOUT_REGION_SENTINEL
#define ITERS         8
#define MSG_BASE      0x80030000ul
#define RES_BASE      0x80039000ul
#define LLR_BASE      0x8003C000ul
#define GATH_BASE     0x8003E000ul
#define BOUT_BASE     0x80059000ul
#define GSPAD         ((GPCP_LAYOUT_REGION_HARD + 1) * ZC)

#define GOLD_BASE     0x80380000ul
#define YW_BASE       0x80390000ul
#define YW2_BASE      0x803A0000ul
#define LLRE_BASE     0x80391000ul
#define DEM_BASE      0x80392000ul
#define BUF_BASE      0x80393000ul
#define DEINT_BASE    0x80394000ul

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, dmas, busy;
static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

static uint32_t *gold_x1 = (uint32_t *)GOLD_BASE;
static uint32_t *gold_x2 = (uint32_t *)(GOLD_BASE + 4 * GOLD_LEN);
/* 每个 HARQ 传输一份频域 RE，阶段化后不能在传输间复用同一块 */
static int32_t *yw_tx[2] = { (int32_t *)YW_BASE, (int32_t *)YW2_BASE };
static int32_t *llr_e = (int32_t *)LLRE_BASE;
static int32_t *dem = (int32_t *)DEM_BASE;
static int32_t *rbuf = (int32_t *)BUF_BASE;
static int32_t *llr_i = (int32_t *)DEINT_BASE;
static int32_t dmrs_re[NSYM][NPILOT], dmrs_im[NSYM][NPILOT];

static int32_t (*msg)[MAXD][ZC] = (int32_t (*)[MAXD][ZC])MSG_BASE;
static int32_t (*res_r)[3][ZC] = (int32_t (*)[3][ZC])RES_BASE;
static int32_t (*llr)[ZC] = (int32_t (*)[ZC])LLR_BASE;
static int32_t *g_all = (int32_t *)GATH_BASE;
static int32_t *b_all = (int32_t *)BOUT_BASE;

/* ------------------------------------------------------------ 结果状态 */
static uint32_t us_crc_ok, us_k0;
static int32_t us_hre[HARQ_TX][NSYM], us_him[HARQ_TX][NSYM];
static int32_t us_raw0[8], us_descr0[8];
static int32_t us_dbg_spec_re, us_dbg_spec_im, us_dbg_yw_re, us_dbg_yw_im;
static int32_t us_hard[BG2_COLS][ZC] __attribute__((aligned(8)));

#if defined(TP) && !defined(PROTO_CPU_ONLY)
static void dft_bluestein_setup(void);      /* dft_bluestein.inc，下方 include */
#endif
/* 激励是裸的 2 符号时域样本 + 尾部一个 rv 字（没有 magic 头）。 */
static int us_setup(const uint32_t *par)
{
    (void)par;
    errs = runs = dmas = busy = 0;
    us_crc_ok = us_k0 = 0;
    us_dbg_spec_re = us_dbg_spec_im = us_dbg_yw_re = us_dbg_yw_im = 0;
    for (int i = 0; i < 8; i++) { us_raw0[i] = 0; us_descr0[i] = 0; }
#if defined(TP) && !defined(PROTO_CPU_ONLY)
    dft_bluestein_setup();      /* 360 点 Bluestein 的 B 表/比例，计在 setup 之外 */
#endif
    return 0;
}

#include "ssb_fft.inc"          /* fft4096() + rev4() */
#ifdef PROTO_CPU_ONLY
#include "fft_cpu.inc"
#endif
#if defined(TP) && !defined(PROTO_CPU_ONLY)
#define DFT_N 360
#include "dft_bluestein.inc"    /* 360 点 IDFT：Bluestein，复用 1024 点 FFT 引擎 */
#endif

#ifdef TP
/* 360-point IDFT of the equalised data REs (DFT-s-OFDM deprecoding).
   Q20 cosine table; sin(x) = cos(x - 90 deg).  Integer only. */
static void __attribute__((unused))
idft360(const int32_t *ar, const int32_t *ai, int32_t *out_iq)
{
    for (int m = 0; m < IDFT_N; m++) {
        int64_t sr = 0, si = 0;
        uint32_t idx = 0;            /* (m*k) % IDFT_N，增量递推 */
        uint32_t sidx = 270;         /* (idx + 270) % IDFT_N */
        for (int k = 0; k < IDFT_N; k++) {
            int64_t c = idft_cos[idx];
            int64_t s = idft_cos[sidx];
            sr += (int64_t)ar[k] * c - (int64_t)ai[k] * s;
            si += (int64_t)ar[k] * s + (int64_t)ai[k] * c;
            idx += (uint32_t)m;
            if (idx >= (uint32_t)IDFT_N)
                idx -= (uint32_t)IDFT_N;
            sidx = idx + 270u;
            if (sidx >= (uint32_t)IDFT_N)
                sidx -= (uint32_t)IDFT_N;
        }
        out_iq[2 * m + 0] = (int32_t)(sr / ((int64_t)IDFT_N * IDFT_SCALE));
        out_iq[2 * m + 1] = (int32_t)(si / ((int64_t)IDFT_N * IDFT_SCALE));
    }
}
#endif

/* ------------------------------------------------------------ 38.211 Gold */
static void gold_init(uint32_t c_init)
{
    uint32_t n = E_BITS + GOLD_NC + 31u;
    for (uint32_t i = 0; i < n; i++) { gold_x1[i] = 0; gold_x2[i] = 0; }
    gold_x1[0] = 1;
    for (uint32_t i = 0; i < 31u; i++) gold_x2[i] = (c_init >> i) & 1u;
    for (uint32_t i = 0; i + 31u < GOLD_LEN; i++) {
        gold_x1[i + 31] = gold_x1[i + 3] ^ gold_x1[i];
        gold_x2[i + 31] = gold_x2[i + 3] ^ gold_x2[i + 2]
                        ^ gold_x2[i + 1] ^ gold_x2[i];
    }
}

static uint32_t gold_bit(uint32_t n)
{
    return gold_x1[GOLD_NC + n] ^ gold_x2[GOLD_NC + n];
}

/* 38.211 6.4.1.1 PUSCH DMRS scrambling init. */
static uint32_t dmrs_cinit(int s)
{
    uint64_t l = (uint64_t)(PUSCH_L0 + s);
    uint64_t v = ((uint64_t)1 << 17)
                 * (uint64_t)(PUSCH_N_DMRS_SYMB * PUSCH_NSLOT + l + 1)
                 * (uint64_t)(2 * PUSCH_NID + 1)
                 + (uint64_t)(2 * PUSCH_NID + PUSCH_NSCID);
    return (uint32_t)(v & 0x7FFFFFFFu);
}

static void build_dmrs(void)
{
    for (int s = 0; s < NSYM; s++) {
        gold_init(dmrs_cinit(s));
        for (int p = 0; p < NPILOT; p++) {
            uint32_t b0 = gold_bit((uint32_t)(2 * p));
            uint32_t b1 = gold_bit((uint32_t)(2 * p + 1));
            dmrs_re[s][p] = (int32_t)(1 - 2 * (int)b0) * QPSK;
            dmrs_im[s][p] = (int32_t)(1 - 2 * (int)b1) * QPSK;
        }
    }
}

/* ------------------------------------------------------------ CRC24A (38.212 7.1) */
static void crc24a(const uint8_t *bits, int n, uint8_t *out)
{
    const uint32_t poly = 0x864CFBu;
    uint32_t reg = 0;
    for (int i = 0; i < n; i++) {
        reg ^= (uint32_t)(bits[i] & 1u) << 23;
        for (int j = 0; j < 24; j++) {
            if (reg & 0x800000u) reg = ((reg << 1) ^ poly) & 0xFFFFFFu;
            else                 reg = (reg << 1) & 0xFFFFFFu;
        }
    }
    for (int i = 0; i < 24; i++) out[i] = (uint8_t)((reg >> (23 - i)) & 1u);
}

static int32_t floor_div(int64_t a, int64_t b)
{
    int64_t q = a / b;
    if (a % b != 0 && ((a < 0) != (b < 0))) q--;
    return (int32_t)q;
}

static uint32_t re_bin(uint32_t r) { return (uint32_t)BIN0 + r; }

/* ------------------------------------------------------------ LDPC engine */
static uint8_t blk_n[BG2_COLS];
static uint8_t blk_r[BG2_COLS][24], blk_k[BG2_COLS][24], blk_sh[BG2_COLS][24];
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
    if (rt_dma_begin(CGRA_BASE, (uintptr_t)&msg[r][0][0], 0, MAXD * ZC, page, 0,
                     GPCP_LAYOUT_WORDS, ZC))
        errs++;
    dmas++;
}

static void cn_row_out(uint32_t r, uint32_t page)
{
    if (rt_dma_begin(CGRA_BASE, (uintptr_t)&res_r[r][0][0],
                     GPCP_LAYOUT_REGION_MIN1 * GPCP_LAYOUT_WORDS, 3 * ZC, page, 1,
                     GPCP_LAYOUT_WORDS, ZC))
        errs++;
    dmas++;
}

static void reconstruct_gather(void)
{
    for (uint32_t r = 0; r < BG2_ROWS; r++) {
        uint32_t d = bg2_deg[r];
        const int32_t *m1p = res_r[r][0];
        const int32_t *m2p = res_r[r][1];
        const int32_t *sgp = res_r[r][2];
        for (uint32_t k = 0; k < d; k++) {
            uint32_t b = edge_b[r][k], e = edge_e[r][k];
            uint32_t sh = blk_sh[b][e];
            const int32_t *src = msg[r][k];
            int32_t *dst = g_all + b * GSPAD + e * ZC;
            for (uint32_t j = 0; j < ZC - sh; j++) {
                int32_t a = src[j];
                int32_t am = a >> 31;
                int32_t ab = (a ^ am) - am;
                int32_t mag = (ab == m1p[j]) ? m2p[j] : m1p[j];
                int32_t nm = am ^ sgp[j];
                dst[sh + j] = (mag ^ nm) - nm;
            }
            for (uint32_t j = 0; j < sh; j++) {
                uint32_t i = ZC - sh + j;
                int32_t a = src[i];
                int32_t am = a >> 31;
                int32_t ab = (a ^ am) - am;
                int32_t mag = (ab == m1p[i]) ? m2p[i] : m1p[i];
                int32_t nm = am ^ sgp[i];
                dst[j] = (mag ^ nm) - nm;
            }
        }
    }
}

static void bn_dma_in(uint32_t b)
{
    int32_t *gath = g_all + b * GSPAD;
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    if (rt_dma_begin(CGRA_BASE, (uintptr_t)gath, 0, 25 * ZC, 0, 0, GPCP_LAYOUT_WORDS, ZC))
        errs++;
    dmas++;
}

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
                         GPCP_SPAD_IDX(GPCP_LAYOUT_REGION_HARD, 0), ZC, 0, 1, 0, 0))
            errs++;
        dmas++;
    }
}

static void bn_scatter(uint32_t b, int want_hard, int32_t *hard)
{
    uint32_t n = blk_n[b];
    int32_t *bout = b_all + b * GSPAD;
    for (uint32_t e = 0; e < n; e++) {
        uint32_t r = blk_r[b][e], k = blk_k[b][e], sh = blk_sh[b][e];
        const int32_t *src = bout + e * ZC;
        int32_t *dst = msg[r][k];
        for (uint32_t i = 0; i < ZC - sh; i++) dst[i] = src[sh + i];
        for (uint32_t i = 0; i < sh; i++)      dst[ZC - sh + i] = src[i];
    }
    if (want_hard) {
        const int32_t *hb = bout + n * ZC;
        for (uint32_t i = 0; i < ZC; i++) hard[i] = hb[i];
    }
}

/* --------------------------------------------------------- 阶段 0：接收 */
static int us_received(void) { return 0; }

/* ------------------------------------- 阶段 1：DMRS（38.211 6.4.1.1） */
static int us_dmrs(void)
{
    build_dmrs();
    return 0;
}

/* ------------ 阶段 2：OFDM 解调 [array]（每个传输的每个符号一次 FFT） */
static int us_ofdm_demodulate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    for (int tx = 0; tx < HARQ_TX; tx++) {
        int32_t *y = yw_tx[tx];
        for (int s = 0; s < NSYM; s++) {
            int32_t *sp = td + ((size_t)tx * NSYM + s) * SYM_WORDS + 2 * CP_LEN;
#ifdef PROTO_CPU_ONLY
            fft4096_cpu(sp);
#else
            fft4096(sp);
#endif
            for (int r = 0; r < NRE; r++) {
                uint32_t idx = rev4(re_bin((uint32_t)r));
                int32_t vr = sp[2 * idx] << 4;
                int32_t vi = sp[2 * idx + 1] << 4;
                y[(s * NRE + r) * 2 + 0] = vr;
                y[(s * NRE + r) * 2 + 1] = vi;
                if (tx == 0 && s == 0 && r == 0) {
                    us_dbg_spec_re = sp[2 * idx];
                    us_dbg_spec_im = sp[2 * idx + 1];
                    us_dbg_yw_re = vr;
                    us_dbg_yw_im = vi;
                }
            }
        }
    }
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    return 0;
}

/* ------------------------- 阶段 3：信道估计（每符号 60 个导频的 LS） */
static int us_estimate(void)
{
    int64_t den = (int64_t)NPILOT * 2 * QPSK * QPSK / H_SCALE;
    for (int tx = 0; tx < HARQ_TX; tx++) {
        int32_t *y = yw_tx[tx];
        for (int s = 0; s < NSYM; s++) {
            int64_t accr = 0, acci = 0;
            int p = 0;
            for (int r = 0; r < NRE; r += 4) {
                int32_t yr = y[(s * NRE + r) * 2 + 0];
                int32_t yi = y[(s * NRE + r) * 2 + 1];
                int32_t dr = dmrs_re[s][p], di = dmrs_im[s][p];
                accr += (int64_t)yr * dr + (int64_t)yi * di;   /* y*conj(DMRS) */
                acci += (int64_t)yi * dr - (int64_t)yr * di;
                p++;
            }
            us_hre[tx][s] = floor_div(accr, den);
            us_him[tx][s] = floor_div(acci, den);
        }
    }
    return 0;
}

/* ---- 阶段 4：均衡 + 软解调（LLR → 解交织 → 解扰 → Chase 合并 → 解速率匹配） */
#if defined(TP) && HARQ_TX == 1
/* 节点粒度拆分：均衡 / IDFT / 解调 三段各自独立，`where` 才能各自唯一。
 * 只覆盖 TP + 单次传输；其余配置保留下面的合并实现（见 #else）。 */
static int32_t us_eq_re[E_BITS / 2], us_eq_im[E_BITS / 2];

static int us_equalise(void)
{
    int32_t *y = yw_tx[0];
    int32_t *hre = us_hre[0], *him = us_him[0];
    int m = 0;

    for (int s = 0; s < NSYM; s++)
        for (int r = 0; r < NRE; r++) {
            if (r % 4 == 0) continue;          /* DMRS RE */
            int32_t yr = y[(s * NRE + r) * 2 + 0];
            int32_t yi = y[(s * NRE + r) * 2 + 1];
            int64_t er = (int64_t)yr * hre[s] + (int64_t)yi * him[s];
            int64_t ei = (int64_t)yi * hre[s] - (int64_t)yr * him[s];
            us_eq_re[m] = (int32_t)er;
            us_eq_im[m] = (int32_t)ei;
            m++;
        }
    return 0;
}

static int us_transform(void)
{
#ifdef PROTO_CPU_ONLY
    idft360(us_eq_re, us_eq_im, llr_e);
#else
    dft_bluestein_inv(us_eq_re, us_eq_im, llr_e);
#endif
    return 0;
}

static int us_demap(void)
{
    for (int q = 0; q < USABLE; q++)
        rbuf[q] = 0;

    for (int q = 0; q < 8; q++) us_raw0[q] = llr_e[q];

    /* 38.212 5.4.2.2 de-interleave (Qm=2, 360 rows) */
    {
        int rows = E_BITS / QM;
        for (int q = 0; q < rows; q++)
            for (int c = 0; c < QM; c++)
                llr_i[c * rows + q] = llr_e[QM * q + c];
    }

    /* descramble (38.211 7.3.3.1, c_init = n_RNTI << 15) */
    gold_init(N_RNTI << 15);
    for (int q = 0; q < E_BITS; q++)
        if (gold_bit((uint32_t)q)) llr_i[q] = -llr_i[q];
    for (int q = 0; q < 8; q++) us_descr0[q] = llr_i[q];

    /* Chase combining in circular-buffer coordinates */
    {
        uint32_t rv_word = *(volatile uint32_t *)(TD_RAM
                           + (uint32_t)NSYM * SYM_WORDS * 4u);
        us_k0 = rv_word * (USABLE / 4u);
        for (int q = 0; q < E_BITS; q++)
            rbuf[(us_k0 + (uint32_t)q) % USABLE] += llr_i[q];
    }

    /* rate de-match: combined LLRs -> 1040 coded LLRs */
    for (int q = 0; q < PUNCT; q++) dem[q] = 0;
    {
        int u = 0;
        for (int q = 0; q < NCODED - PUNCT; q++) {
            if (q >= FILLER0 && q < FILLER0 + FILLER_N) dem[PUNCT + q] = 0;
            else                                        dem[PUNCT + q] = rbuf[u++];
        }
    }

    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t q = 0; q < ZC; q++)
            llr[b][q] = dem[b * ZC + q] >> LLR_SHIFT;
    return 0;
}

/* 兼容入口：老的单片调用路径（bm 程序）仍可用。 */
static int us_equalise_demap(void)
{
    us_equalise();
    us_transform();
    return us_demap();
}
#elif HARQ_TX == 1
/* 节点粒度（非 TP 单次传输）：匹配滤波与解调分离。 */
static int us_equalise(void)
{
    int32_t *y = yw_tx[0];
    int32_t *hre = us_hre[0], *him = us_him[0];
    int m = 0;

    for (int i = 0; i < USABLE; i++)
        rbuf[i] = 0;

    for (int s = 0; s < NSYM; s++)
        for (int r = 0; r < NRE; r++) {
            int32_t yr, yi;
            int64_t er, ei;
            if (r % 4 == 0) continue;          /* DMRS RE */
            yr = y[(s * NRE + r) * 2 + 0];
            yi = y[(s * NRE + r) * 2 + 1];
            er = (int64_t)yr * hre[s] + (int64_t)yi * him[s];
            ei = (int64_t)yi * hre[s] - (int64_t)yr * him[s];
            llr_e[2 * m + 0] = (int32_t)er;
            llr_e[2 * m + 1] = (int32_t)ei;
            m++;
        }
    for (int i = 0; i < 8; i++) us_raw0[i] = llr_e[i];
    return 0;
}

static int us_demap(void)
{
    {
        int rows = E_BITS / QM;
        for (int i = 0; i < rows; i++)
            for (int j = 0; j < QM; j++)
                llr_i[j * rows + i] = llr_e[QM * i + j];
    }

    gold_init(N_RNTI << 15);
    for (int k = 0; k < E_BITS; k++)
        if (gold_bit((uint32_t)k)) llr_i[k] = -llr_i[k];
    for (int i = 0; i < 8; i++) us_descr0[i] = llr_i[i];

    {
        uint32_t rv_word = *(volatile uint32_t *)(TD_RAM
                           + (uint32_t)NSYM * SYM_WORDS * 4u);
        us_k0 = rv_word * (USABLE / 4u);
        for (int k = 0; k < E_BITS; k++)
            rbuf[(us_k0 + (uint32_t)k) % USABLE] += llr_i[k];
    }

    for (int j = 0; j < PUNCT; j++) dem[j] = 0;
    {
        int u = 0;
        for (int i = 0; i < NCODED - PUNCT; i++) {
            if (i >= FILLER0 && i < FILLER0 + FILLER_N) dem[PUNCT + i] = 0;
            else                                        dem[PUNCT + i] = rbuf[u++];
        }
    }

    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t i = 0; i < ZC; i++)
            llr[b][i] = dem[b * ZC + i] >> LLR_SHIFT;
    return 0;
}

/* 兼容入口：老的单片调用路径仍可用。 */
static int us_equalise_demap(void)
{
    us_equalise();
    return us_demap();
}
#else
static int us_equalise_demap(void)
{
    for (int i = 0; i < USABLE; i++)
        rbuf[i] = 0;

    for (int tx = 0; tx < HARQ_TX; tx++) {
        int32_t *y = yw_tx[tx];
        int32_t *hre = us_hre[tx], *him = us_him[tx];

#ifdef TP
        /* DFT-s-OFDM: matched-filter every data RE into a flat vector, then
           one 360-point IDFT recovers the QAM symbols (nr_pusch.idft). */
        {
            static int32_t ar[E_BITS / 2], ai[E_BITS / 2];
            int m = 0;
            for (int s = 0; s < NSYM; s++)
                for (int r = 0; r < NRE; r++) {
                    if (r % 4 == 0) continue;      /* DMRS RE */
                    int32_t yr = y[(s * NRE + r) * 2 + 0];
                    int32_t yi = y[(s * NRE + r) * 2 + 1];
                    int64_t er = (int64_t)yr * hre[s] + (int64_t)yi * him[s];
                    int64_t ei = (int64_t)yi * hre[s] - (int64_t)yr * him[s];
                    ar[m] = (int32_t)er;
                    ai[m] = (int32_t)ei;
                    m++;
                }
#ifdef PROTO_CPU_ONLY
            idft360(ar, ai, llr_e);
#else
            dft_bluestein_inv(ar, ai, llr_e);
#endif
        }
#else
        {
            int m = 0;
            for (int s = 0; s < NSYM; s++)
                for (int r = 0; r < NRE; r++) {
                    if (r % 4 == 0) continue;      /* DMRS RE */
                    int32_t yr = y[(s * NRE + r) * 2 + 0];
                    int32_t yi = y[(s * NRE + r) * 2 + 1];
                    int64_t er = (int64_t)yr * hre[s] + (int64_t)yi * him[s];
                    int64_t ei = (int64_t)yi * hre[s] - (int64_t)yr * him[s];
                    llr_e[2 * m + 0] = (int32_t)er;
                    llr_e[2 * m + 1] = (int32_t)ei;
                    m++;
                }
        }
#endif
        if (tx == 0)
            for (int i = 0; i < 8; i++) us_raw0[i] = llr_e[i];

        /* 38.212 5.4.2.2 de-interleave (Qm=2, 360 rows) */
        {
            int rows = E_BITS / QM;
            for (int i = 0; i < rows; i++)
                for (int j = 0; j < QM; j++)
                    llr_i[j * rows + i] = llr_e[QM * i + j];
        }

        /* descramble (38.211 7.3.3.1, c_init = n_RNTI << 15) */
        gold_init(N_RNTI << 15);
        for (int k = 0; k < E_BITS; k++)
            if (gold_bit((uint32_t)k)) llr_i[k] = -llr_i[k];
        if (tx == 0)
            for (int i = 0; i < 8; i++) us_descr0[i] = llr_i[i];

        /* Chase combining in circular-buffer coordinates */
#if HARQ_TX == 1
        {
            uint32_t rv_word = *(volatile uint32_t *)(TD_RAM
                               + (uint32_t)NSYM * SYM_WORDS * 4u);
            us_k0 = rv_word * (USABLE / 4u);
            for (int k = 0; k < E_BITS; k++)
                rbuf[(us_k0 + (uint32_t)k) % USABLE] += llr_i[k];
        }
#else
        for (int k = 0; k < E_BITS; k++)
            rbuf[(rv_k0[tx] + (uint32_t)k) % USABLE] += llr_i[k];
#endif
    }

    /* rate de-match: combined LLRs -> 1040 coded LLRs */
    for (int j = 0; j < PUNCT; j++) dem[j] = 0;
    {
        int u = 0;
        for (int i = 0; i < NCODED - PUNCT; i++) {
            if (i >= FILLER0 && i < FILLER0 + FILLER_N) dem[PUNCT + i] = 0;
            else                                        dem[PUNCT + i] = rbuf[u++];
        }
    }

    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t i = 0; i < ZC; i++)
            llr[b][i] = dem[b * ZC + i] >> LLR_SHIFT;
    return 0;
}
#endif

/* ---------------- 阶段 5：LDPC 译码 [array]（8 次 min-sum）+ CRC24A */
static int us_decode(void)
{
    uint8_t bits[A_TB + CRC_LEN], crc[CRC_LEN];
    int crc_ok = 1;

    build_tables();
    for (uint32_t r = 0; r < BG2_ROWS; r++)
        for (uint32_t k = 0; k < MAXD; k++)
            for (uint32_t i = 0; i < ZC; i++)
                msg[r][k][i] = (k < bg2_deg[r])
                               ? llr[bg2_col[r][k]][(i + bg2_sh[r][k]) % ZC]
                               : SENTINEL;
    for (uint32_t b = 0; b < BG2_COLS; b++) {
        int32_t (*gath)[ZC] = (int32_t (*)[ZC])(g_all + b * GSPAD);
        for (uint32_t k = 0; k < 24; k++)
            for (uint32_t i = 0; i < ZC; i++) gath[k][i] = 0;
        for (uint32_t i = 0; i < ZC; i++) gath[24][i] = llr[b][i];
    }
#ifdef PROTO_CPU_ONLY
    /* 对照路径：同一 min-sum / BN 公式全在 CVA6 上跑，不碰阵列 */
    for (uint32_t it = 0; it < ITERS; it++) {
        for (uint32_t r = 0; r < BG2_ROWS; r++) {
            for (uint32_t i = 0; i < ZC; i++) {
                int32_t m1 = SENTINEL, m2 = SENTINEL, sg = 0;
                for (uint32_t p = 0; p < MAXD / 2; p++) {
                    int32_t a = msg[r][2 * p][i], b = msg[r][2 * p + 1][i];
                    int32_t al = a >> 31, bl = b >> 31;
                    int32_t aa = (a ^ al) - al, ba = (b ^ bl) - bl;
                    int32_t lo = aa < ba ? aa : ba, hi = aa > ba ? aa : ba;
                    int32_t t2 = m2 < hi ? m2 : hi;
                    int32_t t1 = m1 > lo ? m1 : lo;
                    m2 = t1 < t2 ? t1 : t2;
                    m1 = m1 < lo ? m1 : lo;
                    sg ^= al ^ bl;
                }
                res_r[r][0][i] = m1;
                res_r[r][1][i] = m2;
                res_r[r][2][i] = sg;
            }
        }
        reconstruct_gather();

        {
            int want_hard = (it + 1u == ITERS);
            for (uint32_t b = 0; b < BG2_COLS; b++) {
                uint32_t n = blk_n[b];
                int32_t (*gath)[ZC] = (int32_t (*)[ZC])(g_all + b * GSPAD);
                int32_t *bout = b_all + b * GSPAD;
                for (uint32_t i = 0; i < ZC; i++) {
                    int32_t v = llr[b][i];
                    for (uint32_t k = 0; k < 24; k++)
                        v += gath[k][i];
                    for (uint32_t k = 0; k < n; k++)
                        bout[k * ZC + i] = v - gath[k][i];
                    if (want_hard)
                        bout[n * ZC + i] = v < 0 ? -1 : 0;
                }
                bn_scatter(b, want_hard, us_hard[b]);
            }
        }
    }
#else
    for (uint32_t i = 0; i < ZC; i++) {
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK, i), (uint32_t)SENTINEL);
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK + GPCP_LAYOUT_PAGE_BANKS, i),
               (uint32_t)SENTINEL);
    }
    if (gpcp_cgra_imem_load(CGRA_BASE, cn_prog, CN_PROG_CYCLES * NPES))
        errs++;

    for (uint32_t it = 0; it < ITERS; it++) {
        if (gpcp_cgra_imem_load(CGRA_BASE, cn_prog, CN_PROG_CYCLES * NPES))
            errs++;
        cn_row_in(0, 0);
        if (rt_dma_wait(CGRA_BASE)) errs++;
        for (uint32_t r = 0; r < BG2_ROWS; r++) {
            if (r + 1 < BG2_ROWS) cn_row_in(r + 1, (r + 1) & 1u);
            if (rt_array_run(CGRA_BASE, r & 1u, CN_PROG_CYCLES)) errs++;
            runs++;
            busy += CN_PROG_CYCLES;
            if (r + 1 < BG2_ROWS) { if (rt_dma_wait(CGRA_BASE)) errs++; }
            cn_row_out(r, r & 1u);
            if (rt_dma_wait(CGRA_BASE)) errs++;
        }
        rt_dcache_flush();
        reconstruct_gather();

        if (gpcp_cgra_imem_load(CGRA_BASE, bn_prog, BN_PROG_CYCLES * NPES))
            errs++;
        int want_hard = (it + 1u == ITERS);
        bn_dma_in(0);
        if (rt_dma_wait(CGRA_BASE)) errs++;
        for (uint32_t b = 0; b < BG2_COLS; b++) {
            if (rt_array_run(CGRA_BASE, 0, BN_PROG_CYCLES)) errs++;
            runs++;
            busy += BN_PROG_CYCLES;
            bn_dma_out(b, want_hard);
            if (rt_dma_wait(CGRA_BASE)) errs++;
            if (b + 1 < BG2_COLS) bn_dma_in(b + 1);
            bn_scatter(b, want_hard, us_hard[b]);
            if (b + 1 < BG2_COLS) { if (rt_dma_wait(CGRA_BASE)) errs++; }
        }
    }
#endif

    for (int i = 0; i < A_TB + CRC_LEN; i++)
        bits[i] = (uint8_t)(us_hard[i / ZC][i % ZC] < 0 ? 1 : 0);
    crc24a(bits, A_TB, crc);
    for (int i = 0; i < CRC_LEN; i++)
        if (crc[i] != bits[A_TB + i]) crc_ok = 0;
    us_crc_ok = (uint32_t)crc_ok;
    return 0;
}
