/* -----------------------------------------------------------------------------
 * PDSCH 收发自环的「阶段实现」——被两个程序共用（同一算法只留一份）：
 *
 *   sw/soc/pdsch_loop_bm.c   人写参照基准（run-pdsch-loop），按老顺序直接调用；
 *   sw/soc/proto_runner.c    协议运行器（docs/20 §6），按 plan.h 的阶段表调用。
 *
 * 阶段划分与 docs/20 §2 的动词一一对应（顺序也一致）：
 *   transport_block -> encode -> modulate -> dmrs -> map_to_grid
 *   -> ofdm_modulate -> channel -> estimate -> equalise_demap -> decode
 *
 * 标 [array] 的阶段会真的启动阵列（IFFT 用 fft4096、LDPC 用 CN/BN 微码）；
 * 其余是标量阶段。本文件不含 main()，也不解析激励——调用者先 qs_setup()，
 * 再按阶段顺序调用（表只在 encode 里建，必须按序）。
 *
 * 使用本文件前必须 include：gpcp_cgra.h / soc_io.h / gpcp_cgra_rt.h /
 * ldpc_bg2.h / ldpc_enc_tables.h / ldpc_enc.inc / pdsch_{cn,bn}_prog.h。
 * -------------------------------------------------------------------------- */

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)

#define NPES          16
#define RING_DEPTH    1024
#define DESC_BASE     0x80020000ul
#define TD_RAM        0x80400000ul
/* 两个符号各占一块 32 KB 的频域工作区：map_to_grid 建整格的图，
 * ofdm_modulate 再逐符号做 IFFT（原程序复用一块，阶段化后必须分开）。 */
#define FREQ_BASE     0x803A0000ul
#define FREQ2_BASE    0x803A8000ul
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

/* 38.211 7.4.1.1 DMRS 参数（n_ID=1、l0=2） */
#define DMRS_NID      1
#define DMRS_NSCID    0
#define DMRS_NSLOT    0
#define DMRS_L0       2
#define DMRS_NSYMB    14
#define DMRS_NCP      0
#define TD_MAGIC      0x1D10D001ul

/* LDPC BG2/Zc=20（与 ldpc_bg2.h 的 42x52 基图一致） */
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
static int32_t *freq[NSYM] = { (int32_t *)FREQ_BASE, (int32_t *)FREQ2_BASE };

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, dmas;
static uint32_t busy;                      /* 阵列忙拍数（仅运行器统计） */

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

/* --------------------------------------------------------- 阶段状态与激励 */
static const uint32_t *qs_par;
static uint32_t qs_rv, qs_n_rnti;
static uint32_t qs_syn;                    /* 编码后 syndrome（必须 0） */
static uint32_t qs_sym_max;                /* max|symbol| */
static uint32_t qs_mm;                     /* 已处理的软符号数（跨两符号） */
static uint32_t qs_re_cnt;                 /* 已映射的数据 RE 数 */
static int32_t qs_hre[NSYM], qs_him[NSYM];
static int32_t qs_hard[BG2_COLS][ZC] __attribute__((aligned(8)));
static uint32_t qs_llr_bad, qs_llr_first, qs_llr_val;
static uint32_t qs_crc_ok, qs_tb_bad;
static uint8_t qs_rx_bits[A_TB + CRC_LEN];

/* 读激励（tools/nr_dl_mod.py dump-td 的格式）并复位统计量。
 * 返回 0 成功；非 0 表示激励本程序不支持（rv≠0 或 magic/长度不对）。 */
static int qs_setup(const uint32_t *par)
{
    if (par[0] != TD_MAGIC || par[1] == 0 || par[2] != A_TB)
        return 1;
    qs_rv = par[9];                 /* variants[0] = {qm, rv, rnti, pad} at 8..11 */
    qs_n_rnti = par[10];
    if (qs_rv != 0)                 /* 接收链的 rv_k0[0] == 0 */
        return 2;
    qs_par = par;
    errs = runs = dmas = busy = 0;
    qs_syn = qs_sym_max = qs_mm = qs_re_cnt = 0;
    qs_llr_bad = qs_crc_ok = qs_tb_bad = 0;
    qs_llr_first = 0xFFFFFFFFu;
    qs_llr_val = 0;
    return 0;
}

/* ------------------------------------------------------- 阶段 0：传输块 */
/* TB 80 bit + CRC24A + filler 零 → KB*Zc 个信息位。 */
static int qs_transport_block(void)
{
    uint32_t i, crc;
    for (i = 0; i < A_TB; i++)
        tb_bits[i] = (uint8_t)((qs_par[4 + (i >> 5)] >> (i & 31u)) & 1u);
    for (i = 0; i < A_TB; i++)
        info_bits[i] = tb_bits[i];
    crc = crc24a_word(tb_bits, A_TB);
    for (i = 0; i < CRC_LEN; i++)
        info_bits[A_TB + i] = (uint8_t)((crc >> (23u - i)) & 1u);
    for (i = A_TB + CRC_LEN; i < KB * ZC; i++)
        info_bits[i] = 0;                       /* filler: known zero, not sent */
    return 0;
}

/* -------------------------------------- 阶段 1：LDPC 编码（标量，CVA6） */
static int qs_encode(void)
{
    build_tables();
    if (ldpc_encode(&LDPC_BG2_ILS2, info_bits, KB, ZC, cw_bits) != 0)
        errs++;
    qs_syn = ldpc_syndrome(&LDPC_BG2_ILS2, cw_bits, ZC);
    return 0;
}

/* -------------------- 阶段 2：速率匹配 + 加扰 + 交织 + QPSK（全标量） */
static int qs_modulate(void)
{
    uint32_t k;
    if (rate_match(cw_bits, qs_rv) != 0)
        errs++;
    gold_init(qs_n_rnti << 15);                 /* 38.211 7.3.1.1 */
    for (k = 0; k < E_BITS; k++)
        e_sc[k] = (uint8_t)(e_bits[k] ^ gold_bit(k));
    interleave(e_sc, f_bits);
    qs_sym_max = 0;
    for (k = 0; k < MAX_SYM; k++) {
        sym[2 * k] = (1 - 2 * (int32_t)f_bits[2 * k]) * QPSK;
        sym[2 * k + 1] = (1 - 2 * (int32_t)f_bits[2 * k + 1]) * QPSK;
        {
            uint32_t a = (uint32_t)(sym[2 * k] < 0 ? -sym[2 * k] : sym[2 * k]);
            if (a > qs_sym_max) qs_sym_max = a;
        }
    }
    return 0;
}

/* ------------------------------------------- 阶段 3：解调参考信号（DMRS） */
static int qs_dmrs(void)
{
    build_dmrs();
    return 0;
}

/* --------------------------- 阶段 4：资源映射（2 个符号的频域格点） */
/* DMRS 每 4 个 RE 一个（60 导频 + 180 数据），共轭由虚部取负完成。 */
static int qs_map_to_grid(void)
{
    uint32_t s, i, r;
    qs_re_cnt = 0;
    for (s = 0; s < NSYM; s++) {
        for (i = 0; i < 2u * NFFT; i++)
            freq[s][i] = 0;
        for (r = 0; r < NRE; r++) {
            int32_t vr, vi;
            if (r % 4u == 0) {                  /* DMRS RE */
                uint32_t p = r / 4u;
                vr = dmrs_re[s][p];
                vi = dmrs_im[s][p];
            } else {                            /* 数据 RE（顺序与接收一致） */
                vr = sym[2 * qs_re_cnt];
                vi = sym[2 * qs_re_cnt + 1];
                qs_re_cnt++;
                if (qs_re_cnt > MAX_SYM)
                    errs++;
            }
            freq[s][2 * (BIN0 + r)] = vr;       /* 共轭输入：虚部取负 */
            freq[s][2 * (BIN0 + r) + 1] = -vi;
        }
        drain_stores(freq[s], 2u * (BIN0 + NRE) - 1u);
    }
    return 0;
}

/* ------------------------------- 阶段 5：OFDM 调制 [array]（每符号 IFFT+CP） */
static int qs_ofdm_modulate(void)
{
    uint32_t s, k;
    int32_t *td = (int32_t *)TD_RAM;
    for (s = 0; s < NSYM; s++) {
        fft4096(freq[s]);
        rt_dcache_flush();                      /* 引擎原地写了这片 */
        {
            int32_t *t = td + s * SYM_WORDS;
            for (k = 0; k < CP_LEN; k++) {
                uint32_t tt = NFFT - CP_LEN + k, rr = rev4(tt);
                t[2 * k] = freq[s][2 * rr] * 256;   /* TD_SCALE */
                t[2 * k + 1] = -freq[s][2 * rr + 1] * 256;
            }
            for (k = 0; k < NFFT; k++) {
                uint32_t rr = rev4(k);
                t[2 * (CP_LEN + k)] = freq[s][2 * rr] * 256;
                t[2 * (CP_LEN + k) + 1] = -freq[s][2 * rr + 1] * 256;
            }
        }
    }
    drain_stores(td, 2u * NSYM * SYM_WORDS - 1u);
    return 0;
}

/* ------------------------------------------------- 阶段 6：信道（自环 = 理想） */
static int qs_channel(void) { return 0; }

/* -------- 阶段 7：信道估计 [array]（每符号 FFT + RE 抽取 + 60 导频 LS） */
static int qs_estimate(void)
{
    uint32_t s, r, i;
    int32_t *td = (int32_t *)TD_RAM;
    int64_t den = (int64_t)NPILOT * 2 * QPSK * QPSK / H_SCALE;

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
        qs_hre[s] = floor_div(accr, den);
        qs_him[s] = floor_div(acci, den);
    }
    return 0;
}

/* ---- 阶段 8：均衡 + 软解调（LLR → 解交织 → 符号锚点 → 解扰 → 解速率匹配） */
static int qs_equalise_demap(void)
{
    uint32_t s, r, i, k;
    int rows = E_BITS / QM;
    uint32_t u;

    /* 均衡 + QPSK 软 LLR（720 个，交织顺序） */
    qs_mm = 0;
    for (s = 0; s < NSYM; s++)
        for (r = 0; r < NRE; r++) {
            int32_t yr, yi;
            int64_t er, ei;
            if (r % 4u == 0)
                continue;                       /* DMRS RE */
            if (qs_mm >= MAX_SYM)
                break;
            yr = yw[(s * NRE + r) * 2 + 0];
            yi = yw[(s * NRE + r) * 2 + 1];
            er = (int64_t)yr * qs_hre[s] + (int64_t)yi * qs_him[s];
            ei = (int64_t)yi * qs_hre[s] - (int64_t)yr * qs_him[s];
            llr_e[2 * qs_mm + 0] = (int32_t)er;
            llr_e[2 * qs_mm + 1] = (int32_t)ei;
            qs_mm++;
        }

    /* 38.212 5.4.2.2 解交织（Qm=2，360 行） */
    for (i = 0; i < (uint32_t)rows; i++)
        for (k = 0; k < QM; k++)
            llr_i[k * rows + i] = llr_e[QM * i + k];

    {   /* LLR 符号 anchor（解扰之前）：此时 llr_i 是 E 顺序，应与发射的
         * 加扰序列 e_sc 逐位同号。解扰之后它对应的是 e_bits，不能再比。 */
        uint32_t bad = 0, first = 0xFFFFFFFFu;
        for (k = 0; k < E_BITS; k++)
            if ((uint32_t)(llr_i[k] < 0) != (uint32_t)e_sc[k]) {
                if (first == 0xFFFFFFFFu) first = k;
                bad++;
            }
        qs_llr_bad = bad;
        qs_llr_first = first;
        qs_llr_val = (first == 0xFFFFFFFFu) ? 0 : (uint32_t)llr_i[first];
    }

    /* 解扰（c_init = n_RNTI << 15） */
    gold_init(qs_n_rnti << 15);
    for (k = 0; k < E_BITS; k++)
        if (gold_bit(k))
            llr_i[k] = -llr_i[k];

    /* 按循环缓冲坐标合并（单次传输），再解速率匹配 */
    for (k = 0; k < E_BITS; k++)
        rbuf[(k) % USABLE] += llr_i[k];
    for (i = 0; i < PUNCT; i++)
        dem[i] = 0;
    u = 0;
    for (i = 0; i < NCODED - PUNCT; i++) {
        if (i >= FILLER0 && i < FILLER0 + FILLER_N)
            dem[PUNCT + i] = 0;
        else
            dem[PUNCT + i] = rbuf[u++];
    }
    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (i = 0; i < ZC; i++)
            llr[b][i] = dem[b * ZC + i] >> LLR_SHIFT;
    return 0;
}

/* ---- 阶段 9：LDPC 译码 [array]（8 次 min-sum）+ 硬判 + CRC24A ------- */
static int qs_decode(void)
{
    uint32_t i, k;
    uint8_t bits[A_TB + CRC_LEN], crc_b[CRC_LEN];
    uint32_t crc_ok = 1;

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
#ifdef PS_TRACK_STATS
            busy += CN_PROG_CYCLES;
#endif
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
#ifdef PS_TRACK_STATS
                busy += BN_PROG_CYCLES;
#endif
                bn_dma_out(b, want_hard);
                if (rt_dma_wait(CGRA_BASE)) errs++;
                if (b + 1 < BG2_COLS) bn_dma_in(b + 1);
                bn_scatter(b, want_hard, qs_hard[b]);
                if (b + 1 < BG2_COLS) { if (rt_dma_wait(CGRA_BASE)) errs++; }
            }
        }
    }

    /* CRC24A：硬判前 104 位 = 80 TB + 24 CRC */
    for (i = 0; i < A_TB + CRC_LEN; i++)
        bits[i] = (uint8_t)(qs_hard[i / ZC][i % ZC] < 0 ? 1 : 0);
    crc24a(bits, A_TB, crc_b);
    for (i = 0; i < CRC_LEN; i++)
        if (crc_b[i] != bits[A_TB + i])
            crc_ok = 0;
    qs_crc_ok = crc_ok;
    qs_tb_bad = 0;
    for (i = 0; i < A_TB; i++) {
        qs_rx_bits[i] = bits[i];
        if (tb_bits[i] != bits[i])
            qs_tb_bad++;
    }
    for (i = 0; i < CRC_LEN; i++)
        qs_rx_bits[A_TB + i] = bits[A_TB + i];
    return 0;
}
