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
/* 描述符环放在 bn_tab（0x80041A00 结束）与 hard_all（0x80059000）之间的空闲段：
 * 以前放在 0x80020000，程序 .bss 一长过 0x80020000 就会被描述符覆盖（errs 正好
 * 落在 0x800203A0）。qs_layout_check() 现在把程序镜像也算作一个区域，重叠会报错。 */
#define DESC_BASE     0x80042000ul
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
/* Qm 由激励驱动（par[8]），数组按最大符号数（QPSK 360）开。 */
#define MAX_SYM_CAP   (E_BITS / 2)

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
#define SENTINEL      0x40000000   /* 必须大于 LLR 饱和值（32767），否则真实饱和边与缺失边无法区分 */
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
static int32_t sym[2 * MAX_SYM_CAP];
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
#include "../include/gpcp_desc64.h"   /* 64B 描述符字段（tools/gen_cgra_desc.py 生成） */
#include "pdsch_cn_wb_prog.h"   /* CN + 逐边 m' 回写（无 CPU reconstruct） */
#include "pdsch_bn_rot_prog.h"  /* BN 消息 bank 走每 bank 循环旋转 */
#ifdef PROTO_CPU_ONLY
#include "fft_cpu.inc"          /* 对照路径：同一算法的 CVA6 标量实现 */
#endif

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
static void reconstruct_gather_row(uint32_t r)
{
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

static void reconstruct_gather(void)
{
    for (uint32_t r = 0; r < BG2_ROWS; r++)
        reconstruct_gather_row(r);
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
static void interleave(const uint8_t *in, uint8_t *out, uint32_t qm)
{
    uint32_t rows = E_BITS / qm, i, j;
    for (i = 0; i < rows; i++)
        for (j = 0; j < qm; j++)
            out[qm * i + j] = in[j * rows + i];
}

/* 38.211 7.3.1.2 方星座的一个 I/Q 分支：b0 符号位，其余为 Gray 幅度位。 */
static int32_t qam_branch_level(uint32_t pat, uint32_t n)
{
    uint32_t sign = ((pat >> (n - 1)) & 1u) ? 0u : 1u;   /* 0 -> + */
    int32_t mag;
    if (n == 1)
        mag = 1;
    else if (n == 2)
        mag = (int32_t)((pat & 1u) ? 1 : 3);
    else {
        uint32_t m = pat & 3u;
        /* 64QAM 幅度 Gray：b1b2 = 00->1, 01->3, 11->5, 10->7。 */
        mag = (m == 0u) ? 1 : (m == 1u) ? 3 : (m == 3u) ? 5 : 7;
    }
    return sign ? mag : -mag;
}

static int32_t qam_branch_map(const uint8_t *b, uint32_t n, int32_t amp)
{
    uint32_t pat = 0, j;
    for (j = 0; j < n; j++)
        pat = (pat << 1) | (uint32_t)(b[j] & 1u);
    return qam_branch_level(pat, n) * amp;
}

/* max-log LLR，与 tools/nr_qam.py 的 qam_llr 逐值同口径。 */
static void qam_llr(int64_t eq_r, int64_t eq_i, int64_t h2, uint32_t qm,
                    int32_t *out)
{
    int32_t amp = (qm == 4) ? 894 : 436;
    uint32_t n = qm / 2u, b, k, p;
    int64_t ref = (h2 / H_SCALE) * amp;
    int64_t den, u[2];
    if (ref <= 0)
        ref = amp;
    den = 2 * ref;
    u[0] = eq_r;
    u[1] = eq_i;
    for (b = 0; b < 2; b++) {
        for (k = 0; k < n; k++) {
            int64_t d1 = (int64_t)1 << 62, d0 = (int64_t)1 << 62;
            for (p = 0; p < (1u << n); p++) {
                int64_t d = u[b] - ref * qam_branch_level(p, n);
                uint32_t bit = (p >> (n - 1u - k)) & 1u;
                d *= d;
                if (bit) {
                    if (d < d1) d1 = d;
                } else {
                    if (d < d0) d0 = d;
                }
            }
            {
                int64_t num = d1 - d0;
                int64_t q = (num < 0 ? -num : num) / den;
                out[n * b + k] = (int32_t)(num < 0 ? -q : q);
            }
        }
    }
}

/* --------------------------------------------------------- 阶段状态与激励 */
static const uint32_t *qs_par;
static uint32_t qs_rv, qs_n_rnti;
static uint32_t qs_qm, qs_max_sym;         /* 由激励 par[8] 驱动 */
static uint32_t qs_decode_iters = ITERS;   /* 纯接收 QAM 可加迭代 */
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
    qs_qm = par[8];
    if (qs_qm != 2u && qs_qm != 4u && qs_qm != 6u)
        return 3;
    qs_max_sym = E_BITS / qs_qm;
    if (qs_rv != 0)                 /* 接收链的 rv_k0[0] == 0 */
        return 2;
    qs_par = par;
    qs_decode_iters = ITERS;
    errs = runs = dmas = busy = 0;
    qs_syn = qs_sym_max = qs_mm = qs_re_cnt = 0;
    qs_llr_bad = qs_crc_ok = qs_tb_bad = 0;
    qs_llr_first = 0xFFFFFFFFu;
    qs_llr_val = 0;
    return 0;
}

/* 高阶 QAM 的纯接收激励：2 符号 TD + 尾部 7 字
 *   [0..2] TB 80 bit  [3] qm  [4] rv  [5] n_RNTI  [6] magic
 * 硬件只做 FFT -> LS -> QAM max-log -> LDPC；与 run-pdsch-16qam/64qam 同口径。 */
#define QAM_RX_MAGIC  0x51414D31ul
static int qs_rx_setup(const uint32_t *par)
{
    const uint32_t *tail = (const uint32_t *)(TD_RAM + NSYM * SYM_WORDS * 4u);
    (void)par;
    if (tail[6] != QAM_RX_MAGIC)
        return 1;
    for (uint32_t i = 0; i < A_TB; i++)
        tb_bits[i] = (uint8_t)((tail[i >> 5] >> (i & 31u)) & 1u);
    qs_qm = tail[3];
    qs_rv = tail[4];
    qs_n_rnti = tail[5];
    if (qs_qm != 2u && qs_qm != 4u && qs_qm != 6u)
        return 2;
    if (qs_rv != 0)
        return 3;
    qs_max_sym = E_BITS / qs_qm;
    qs_sym_max = (qs_qm == 2u) ? QPSK : ((qs_qm == 4u) ? 894u : 436u);
    qs_decode_iters = ITERS;
    errs = runs = dmas = busy = 0;
    qs_syn = qs_mm = qs_re_cnt = 0;
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

/* -------------------- 阶段 2：速率匹配 + 加扰 + 交织 + QAM（全标量） */
static int qs_modulate(void)
{
    uint32_t k, n = qs_qm / 2u;
    if (rate_match(cw_bits, qs_rv) != 0)
        errs++;
    gold_init(qs_n_rnti << 15);                 /* 38.211 7.3.1.1 */
    for (k = 0; k < E_BITS; k++)
        e_sc[k] = (uint8_t)(e_bits[k] ^ gold_bit(k));
    interleave(e_sc, f_bits, qs_qm);
    qs_sym_max = (qs_qm == 2u) ? QPSK : ((qs_qm == 4u) ? 894u : 436u);
    for (k = 0; k < qs_max_sym; k++) {
        if (qs_qm == 2u) {
            sym[2 * k] = (1 - 2 * (int32_t)f_bits[2 * k]) * QPSK;
            sym[2 * k + 1] = (1 - 2 * (int32_t)f_bits[2 * k + 1]) * QPSK;
        } else {
            int32_t amp = (int32_t)qs_sym_max;
            sym[2 * k] = qam_branch_map(&f_bits[qs_qm * k], n, amp);
            sym[2 * k + 1] = qam_branch_map(&f_bits[qs_qm * k + n], n, amp);
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
                if (qs_re_cnt < qs_max_sym) {
                    vr = sym[2 * qs_re_cnt];
                    vi = sym[2 * qs_re_cnt + 1];
                } else {
                    vr = 0;                     /* Qm>2 时剩余数据 RE 空闲 */
                    vi = 0;
                }
                qs_re_cnt++;
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
#ifdef PROTO_CPU_ONLY
        fft4096_cpu(freq[s]);
#else
        fft4096(freq[s]);
        rt_dcache_flush();                      /* 引擎原地写了这片 */
#endif
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
/* 节点粒度：接收 FFT 是独立变换节点，不再藏在 estimate 里。 */
static int qs_transform(void)
{
    uint32_t s, r;
    int32_t *td = (int32_t *)TD_RAM;

    for (s = 0; s < NSYM; s++) {
        int32_t *sp = td + s * SYM_WORDS + 2 * CP_LEN;
#ifdef PROTO_CPU_ONLY
        fft4096_cpu(sp);
#else
        fft4096(sp);
        rt_dcache_flush();
#endif
        for (r = 0; r < NRE; r++) {
            uint32_t idx = rev4(re_bin(r));
            yw[(s * NRE + r) * 2 + 0] = sp[2 * idx] << 4;
            yw[(s * NRE + r) * 2 + 1] = sp[2 * idx + 1] << 4;
        }
    }
    return 0;
}

static int qs_estimate(void)
{
    uint32_t s, r;
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
        qs_hre[s] = (int32_t)((accr >= 0 ? accr + den / 2 : accr - den / 2) / den);
        qs_him[s] = (int32_t)((acci >= 0 ? acci + den / 2 : acci - den / 2) / den);
    }
    return 0;
}

/* ---- 阶段 8：均衡 + 软解调（LLR → 解交织 → 符号锚点 → 解扰 → 解速率匹配） */
static int qs_equalise(void)
{
    uint32_t s, r, i, k;

    /* 均衡 + QAM 软 LLR（E_BITS 个，交织顺序） */
    qs_mm = 0;
    for (s = 0; s < NSYM; s++) {
        int64_t h2 = (int64_t)qs_hre[s] * qs_hre[s]
                   + (int64_t)qs_him[s] * qs_him[s];
        for (r = 0; r < NRE; r++) {
            int32_t yr, yi;
            int64_t er, ei;
            if (r % 4u == 0)
                continue;                       /* DMRS RE */
            if (qs_mm >= qs_max_sym)
                break;
            yr = yw[(s * NRE + r) * 2 + 0];
            yi = yw[(s * NRE + r) * 2 + 1];
            er = (int64_t)yr * qs_hre[s] + (int64_t)yi * qs_him[s];
            ei = (int64_t)yi * qs_hre[s] - (int64_t)yr * qs_him[s];
            if (qs_qm == 2u) {
                llr_e[2 * qs_mm + 0] = (int32_t)er;
                llr_e[2 * qs_mm + 1] = (int32_t)ei;
            } else {
                int32_t tmp[6];
                qam_llr(er, ei, h2, qs_qm, tmp);
                for (k = 0; k < qs_qm; k++)
                    llr_e[qs_qm * qs_mm + k] = tmp[k];
            }
            qs_mm++;
        }
    }
    return 0;
}

static int qs_demap(void)
{
    uint32_t i, k, rows = E_BITS / qs_qm;
    uint32_t u;

    for (i = 0; i < USABLE; i++)
        rbuf[i] = 0;

    /* 38.212 5.4.2.2 解交织 */
    for (i = 0; i < rows; i++)
        for (k = 0; k < qs_qm; k++)
            llr_i[k * rows + i] = llr_e[qs_qm * i + k];

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

/* 兼容入口：老的单片调用路径（bm 程序 / 逐阶段调试）仍可用。 */
static int qs_equalise_demap(void)
{
    qs_equalise();
    qs_demap();
    return 0;
}


/* ---------------------------------------------------------------------------
 * 64 字节复合描述符：一条 = 搬入 → 阵列 → 搬出，可重复 N 次（repeat）。
 *
 *   w0  src | spad | page<<16 | dir<<17          w1  len | stride<<32 | row<<48
 *   w2  status（引擎写）                          w3  cycles | page<<16 | en<<31
 *   w4  out_len | out_spad<<16 | out_en<<32       w5  out_dst
 *   w6  repeat | in_step<<16 | out_step<<32
 *
 * 打开方式：REG_DESC_MODE=1。旧的 32 字节格式不受影响。
 * 一条 CN 描述符覆盖 42 行：CPU 只写 16 个字，硬件自己跑 42 遍。
 * ------------------------------------------------------------------------- */
#define QDSC_DEPTH   256u
static uint32_t qn_desc, qn_arr, qn_busy;

static void qdsc_tile(uint32_t ram, uint32_t spad, uint32_t len, uint32_t page,
                      uint32_t stride, uint32_t row,
                      uint32_t cycles, uint32_t arr_page,
                      uint32_t out_len, uint32_t out_spad, uint32_t out_dst,
                      uint32_t repeat, uint32_t in_step, uint32_t out_step)
{
    uint32_t *d = (uint32_t *)DESC_BASE + 16u * qn_desc;   /* 64 字节 = 16 字 */
    uint32_t rep = repeat ? repeat : 1u;
    d[0]  = ram;
    d[1]  = (spad & 0xFFFFu) | ((page & 1u) << 16);        /* 搬入方向 dir=0 */
    d[2]  = len & 0xFFFFu;
    d[3]  = (stride & 0xFFFFu) | ((row & 0xFFFFu) << 16);
    d[4]  = 0;
    d[5]  = 0;                                             /* 状态字，引擎写 */
    d[6]  = cycles ? ((cycles & 0xFFFFu) | ((arr_page & 1u) << 16) | (1u << 31)) : 0u;
    d[7]  = 0;
    d[8]  = (out_len & 0xFFFFu) | ((out_spad & 0xFFFFu) << 16);
    d[9]  = out_len ? 1u : 0u;                             /* out_en */
    d[10] = out_dst;
    d[11] = 0;
    d[12] = (rep & 0xFFFFu) | ((in_step & 0xFFFFu) << 16);
    d[13] = out_step & 0xFFFFu;
    d[14] = 0;
    d[15] = 0;
    qn_desc++;
    if (cycles) {
        qn_arr  += rep;
        qn_busy += cycles * rep;
    }
}

/* 纯搬运（无阵列、无搬出）：末轮的硬判拷贝用它。 */
static void qdsc_plain(uint32_t ram, uint32_t spad, uint32_t len, uint32_t page,
                       uint32_t out, uint32_t stride, uint32_t row)
{
    uint32_t *d = (uint32_t *)DESC_BASE + 16u * qn_desc;
    d[0]  = ram;
    d[1]  = (spad & 0xFFFFu) | ((page & 1u) << 16) | ((out & 1u) << 17);
    d[2]  = len & 0xFFFFu;
    d[3]  = (stride & 0xFFFFu) | ((row & 0xFFFFu) << 16);
    d[4]  = 0; d[5] = 0; d[6] = 0; d[7] = 0;
    d[8]  = 0; d[9] = 0; d[10] = 0; d[11] = 0;
    d[12] = 1u; d[13] = 0; d[14] = 0; d[15] = 0;
    qn_desc++;
}

static uint32_t qd_done0, qd_wait;

/* 非阻塞提交：把 qn_desc 条描述符压进环，之后可以逐条 qdsc_wait_one()。
 * CPU 在等第 r 条完成时，引擎已经在跑第 r+1 条。 */
static void qdsc_start(void)
{
    qd_done0 = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE);
    qd_wait = 0;
    /* 描述符是 CPU 经 D-cache 写的，引擎从 RAM 读：必须先写回 */
    rt_dcache_flush();
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_DESC_MODE, 1u);         /* 64 字节模式 */
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_BASE, DESC_BASE);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_SIZE, QDSC_DEPTH);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_HEAD, 0u);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_TAIL, qn_desc);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_CTRL, GPCP_CGRA_RING_EN);
}

static void qdsc_wait_one(void)
{
    uint32_t spins = 0;
    qd_wait++;
    /* 拷贝引擎和 CPU 共用一个 AXI 主口：SoC 只在 CPU 当拍不发请求时才把
     * 总线让给 DMA（gpcp_soc 的 a_grant）。两查之间让出几拍。
     * 注：改成「逐描述符中断 + wfi」实测更慢（nr_pdsch decode +67k），
     * 一次中断进出 ~80 拍，804 次就是把省下的全吃掉；这个 SoC 上 DMA
     * 并没有被 CPU 抢总线饿到，等待本来就是 CPU 自己在干活。 */
    while (((mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE) - qd_done0) & 0xFFFFu) < qd_wait) {
        for (volatile uint32_t z = 0; z < 8u; z++)
            ;
        if (++spins > 4000000u) { errs++; break; }
    }
}

static void qdsc_finish(void)
{
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_CTRL, 0u);
    runs += qn_arr;
    dmas += qn_desc;
#ifdef PS_TRACK_STATS
    busy += qn_busy;
#endif
}

/* LDPC 的 8 次 min-sum 迭代：tile 不再逐个由 CPU 启动，而是一次提交一个环。
 *
 *   CN 环：42 × (旋转表 + 复合描述符)。复合描述符自己完成「搬入 → 阵列 → 搬出」，
 *          CPU 只在环尾等一次完成。
 *   BN 环：52 × (表 + LLR + n 条边搬入 + 复合 + n-1 条边搬出 + 硬判)。
 *
 * 视图转置（CN 索引 ↔ 变量节点索引）由阵列的每 bank 旋转表在地址通路完成：
 *   - CN 的 m' 直接回写到原 bank（keep=1 的哨兵边不写）；
 *   - BN 的消息 bank 用 rot=(ZC-sh)%ZC 读写，缺失边 valid=0 读 0。
 * 因此 CPU 不再有 reconstruct_gather / bn_scatter，也不再逐 tile 轮询。
 */
#define CN_TAB_BASE   0x8003E000ul
#define BN_TAB_BASE   0x80040000ul
#define HARD_ALL_BASE 0x80059000ul
#define CN_RING_SIZE  128u             /* 42 × 2 = 84 条，2 的幂 */
#define BN_RING_BASE  (DESC_BASE + 0x2000ul)
#define BN_RING_SIZE  1024u

static int32_t *hard_all = (int32_t *)HARD_ALL_BASE;
static uint32_t qs_ring_n, qs_bn_n;
static int qs_ring_ready;

/* ------------------------------------------------- 内存区域契约（布局唯一声明）
 * 谁占哪一段、多大、尾部是否有 canary。描述符提交前用它校验 src/dst 是否落在
 * 声明区域内；阶段结束时校验 canary。这样"指针算错/长度写错"会在提交时被拦住，
 * 而不是等下游数据算错（2026-11-15 的 row10 事故就是越界写静默改坏了邻居）。
 * 只列 LDPC 环路径真正用到的区域；gath/bout 是 CPU 对照路径的旧缓冲，未列入。 */
typedef struct {
    const char *name;
    uint32_t    base;
    uint32_t    size;
    uint32_t    row;        /* >0：窗口不得跨该字节边界（msg 的行） */
    uint8_t     guard;      /* 1 = 尾部有 QS_GUARD_WORDS 个字的 canary */
} qs_region_t;

#define QS_CANARY       0xA5C3A5C3u
#define QS_GUARD_WORDS  16u
#define QS_REGION_N     (sizeof(qs_regions) / sizeof(qs_regions[0]))

static qs_region_t qs_regions[] = {
    { "msg",     MSG_BASE,      BG2_ROWS * MAXD * ZC * 4u, MAXD * ZC * 4u, 1 },
    { "res",     RES_BASE,      42u * 3u * ZC * 4u,        0, 0 },
    { "llr",     LLR_BASE,      BG2_COLS * ZC * 4u,        0, 0 },
    { "hard",    HARD_ALL_BASE, BG2_COLS * ZC * 4u,        0, 0 },
    { "cn_tab",  CN_TAB_BASE,   BG2_ROWS * 32u * 4u,       0, 0 },
    { "bn_tab",  BN_TAB_BASE,   BG2_COLS * 32u * 4u,       0, 0 },
    { "desc",    DESC_BASE,     0,                         0, 0 },
    { "bn_desc", BN_RING_BASE,  0,                         0, 0 },
    { "prog",    0x80000000ul,  0,                         0, 0 },  /* .text+.bss */
};

static void qs_puts(const char *s)
{
    while (*s) mmio_w(0x10000000ul, (uint32_t)(uint8_t)*s++);
}

extern char _bss_end[];   /* link.ld 提供：程序镜像（text/rodata/data/bss）末尾 */

static void qs_puts_hex(uint32_t v)
{
    static const char hx[] = "0123456789abcdef";
    int32_t s;
    qs_puts("0x");
    for (s = 28; s >= 0; s -= 4) mmio_w(0x10000000ul, (uint32_t)hx[(v >> s) & 15u]);
}

static const qs_region_t *qs_region_of(uint32_t addr)
{
    for (uint32_t i = 0; i < QS_REGION_N; i++) {
        const qs_region_t *r = &qs_regions[i];
        if (r->size && addr >= r->base && addr < r->base + r->size) return r;
    }
    return 0;
}

static int qs_region_contains(uint32_t addr, uint32_t bytes)
{
    for (uint32_t i = 0; i < QS_REGION_N; i++) {
        const qs_region_t *r = &qs_regions[i];
        if (!r->size || addr < r->base || bytes > r->size - (addr - r->base)) continue;
        if (r->row && ((addr - r->base) % r->row) + bytes > r->row) continue;  /* 跨行 */
        return 1;
    }
    return 0;
}

/* 区域表自检：互不重叠，且都在 CPU 可访问的 512 KiB 窗口内。 */
static int qs_layout_check(void)
{
    int bad = 0;
    for (uint32_t i = 0; i < QS_REGION_N; i++) {
        const qs_region_t *a = &qs_regions[i];
        if (!a->size) continue;
        if (a->base < 0x80000000ul || a->base + a->size > 0x80080000ul) {
            qs_puts("qs_layout: 区域越出 CPU 窗口: "); qs_puts(a->name); qs_puts("\n");
            bad++;
        }
        for (uint32_t j = i + 1; j < QS_REGION_N; j++) {
            const qs_region_t *b = &qs_regions[j];
            if (!b->size) continue;
            if (a->base < b->base + b->size && b->base < a->base + a->size) {
                qs_puts("qs_layout: 区域重叠: "); qs_puts(a->name);
                qs_puts(" / "); qs_puts(b->name); qs_puts("\n");
                bad++;
            }
        }
    }
    return bad;
}

static void qs_canary_arm(void)
{
    for (uint32_t i = 0; i < QS_REGION_N; i++) {
        if (!qs_regions[i].guard) continue;
        volatile uint32_t *p = (volatile uint32_t *)(qs_regions[i].base + qs_regions[i].size);
        for (uint32_t w = 0; w < QS_GUARD_WORDS; w++) p[w] = QS_CANARY;
    }
}

static int qs_canary_check(void)
{
    int bad = 0;
    for (uint32_t i = 0; i < QS_REGION_N; i++) {
        if (!qs_regions[i].guard) continue;
        volatile const uint32_t *p =
            (volatile const uint32_t *)(qs_regions[i].base + qs_regions[i].size);
        for (uint32_t w = 0; w < QS_GUARD_WORDS; w++) {
            if (p[w] != QS_CANARY) {
                qs_puts("qs_canary: 区域尾部被写: "); qs_puts(qs_regions[i].name);
                qs_puts(" +"); qs_puts_hex(w * 4u); qs_puts("\n");
                bad++;
                break;
            }
        }
    }
    return bad;
}

/* 描述符窗口校验：搬入/搬出窗口都必须完整落在声明区域内，长度 > 0。 */
static int qs_desc_validate(const uint32_t *d)
{
    uint32_t len = gpcp_d64_in_len(d);
    uint32_t src = gpcp_d64_src(d);
    int ok = 1;
    if (!len || !qs_region_contains(src, len * 4u)) {
        const qs_region_t *rg = qs_region_of(src);
        qs_puts("qs_desc: 搬入越界 "); qs_puts(rg ? rg->name : "?");
        qs_puts(" src="); qs_puts_hex(src);
        qs_puts(" len="); qs_puts_hex(len); qs_puts("\n");
        ok = 0;
    }
    if (gpcp_d64_out_en(d)) {
        uint32_t olen = gpcp_d64_out_len(d);
        uint32_t odst = gpcp_d64_out_dst(d);
        if (!olen || !qs_region_contains(odst, olen * 4u)) {
            const qs_region_t *rg = qs_region_of(odst);
            qs_puts("qs_desc: 搬出越界 "); qs_puts(rg ? rg->name : "?");
            qs_puts(" dst="); qs_puts_hex(odst);
            qs_puts(" len="); qs_puts_hex(olen); qs_puts("\n");
            ok = 0;
        }
    }
    return ok;
}

static uint32_t *qs_desc(void)
{
    uint32_t *d = (uint32_t *)DESC_BASE + 16u * qs_ring_n;
    for (uint32_t i = 0; i < 16u; i++) d[i] = 0u;
    qs_ring_n++;
    return d;
}

static uint32_t *qs_desc_bn(void)
{
    uint32_t *d = (uint32_t *)BN_RING_BASE + 16u * qs_ring_n;
    for (uint32_t i = 0; i < 16u; i++) d[i] = 0u;
    qs_ring_n++;
    return d;
}

/* 每 bank 旋转表：bit[4:0]=rot、bit5=valid、bit6=keep。 */
static void qs_tables_build(void)
{
    uint32_t *ct = (uint32_t *)CN_TAB_BASE;
    uint32_t *bt = (uint32_t *)BN_TAB_BASE;
    for (uint32_t r = 0; r < BG2_ROWS; r++) {
        uint32_t deg = bg2_deg[r];
        for (uint32_t k = 0; k < 32u; k++)
            ct[r * 32u + k] = ((k >= deg) && (k < MAXD)) ? 0x40u : 0u;
    }
    for (uint32_t b = 0; b < BG2_COLS; b++) {
        uint32_t n = blk_n[b];
        for (uint32_t e = 0; e < 32u; e++) {
            uint32_t v = 0u;
            if (e < n)          v = (ZC - blk_sh[b][e]) % ZC;
            else if (e < 24u)   v = 0x20u;      /* invalid=1：缺失边读 0 */
            bt[b * 32u + e] = v;
        }
    }
}

static uint32_t qs_cn_row_build(uint32_t r)
{
    uint32_t n0 = qs_ring_n;
    uint32_t *d = qs_desc();                 /* 旋转表（只用于 keep 位） */
    gpcp_d64_set_src(d, CN_TAB_BASE + r * 128u);
    gpcp_d64_set_tgt_rot(d, 1u);
    gpcp_d64_set_in_len(d, 32u);
    d = qs_desc();                           /* 搬入 → CN → 搬回 */
    gpcp_d64_set_src(d, (uint32_t)(uintptr_t)&msg[r][0][0]);
    gpcp_d64_set_in_len(d, MAXD * ZC);
    gpcp_d64_set_stride(d, GPCP_LAYOUT_WORDS);
    gpcp_d64_set_row(d, ZC);
    gpcp_d64_set_cycles(d, CN_WB_PROG_CYCLES);
    gpcp_d64_set_arr_en(d, 1u);
    gpcp_d64_set_out_len(d, MAXD * ZC);
    gpcp_d64_set_out_en(d, 1u);
    gpcp_d64_set_out_dst(d, (uint32_t)(uintptr_t)&msg[r][0][0]);
    gpcp_d64_set_rep(d, 1u);
    return qs_ring_n - n0;
}

static uint32_t qs_bn_block_build(uint32_t b)
{
    uint32_t n0 = qs_ring_n;
    uint32_t n = blk_n[b];
    uint32_t *d = qs_desc_bn();              /* 旋转表 */
    gpcp_d64_set_src(d, BN_TAB_BASE + b * 128u);
    gpcp_d64_set_tgt_rot(d, 1u);
    gpcp_d64_set_in_len(d, 32u);
    d = qs_desc_bn();                        /* LLR → bank 24 */
    gpcp_d64_set_src(d, (uint32_t)(uintptr_t)&llr[b][0]);
    gpcp_d64_set_spad(d, GPCP_SPAD_IDX(GPCP_LAYOUT_REGION_LLR, 0));
    gpcp_d64_set_in_len(d, ZC);
    for (uint32_t e = 0; e + 1u < n; e++) {  /* 前 n-1 条边搬入 */
        d = qs_desc_bn();
        gpcp_d64_set_src(d, (uint32_t)(uintptr_t)&msg[blk_r[b][e]][blk_k[b][e]][0]);
        gpcp_d64_set_spad(d, GPCP_SPAD_IDX(e, 0));
        gpcp_d64_set_in_len(d, ZC);
    }
    d = qs_desc_bn();                        /* 最后一条边 + BN + 第 0 条边搬出 */
    gpcp_d64_set_src(d, (uint32_t)(uintptr_t)&msg[blk_r[b][n - 1u]][blk_k[b][n - 1u]][0]);
    gpcp_d64_set_spad(d, GPCP_SPAD_IDX(n - 1u, 0));
    gpcp_d64_set_in_len(d, ZC);
    gpcp_d64_set_cycles(d, BN_ROT_PROG_CYCLES);
    gpcp_d64_set_arr_en(d, 1u);
    gpcp_d64_set_out_len(d, ZC);
    gpcp_d64_set_out_en(d, 1u);
    gpcp_d64_set_out_dst(d, (uint32_t)(uintptr_t)&msg[blk_r[b][0]][blk_k[b][0]][0]);
    gpcp_d64_set_rep(d, 1u);
    for (uint32_t e = 1; e < n; e++) {       /* 其余边搬出 */
        d = qs_desc_bn();
        gpcp_d64_set_src(d, (uint32_t)(uintptr_t)&msg[blk_r[b][e]][blk_k[b][e]][0]);
        gpcp_d64_set_spad(d, GPCP_SPAD_IDX(e, 0));
        gpcp_d64_set_dir(d, 1u);
        gpcp_d64_set_in_len(d, ZC);
    }
    d = qs_desc_bn();                        /* 硬判 bank 25 → hard_all[b] */
    gpcp_d64_set_src(d, (uint32_t)(uintptr_t)&hard_all[b * ZC]);
    gpcp_d64_set_spad(d, GPCP_SPAD_IDX(GPCP_LAYOUT_REGION_HARD, 0));
    gpcp_d64_set_dir(d, 1u);
    gpcp_d64_set_in_len(d, ZC);
    return qs_ring_n - n0;
}

static void qs_ring_submit(uint32_t base, uint32_t n, uint32_t size)
{
    uint32_t spins = 0;
    uint32_t d0 = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE);
    /* 描述符由 CPU 刚写入：提交前统一刷 D$，避免 DMA 读到旧 RAM。
     * 放在 API 内部，调用方不可能漏。 */
    rt_dcache_flush();
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_DESC_MODE, 1u);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_BASE, base);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_SIZE, size);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_HEAD, 0u);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_TAIL, n);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_CTRL, GPCP_CGRA_RING_EN);
    while ((((mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE) - d0)) & 0xFFFFu) < n) {
        if (++spins > 8000000u) { errs++; break; }
    }
    /* 硬件写窗口：任何越窗的写拍都会被丢弃并置 sticky 位。这里并进 errs 并清掉，
     * 让门禁直接失败，而不是“静默少写”后在下游算错。 */
    if (mmio_r(CGRA_BASE + GPCP_CGRA_WIN_STATUS) & 1u) {
        errs++;
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_WIN_STATUS, 1u);
    }
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_CTRL, 0u);
}

static void qs_imem_host_load(const uint32_t *prog, uint32_t words)
{
    uint32_t i;
    for (i = 0; i < words; i++)
        gpcp_cgra_write(CGRA_BASE, 0x2000u + i * 4u, prog[i]);
}

static int qs_decode_ring(void)
{
    uint32_t it, r, b, i;

    if (!qs_ring_ready) {
        qs_tables_build();
        /* 两个整环都只建一次：描述符内容不随迭代变化（msg/表地址固定），
         * 引擎退休时只往 d[4]/d[5] 写状态字，本驱动的描述符没用到这两个字。
         * 这样每次迭代只剩「重装 IMEM + 使能环」，CPU 不必逐 tile 重建。 */
        qs_ring_n = 0;
        for (r = 0; r < BG2_ROWS; r++)
            (void)qs_cn_row_build(r);
        uint32_t ncn = qs_ring_n;
        qs_ring_n = 0;
        for (b = 0; b < BG2_COLS; b++)
            qs_bn_n += qs_bn_block_build(b);
        /* 环的实际长度进区域表，然后做布局自检 + canary + 逐条窗口校验。 */
        qs_regions[6].size = ncn * 64u;
        qs_regions[7].size = qs_bn_n * 64u;
        qs_regions[8].size = (uint32_t)(uintptr_t)_bss_end - 0x80000000ul;
        errs += (uint32_t)qs_layout_check();
        qs_canary_arm();
        for (i = 0; i < ncn; i++)
            errs += qs_desc_validate((const uint32_t *)DESC_BASE + 16u * i) ? 0u : 1u;
        for (i = 0; i < qs_bn_n; i++)
            errs += qs_desc_validate((const uint32_t *)BN_RING_BASE + 16u * i) ? 0u : 1u;
        qs_ring_ready = 1;
    }
    /* CN 微码用 bank 30 的 +32767 初始化 min1/min2；只跑 page 0。 */
    for (i = 0; i < ZC; i++)
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK, i), (uint32_t)SENTINEL);
    /* 大块 CPU 写（msg/表/描述符）必须先排空写缓冲，否则 DMA 会读到旧 RAM。 */
    drain_stores((const int32_t *)CN_TAB_BASE, BG2_ROWS * 32u - 1u);
    drain_stores((const int32_t *)BN_TAB_BASE, BG2_COLS * 32u - 1u);
    drain_stores(&msg[0][0][0], BG2_ROWS * MAXD * ZC - 1u);
    /* 表/描述符是 CPU 经 D-cache 写的，引擎从 RAM 读：先落盘并失效 */
    rt_dcache_flush();
    /* 旋转表的模 = ZC；不写这个寄存器时硬件默认 32 */
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_ROT_ZC, ZC);

    for (it = 0; it < qs_decode_iters; it++) {
        qs_imem_host_load(cn_wb_prog, CN_WB_PROG_CYCLES * NPES);
        qs_ring_submit(DESC_BASE, 2u * BG2_ROWS, CN_RING_SIZE);
        qs_imem_host_load(bn_rot_prog, BN_ROT_PROG_CYCLES * NPES);
        qs_ring_submit(BN_RING_BASE, qs_bn_n, BN_RING_SIZE);
    }

    rt_dcache_flush();
    errs += (uint32_t)qs_canary_check();   /* 阶段结束：区域尾部 must 未被写 */
    for (b = 0; b < BG2_COLS; b++)
        for (i = 0; i < ZC; i++)
            qs_hard[b][i] = hard_all[b * ZC + i];

    runs += (BG2_ROWS + BG2_COLS) * qs_decode_iters;
    dmas += (2u * BG2_ROWS + qs_bn_n) * qs_decode_iters;
    busy += (BG2_ROWS * CN_WB_PROG_CYCLES +
             BG2_COLS * BN_ROT_PROG_CYCLES) * qs_decode_iters;
    return 0;
}

/* TaskPlan 生成物：proto/emit.py 写下 task_decode.inc（编排），这里只做包含。
 * bm 程序没有该 include 路径，__has_include 为假，自动回落到手写实现。 */
#define GPCP_RING_DECODE 1

#if defined(__has_include)
#  if __has_include("task_decode.inc")
#    include "task_decode.inc"
#    define GPCP_TASK_DECODE 1
#  endif
#endif

/* ---- 阶段 9：LDPC 译码 [array]（8 次 min-sum）+ 硬判 + CRC24A ------- */
static int qs_decode(void)
{
    uint32_t i, k;
    uint8_t bits[A_TB + CRC_LEN], crc_b[CRC_LEN];
    uint32_t crc_ok = 1;

    build_tables();                 /* 纯接收路径没有 encode 阶段，这里补建 */
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
#ifdef PROTO_CPU_ONLY
    /* 对照路径：同一 min-sum / BN 公式全在 CVA6 上跑，不碰阵列 */
    for (uint32_t it = 0; it < qs_decode_iters; it++) {
        for (uint32_t r2 = 0; r2 < BG2_ROWS; r2++) {
            for (i = 0; i < ZC; i++) {
                int32_t m1 = SENTINEL, m2 = SENTINEL, sg = 0;
                for (uint32_t p = 0; p < MAXD / 2; p++) {
                    int32_t a = msg[r2][2 * p][i], b = msg[r2][2 * p + 1][i];
                    int32_t al = a >> 31, bl = b >> 31;
                    int32_t aa = (a ^ al) - al, ba = (b ^ bl) - bl;
                    int32_t lo = aa < ba ? aa : ba, hi = aa > ba ? aa : ba;
                    int32_t t2 = m2 < hi ? m2 : hi;
                    int32_t t1 = m1 > lo ? m1 : lo;
                    m2 = t1 < t2 ? t1 : t2;
                    m1 = m1 < lo ? m1 : lo;
                    sg ^= al ^ bl;
                }
                res_r[r2][0][i] = m1;
                res_r[r2][1][i] = m2;
                res_r[r2][2][i] = sg;
            }
        }
        reconstruct_gather();

        {
            int want_hard = (it + 1u == qs_decode_iters);
            for (uint32_t b = 0; b < BG2_COLS; b++) {
                uint32_t n = blk_n[b];
                int32_t (*gath)[ZC] = (int32_t (*)[ZC])(g_all + b * GSPAD);
                int32_t *bout = b_all + b * GSPAD;
                for (i = 0; i < ZC; i++) {
                    int32_t v = llr[b][i];
                    for (k = 0; k < 24; k++)
                        v += gath[k][i];
                    for (k = 0; k < n; k++)
                        bout[k * ZC + i] = v - gath[k][i];
                    if (want_hard)
                        bout[n * ZC + i] = v < 0 ? -1 : 0;
                }
                bn_scatter(b, want_hard, qs_hard[b]);
            }
        }
    }
#else
#if defined(GPCP_RING_DECODE)
    qs_decode_ring();              /* 复合描述符环：CPU 一次提交，硬件逐块滚动 */
#elif defined(GPCP_TASK_DECODE)
    task_decode();                 /* 编排由 TaskPlan 生成（backend/gpcp/emit_host.py） */
#else
    for (i = 0; i < ZC; i++) {
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK, i), (uint32_t)SENTINEL);
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK + GPCP_LAYOUT_PAGE_BANKS, i),
               (uint32_t)SENTINEL);
    }
    if (gpcp_cgra_imem_load(CGRA_BASE, cn_prog, CN_PROG_CYCLES * NPES))
        errs++;

    for (uint32_t it = 0; it < qs_decode_iters; it++) {
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
            int want_hard = (it + 1u == qs_decode_iters);
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
#endif /* GPCP_TASK_DECODE */
#endif

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
