/* -----------------------------------------------------------------------------
 * G2 / B5: PDSCH self-loopback in ONE SoC program (docs/19 G2 验收的后半).
 *
 *   params (0x8060_0000, nr_dl_mod.dump_td: magic, nvar, a_tb, tb_words, TB[3],
 *           pad, variants{qm, rv, rnti})
 *     -> B2 transmit chain: TB -> CRC24A -> LDPC BG2/Zc20 -> rate match (rv=0)
 *        -> scramble (c_init = n_RNTI<<15) -> bit interleave (Qm=2) -> QPSK
 *     -> 38.211 7.4.1.1 Gold DMRS (c_init = 0x120002 / 0x180002)
 *     -> 2 OFDM symbols: 240 REs each, DMRS every 4th RE (60 pilots/180 data)
 *        -> conjugate spectrum -> CGRA IFFT (fft4096) + CP -> TD at 0x8040_0000
 *     -> A4/B4 receive chain: FFT -> RE -> per-symbol LS -> equalise -> 720 LLR
 *        -> de-interleave -> descramble -> rate de-match -> 8 min-sum LDPC
 *        iterations on the CGRA -> CRC24A -> 80-bit TB
 *
 * Nothing is loaded from the host except the stimulus (TB + rv + RNTI): the
 * waveform, the DMRS and the whole receive path are produced on this SoC.
 *
 * Scale chain (same numbers as run-pdcch-loop / run-pdcch):
 *   grid amplitude 2000 -> TD = conj(IFFT_hw) * 256 -> RX FFT gives RE/16,
 *   RE << 4 -> |LLR| ~ 2000; LS over 60 pilots gives h = 16 = H_SCALE, so the
 *   equalised LLR is ~32000 and the >>8 scale puts it at ~125 (well under
 *   SENTINEL=32767).
 *
 * Result area (0x8007_0000; .bss 越过 0x8001_0000，按 run-pusch 的先例上移):
 *   [0] marker 0xCAFEBD51  [1] errors  [2] cycles  [3] array runs  [4] DMAs
 *   [5] CRC24A ok  [6] 0x600D  [7] TB bits decoded (80/80)
 *   [8..1047] 1040 LDPC hard decisions (0/1, block-major)
 *   [1048..1051] channel estimate hre/him per symbol
 *   [1052..1059] first 8 raw LLRs       [1060..1067] first 8 descrambled LLRs
 *   [1081] DMRS c_init sym0  [1082] DMRS c_init sym1  [1083] rv transmitted
 *   [1084] Qm  [1085] QAM amplitude  [1086] E/Qm  [1087] TB mismatch count
 *   [1090..1129] transmitted TB bits   [1130..1169] decoded TB bits
 *   [1170] max |symbol|  [1171] constellation REs placed  [1172] zeros check
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"
#include "../include/ldpc_bg2.h"
#include "../include/ldpc_enc_tables.h"
#include "ldpc_enc.inc"
#include "pdsch_cn_prog.h"
#include "pdsch_bn_prog.h"

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)

/* ---- ssb_fft.inc prerequisites ---- */
#define NPES          16
#define RING_DEPTH    1024
#define DESC_BASE     0x80020000ul
#define TD_RAM        0x80400000ul
#define FREQ_BASE     0x803A0000ul        /* IFFT 工作区/频域格点 (2*4096 words) */
#define PARAM_BASE    0x80600000ul

/* ---- shared front-end parameters (must match pdsch_full_bm.c) ---- */
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
#define QM            2
#define GOLD_NC       1600
#define GOLD_LEN      (E_BITS + GOLD_NC + 31)
#define USABLE        984
#define FILLER0       64
#define FILLER_N      16
#define PUNCT         40
#define NCODED        1040
#define A_TB          80
#define CRC_LEN       24
#define CRC_POLY      0x864CFBu
#define KB            6
#define RV_STEP       (USABLE / 4)
#define MAX_SYM       (E_BITS / QM)       /* 360 QPSK symbols */

/* 38.211 7.4.1.1 DMRS */
#define DMRS_NID      1
#define DMRS_NSCID    0
#define DMRS_NSLOT    0
#define DMRS_L0       2
#define DMRS_NSYMB    14
#define DMRS_NCP      0
#define TD_MAGIC      0x1D10D001ul

/* ---- LDPC engine (same addresses as run-pdsch-iter / run-pdsch-full) ---- */
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

/* scratch above the linker region, clear of TD/twiddle */
#define GOLD_BASE     0x80380000ul
#define YW_BASE       0x80390000ul
#define LLRE_BASE     0x80391000ul
#define DEM_BASE      0x80392000ul
#define BUF_BASE      0x80393000ul
#define DEINT_BASE    0x80394000ul

static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;
static uint32_t *gold_x1 = (uint32_t *)GOLD_BASE;
static uint32_t *gold_x2 = (uint32_t *)(GOLD_BASE + 4 * GOLD_LEN);
static int32_t *yw = (int32_t *)YW_BASE;
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

/* TX working vectors (small enough for .bss, unlike the 32 KB FFT buffers) */
static uint8_t tb_bits[A_TB];
static uint8_t info_bits[KB * ZC];
static uint8_t cw_bits[NCODED];
static uint8_t used_bits[USABLE];
static uint8_t e_bits[E_BITS];
static uint8_t e_sc[E_BITS];
static uint8_t f_bits[E_BITS];
static int32_t sym[2 * MAX_SYM];

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, dmas;

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

#include "ssb_fft.inc"          /* fft4096() + rev4() */

/* 写完一大片 RAM 交给引擎读：fence + 读回最后一个字（WT 写缓冲） */
static void drain_stores(const int32_t *p, uint32_t last)
{
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)*(volatile const int32_t *)&p[last];
}

/* ------------------------------------------------------------ 38.211 Gold */
static void gold_init(uint32_t c_init)
{
    uint32_t n = E_BITS + GOLD_NC;
    for (uint32_t i = 0; i < n + 31u; i++) { gold_x1[i] = 0; gold_x2[i] = 0; }
    gold_x1[0] = 1;
    for (uint32_t i = 0; i < 31u; i++) gold_x2[i] = (c_init >> i) & 1u;
    for (uint32_t i = 0; i < n; i++) {
        gold_x1[i + 31] = gold_x1[i + 3] ^ gold_x1[i];
        gold_x2[i + 31] = gold_x2[i + 3] ^ gold_x2[i + 2] ^ gold_x2[i + 1] ^ gold_x2[i];
    }
}

static uint32_t gold_bit(uint32_t n)
{
    return gold_x1[GOLD_NC + n] ^ gold_x2[GOLD_NC + n];
}

static uint32_t dmrs_cinit(int s)
{
    uint64_t l = (uint64_t)(DMRS_L0 + s);
    uint64_t v = ((uint64_t)1 << 17)
                 * (uint64_t)(DMRS_NSYMB * DMRS_NSLOT + l + 1)
                 * (uint64_t)(2 * DMRS_NID + DMRS_NSCID + 1)
                 + (uint64_t)(2 * DMRS_NID + DMRS_NCP);
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
    uint32_t reg = 0;
    for (int i = 0; i < n; i++) {
        reg ^= (uint32_t)(bits[i] & 1u) << 23;
        for (int j = 0; j < 24; j++)
            reg = (reg & 0x800000u) ? (((reg << 1) ^ CRC_POLY) & 0xFFFFFFu)
                                    : ((reg << 1) & 0xFFFFFFu);
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
static uint8_t edge_s[BG2_ROWS][MAXD];
static uint16_t edge_off[BG2_ROWS][MAXD];

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
                    edge_s[r][k] = (uint8_t)(bg2_sh[r][k] % ZC);
                    edge_off[r][k] = (uint16_t)(b * GSPAD + n * ZC);
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

/* 重构 + gather 合并（与 run-pdsch-iter 相同：符号用 0/-1 掩码，落位到块缓冲） */
static void reconstruct_gather(void)
{
    for (uint32_t r = 0; r < BG2_ROWS; r++) {
        uint32_t d = bg2_deg[r];
        const int32_t *m1p = res_r[r][0];
        const int32_t *m2p = res_r[r][1];
        const int32_t *sgp = res_r[r][2];
        for (uint32_t k = 0; k < d; k++) {
            uint32_t sh = edge_s[r][k];
            const int32_t *src = msg[r][k];
            int32_t *dst = g_all + edge_off[r][k];
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

/* ------------------------------------------------------------ transmit chain */
static uint32_t crc24a_word(const uint8_t *bits, uint32_t n)
{
    uint32_t reg = 0, i, j;
    for (i = 0; i < n; i++) {
        reg ^= (uint32_t)(bits[i] & 1u) << 23;
        for (j = 0; j < 24u; j++)
            reg = (reg & 0x800000u) ? (((reg << 1) ^ CRC_POLY) & 0xFFFFFFu)
                                    : ((reg << 1) & 0xFFFFFFu);
    }
    return reg;
}

/* 38.212 5.4.2.1 比特选择：打孔 2*Zc、跳过 filler，从 k0 读 E 位 */
static int rate_match(const uint8_t *cw, uint32_t rv)
{
    uint32_t i, j, k, u = 0;
    uint32_t k0 = rv * RV_STEP;

    for (i = 0; i < NCODED - PUNCT; i++) {
        uint8_t b = cw[PUNCT + i];
        if (i >= FILLER0 && i < FILLER0 + FILLER_N)
            continue;                       /* NULL: 不发送 */
        used_bits[u++] = b;
        if (u > USABLE)
            return 1;
    }
    if (u != USABLE)
        return 2;
    for (j = 0; j < E_BITS; j++) {
        k = (k0 + j) % USABLE;
        e_bits[j] = used_bits[k];
    }
    return 0;
}

/* 38.212 5.4.2.2 比特交织：按 Qm 行写、按列读 */
static void interleave(const uint8_t *in, uint8_t *out)
{
    uint32_t rows = E_BITS / QM, i, j;
    for (i = 0; i < rows; i++)
        for (j = 0; j < QM; j++)
            out[QM * i + j] = in[j * rows + i];
}

int main(void)
{
    uint32_t *out = RESULT_BASE;
    static int32_t hard[BG2_COLS][ZC] __attribute__((aligned(8)));
    const uint32_t *par = (const uint32_t *)PARAM_BASE;
    int32_t *freq = (int32_t *)FREQ_BASE;
    int32_t *td = (int32_t *)TD_RAM;
    int32_t hre[NSYM], him[NSYM];
    uint32_t t0, i, k, s, r;
    uint32_t rv, n_rnti, crc, syn, tb_bad = 0, sym_max = 0, re_cnt = 0;

    if (par[0] != TD_MAGIC || par[1] == 0 || par[2] != A_TB) {
        out[0] = 0x0BADBD51u;
        out[6] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        mmio_w(EXIT_ADDR, 0x600Du);
        return 0;
    }
    rv = par[9];                    /* variants[0] = {qm, rv, rnti, pad} at 8..11 */
    n_rnti = par[10];
    if (rv != 0) {                  /* 接收链的 rv_k0[0] == 0 */
        out[0] = 0x0BADBD52u;
        out[6] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        mmio_w(EXIT_ADDR, 0x600Du);
        return 0;
    }

    t0 = mtime();
    build_tables();

    /* ================= 1. transmit chain (B2) ============================= */
    for (i = 0; i < A_TB; i++)
        tb_bits[i] = (uint8_t)((par[4 + (i >> 5)] >> (i & 31u)) & 1u);
    for (i = 0; i < A_TB; i++)
        info_bits[i] = tb_bits[i];
    crc = crc24a_word(tb_bits, A_TB);
    for (i = 0; i < CRC_LEN; i++)
        info_bits[A_TB + i] = (uint8_t)((crc >> (23u - i)) & 1u);
    for (i = A_TB + CRC_LEN; i < KB * ZC; i++)
        info_bits[i] = 0;                       /* filler: known zero, not sent */

    if (ldpc_encode(&LDPC_BG2_ILS2, info_bits, KB, ZC, cw_bits) != 0)
        errs++;
    syn = ldpc_syndrome(&LDPC_BG2_ILS2, cw_bits, ZC);
    out[1172] = syn;                            /* must be 0 */

    if (rate_match(cw_bits, rv) != 0)
        errs++;
    gold_init(n_rnti << 15);                    /* 38.211 7.3.1.1 */
    for (k = 0; k < E_BITS; k++)
        e_sc[k] = (uint8_t)(e_bits[k] ^ gold_bit(k));
    interleave(e_sc, f_bits);
    for (k = 0; k < MAX_SYM; k++) {
        sym[2 * k] = (1 - 2 * (int32_t)f_bits[2 * k]) * QPSK;
        sym[2 * k + 1] = (1 - 2 * (int32_t)f_bits[2 * k + 1]) * QPSK;
        if ((uint32_t)(sym[2 * k] < 0 ? -sym[2 * k] : sym[2 * k]) > sym_max)
            sym_max = (uint32_t)(sym[2 * k] < 0 ? -sym[2 * k] : sym[2 * k]);
    }
    out[1170] = sym_max;
    out[1171] = 2 * MAX_SYM;

    /* ================= 2. DMRS + 2 OFDM symbols =========================== */
    build_dmrs();
    for (s = 0; s < NSYM; s++) {
        for (i = 0; i < 2u * NFFT; i++)
            freq[i] = 0;
        for (r = 0; r < NRE; r++) {
            int32_t vr, vi;
            if (r % 4u == 0) {                  /* DMRS RE */
                uint32_t p = r / 4u;
                vr = dmrs_re[s][p];
                vi = dmrs_im[s][p];
            } else {                            /* 数据 RE（顺序与接收一致） */
                vr = sym[2 * re_cnt];
                vi = sym[2 * re_cnt + 1];
                re_cnt++;
                if (re_cnt > MAX_SYM)
                    errs++;
            }
            freq[2 * (BIN0 + r)] = vr;          /* 共轭输入：虚部取负 */
            freq[2 * (BIN0 + r) + 1] = -vi;
        }
        drain_stores(freq, 2u * (BIN0 + NRE) - 1u);
        fft4096(freq);
        rt_dcache_flush();                      /* 引擎原地写了这片 */

        {
            int32_t *t = td + s * SYM_WORDS;
            for (k = 0; k < CP_LEN; k++) {
                uint32_t tt = NFFT - CP_LEN + k, rr = rev4(tt);
                t[2 * k] = freq[2 * rr] * 256;  /* TD_SCALE */
                t[2 * k + 1] = -freq[2 * rr + 1] * 256;
            }
            for (k = 0; k < NFFT; k++) {
                uint32_t rr = rev4(k);
                t[2 * (CP_LEN + k)] = freq[2 * rr] * 256;
                t[2 * (CP_LEN + k) + 1] = -freq[2 * rr + 1] * 256;
            }
        }
    }
    drain_stores(td, 2u * NSYM * SYM_WORDS - 1u);

    /* ================= 3. receive chain (A4/B4) =========================== */
    for (i = 0; i < USABLE; i++)
        rbuf[i] = 0;

    for (s = 0; s < NSYM; s++) {
        int32_t *sp = td + s * SYM_WORDS + 2 * CP_LEN;
        fft4096(sp);
        rt_dcache_flush();
        for (r = 0; r < NRE; r++) {
            uint32_t idx = rev4(re_bin(r));
            yw[(s * NRE + r) * 2 + 0] = sp[2 * idx] << 4;
            yw[(s * NRE + r) * 2 + 1] = sp[2 * idx + 1] << 4;
        }
    }

    /* 逐符号 LS 估计（60 个导频，38.211 7.4.1.1 Gold DMRS） */
    {
        int64_t den = (int64_t)NPILOT * 2 * QPSK * QPSK / H_SCALE;
        for (s = 0; s < NSYM; s++) {
            int64_t accr = 0, acci = 0;
            int p = 0;
            for (r = 0; r < NRE; r += 4) {
                int32_t yr = yw[(s * NRE + r) * 2 + 0];
                int32_t yi = yw[(s * NRE + r) * 2 + 1];
                int32_t dr = dmrs_re[s][p], di = dmrs_im[s][p];
                accr += (int64_t)yr * dr + (int64_t)yi * di;
                acci += (int64_t)yi * dr - (int64_t)yr * di;
                p++;
            }
            hre[s] = floor_div(accr, den);
            him[s] = floor_div(acci, den);
        }
    }

    /* 均衡 + QPSK 软 LLR（720 个，交织顺序） */
    {
        int mm = 0;
        for (s = 0; s < NSYM; s++)
            for (r = 0; r < NRE; r++) {
                int32_t yr, yi;
                int64_t er, ei;
                if (r % 4u == 0)
                    continue;                   /* DMRS RE */
                if (mm >= MAX_SYM)
                    break;
                yr = yw[(s * NRE + r) * 2 + 0];
                yi = yw[(s * NRE + r) * 2 + 1];
                er = (int64_t)yr * hre[s] + (int64_t)yi * him[s];
                ei = (int64_t)yi * hre[s] - (int64_t)yr * him[s];
                llr_e[2 * mm + 0] = (int32_t)er;
                llr_e[2 * mm + 1] = (int32_t)ei;
                mm++;
            }
    }

    /* 38.212 5.4.2.2 解交织（Qm=2，360 行） */
    {
        int rows = E_BITS / QM;
        for (i = 0; i < (uint32_t)rows; i++)
            for (k = 0; k < QM; k++)
                llr_i[k * rows + i] = llr_e[QM * i + k];
    }
    {   /* LLR 符号 anchor（解扰之前）：此时 llr_i 是 E 顺序，应与发射的
         * 加扰序列 e_sc 逐位同号。解扰之后它对应的是 e_bits，不能再比。 */
        uint32_t bad = 0, first = 0xFFFFFFFFu;
        for (k = 0; k < E_BITS; k++)
            if ((uint32_t)(llr_i[k] < 0) != (uint32_t)e_sc[k]) {
                if (first == 0xFFFFFFFFu) first = k;
                bad++;
            }
        out[1173] = bad;
        out[1174] = first;
        out[1175] = (first == 0xFFFFFFFFu) ? 0 : (uint32_t)llr_i[first];
    }
    /* 解扰（c_init = n_RNTI << 15） */
    gold_init(n_rnti << 15);
    for (k = 0; k < E_BITS; k++)
        if (gold_bit(k))
            llr_i[k] = -llr_i[k];
    /* 按循环缓冲坐标合并（单次传输），再解速率匹配 */
    for (k = 0; k < E_BITS; k++)
        rbuf[(k) % USABLE] += llr_i[k];
    for (i = 0; i < PUNCT; i++)
        dem[i] = 0;
    {
        uint32_t u = 0;
        for (i = 0; i < NCODED - PUNCT; i++) {
            if (i >= FILLER0 && i < FILLER0 + FILLER_N)
                dem[PUNCT + i] = 0;
            else
                dem[PUNCT + i] = rbuf[u++];
        }
    }
    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (i = 0; i < ZC; i++)
            llr[b][i] = dem[b * ZC + i] >> LLR_SHIFT;

    /* ================= 4. LDPC 迭代（8 次 min-sum） ======================= */
    for (uint32_t r2 = 0; r2 < BG2_ROWS; r2++)
        for (uint32_t kk = 0; kk < MAXD; kk++)
            for (i = 0; i < ZC; i++)
                msg[r2][kk][i] = (kk < bg2_deg[r2])
                                 ? llr[bg2_col[r2][kk]][(i + bg2_sh[r2][kk]) % ZC]
                                 : SENTINEL;
    for (uint32_t b = 0; b < BG2_COLS; b++) {
        int32_t (*gath)[ZC] = (int32_t (*)[ZC])(g_all + b * GSPAD);
        for (k = 0; k < 24; k++)
            for (i = 0; i < ZC; i++)
                gath[k][i] = 0;
        for (i = 0; i < ZC; i++)
            gath[24][i] = llr[b][i];
    }
    for (i = 0; i < ZC; i++) {
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
        for (uint32_t r2 = 0; r2 < BG2_ROWS; r2++) {
            if (r2 + 1 < BG2_ROWS) cn_row_in(r2 + 1, (r2 + 1) & 1u);
            if (rt_array_run(CGRA_BASE, r2 & 1u, CN_PROG_CYCLES)) errs++;
            runs++;
            if (r2 + 1 < BG2_ROWS) { if (rt_dma_wait(CGRA_BASE)) errs++; }
            cn_row_out(r2, r2 & 1u);
            if (rt_dma_wait(CGRA_BASE)) errs++;
        }
        rt_dcache_flush();
        reconstruct_gather();

        if (gpcp_cgra_imem_load(CGRA_BASE, bn_prog, BN_PROG_CYCLES * NPES))
            errs++;
        {
            int want_hard = (it + 1u == ITERS);
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
        }
    }
    uint32_t cyc = mtime() - t0;

    /* ================= 5. CRC24A + publish =============================== */
    {
        uint8_t bits[A_TB + CRC_LEN], crc_b[CRC_LEN];
        uint32_t crc_ok = 1;
        for (uint32_t b = 0; b < BG2_COLS; b++)
            for (i = 0; i < ZC; i++)
                out[8 + b * ZC + i] = (hard[b][i] < 0) ? 1u : 0u;
        for (i = 0; i < A_TB + CRC_LEN; i++)
            bits[i] = (uint8_t)(hard[i / ZC][i % ZC] < 0 ? 1 : 0);
        crc24a(bits, A_TB, crc_b);
        for (i = 0; i < CRC_LEN; i++)
            if (crc_b[i] != bits[A_TB + i])
                crc_ok = 0;
        for (i = 0; i < A_TB; i++) {
            out[1090 + i] = tb_bits[i];
            out[1180 + i] = bits[i];
            if (tb_bits[i] != bits[i])
                tb_bad++;
        }
        out[5] = crc_ok;
        out[7] = A_TB - tb_bad;
    }

    out[0] = 0xCAFEBD51u;
    out[1] = errs;
    out[2] = cyc;
    out[3] = runs;
    out[4] = dmas;
    out[6] = 0x600Du;
    out[1048] = (uint32_t)hre[0];
    out[1049] = (uint32_t)him[0];
    out[1050] = (uint32_t)hre[1];
    out[1051] = (uint32_t)him[1];
    for (i = 0; i < 8; i++)
        out[1052 + i] = (uint32_t)llr_e[i];
    for (i = 0; i < 8; i++)
        out[1060 + i] = (uint32_t)llr_i[i];
    out[1081] = dmrs_cinit(0);
    out[1082] = dmrs_cinit(1);
    out[1083] = rv;
    out[1084] = QM;
    out[1085] = QPSK;
    out[1086] = MAX_SYM;
    out[1087] = tb_bad;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    mmio_w(EXIT_ADDR, 0x600Du);
    return 0;
}
