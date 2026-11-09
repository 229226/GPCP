/* -----------------------------------------------------------------------------
 * LTE PDSCH 的「阶段实现」（36.212 / 36.211）——阶段 3「跨制式」。
 *
 * 这个文件是**实现库**的一部分：HLL 场景（proto/scenarios/lte_pdsch.py）只
 * 描述协议，不出现这里任何常量；本文件与 proto/models/lte_pdsch.py 逐值对拍。
 *
 * 口径（与模型一致，报告里会写明简化）：
 *   K=104（80 bit TB + CRC24A），Turbo 1/3，QPP (f1,f2)=(7,26)；
 *   尾比特零终止（3 拍冲洗 + 1 个 0），两个编码器共用同一组尾比特；
 *   2 符号 × 240 RE，CRS 每 6 个 RE 一个（40 导频/符号），数据 200 RE/符号，
 *   E = 800 bit；加扰 c_init = n_RNTI<<14；CRS c_init 按 36.211 6.10.1.1。
 *
 * 激励：2 符号时域样本（各 CP+NFFT 复数），尾部追加 4 个字：
 *   [2*SYM_WORDS+0..2] TB 80 bit（3 字）  [3] rv
 * -------------------------------------------------------------------------- */

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)
#define PARAM_BASE    0x80600000ul

#define NPES          16
#define RING_DEPTH    1024
#define DESC_BASE     0x80020000ul
#define TD_RAM        0x80400000ul

#define NSYM          2
#define NRE           240
#define CRS_STEP      6
#define NPILOT        (NRE / CRS_STEP)     /* 40 */
#define NDATA         (NRE - NPILOT)       /* 200 */
#define NFFT          4096
#define CP_LEN        288
#define BIN0          (NFFT / 2 + 1)
#define SYM_WORDS     (2 * (CP_LEN + NFFT))
#define QPSK          2000
#define TD_SCALE      256
#define H_SCALE       16
#define LLR_SHIFT     8
#define N_RNTI        0x1234u
#define N_ID          1

#define A_TB          80
#define CRC_LEN       24
#define K_INFO        (A_TB + CRC_LEN)     /* 104 */
#define TAIL          4
#define NPOL          (K_INFO + TAIL)      /* 108 */
#define F1            7
#define F2            26
#define E_BITS        800
#define ITERS         8

#define GOLD_BASE     0x80380000ul
#define GOLD_NC       1600
#define GOLD_LEN      (E_BITS + GOLD_NC + 31)

#define LLR_BASE      0x80390000ul
#define SCR_BASE      0x803A0000ul

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, dmas, busy;
static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;
static uint32_t n_desc, done;

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

#include "ssb_fft.inc"          /* fft4096() + rev4() */

/* --------------------------------------------------------------- 小工具 */
static int32_t *llr_buf = (int32_t *)LLR_BASE;        /* E_BITS */
static int32_t *scr_buf = (int32_t *)SCR_BASE;

static uint8_t tb_bits[A_TB];
static uint8_t info_bits[K_INFO];
static uint8_t d0_bits[NPOL], p1_bits[NPOL], p2_bits[NPOL];
static uint8_t e_bits[E_BITS], e_sc[E_BITS];
static int32_t sym[2 * (E_BITS / 2)];
static int32_t crs_re[NSYM][NPILOT], crs_im[NSYM][NPILOT];
static int32_t llr_d0[NPOL], llr_p1[NPOL], llr_p2[NPOL];
static uint32_t lte_crc_ok, lte_rv;
static uint8_t rx_info[K_INFO];

/* 38.212 7.1 gCRC24A */
static void crc24a_bits(const uint8_t *bits, int n, uint8_t *out)
{
    uint32_t reg = 0;
    for (int i = 0; i < n; i++) {
        reg ^= (uint32_t)(bits[i] & 1u) << 23;
        for (int j = 0; j < 24; j++)
            reg = (reg & 0x800000u) ? (((reg << 1) ^ 0x864CFBu) & 0xFFFFFFu)
                                    : ((reg << 1) & 0xFFFFFFu);
    }
    for (int i = 0; i < 24; i++)
        out[i] = (uint8_t)((reg >> (23 - i)) & 1u);
}

static uint32_t gold_x1[GOLD_LEN], gold_x2[GOLD_LEN];

static void gold_init(uint32_t c_init)
{
    for (uint32_t i = 0; i < GOLD_LEN; i++) { gold_x1[i] = 0; gold_x2[i] = 0; }
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

/* ------------------------------------------------------------ RSC / QPP */
/* 状态位：bit2 = a_{k-1}, bit1 = a_{k-2}, bit0 = a_{k-3} */
static uint32_t rsc_step(uint32_t st, uint32_t u, uint32_t *par)
{
    uint32_t a1 = (st >> 2) & 1u, a2 = (st >> 1) & 1u, a3 = st & 1u;
    uint32_t ak = u ^ a2 ^ a3;
    *par = ak ^ a1 ^ a3;
    return (ak << 2) | (a1 << 1) | a2;
}

static uint32_t qpp(uint32_t i) { return (F1 * i + F2 * i * i) % K_INFO; }

/* 零终止的 3 个冲洗输入（第一个编码器的状态决定，两个编码器共用） */
static void lte_tails(const uint8_t *bits, uint8_t *tails)
{
    uint32_t st = 0, par;
    for (int i = 0; i < K_INFO; i++)
        st = rsc_step(st, bits[i], &par);
    for (int t = 0; t < 3; t++) {
        uint32_t a2 = (st >> 1) & 1u, a3 = st & 1u;
        uint32_t u = a2 ^ a3;
        tails[t] = (uint8_t)u;
        st = rsc_step(st, u, &par);
    }
}

static void rsc_encode(const uint8_t *bits, const uint8_t *tails,
                       uint8_t *d, uint8_t *p)
{
    uint32_t st = 0, par;
    int i;
    for (i = 0; i < K_INFO; i++) {
        st = rsc_step(st, bits[i], &par);
        d[i] = bits[i];
        p[i] = (uint8_t)par;
    }
    for (int t = 0; t < 3; t++) {
        st = rsc_step(st, tails[t], &par);
        d[K_INFO + t] = tails[t];
        p[K_INFO + t] = (uint8_t)par;
    }
    d[NPOL - 1] = 0;
    p[NPOL - 1] = 0;
}

/* ------------------------------------------ 36.212 5.1.4.1 子块交织 + 圆缓冲 */
static const uint8_t SUB_P[32] = {
    0, 16, 8, 24, 4, 20, 12, 28, 2, 18, 10, 26, 6, 22, 14, 30,
    1, 17, 9, 25, 5, 21, 13, 29, 3, 19, 11, 27, 7, 23, 15, 31
};

/* pos[i] = 原下标 i 在子块交织输出里的位置；返回有效位个数（= NPOL） */
static void subblock_pos(uint32_t *pos)
{
    const int rows = (NPOL + 31) / 32;
    int out_i = 0;
    for (int c = 0; c < 32; c++) {
        int col = SUB_P[c];
        for (int r = 0; r < rows; r++) {
            int idx = r * 32 + col;
            if (idx < NPOL)
                pos[idx] = (uint32_t)out_i++;
        }
    }
}

static void subblock_perm(const uint8_t *in, uint8_t *out)
{
    const int rows = (NPOL + 31) / 32;
    int k = 0;
    for (int c = 0; c < 32; c++) {
        int col = SUB_P[c];
        for (int r = 0; r < rows; r++) {
            int idx = r * 32 + col;
            if (idx < NPOL)
                out[k++] = in[idx];
        }
    }
}

static void rate_match(const uint8_t *d, const uint8_t *p1, const uint8_t *p2,
                       uint8_t rv)
{
    static uint8_t v0[NPOL], v1[NPOL], v2[NPOL], w[3 * NPOL];
    uint32_t kw = 3 * NPOL, k0 = (uint32_t)rv * (kw / 4);
    subblock_perm(d, v0);
    subblock_perm(p1, v1);
    subblock_perm(p2, v2);
    for (uint32_t i = 0; i < (uint32_t)NPOL; i++) {
        w[i] = v0[i];
        w[NPOL + 2 * i] = v1[i];
        w[NPOL + 2 * i + 1] = v2[i];
    }
    for (int j = 0; j < E_BITS; j++)
        e_bits[j] = w[(k0 + (uint32_t)j) % kw];
}

/* --------------------------------------------------------- 阶段状态与激励 */
static uint32_t lte_tb_ok, lte_hard_bad;

static int lte_setup(const uint32_t *par)
{
    const uint32_t *tail = (const uint32_t *)(TD_RAM + (uint32_t)NSYM * SYM_WORDS * 4u);
    if (par[0] == 0x4C544530u) {          /* 可选的 magic（模型不写，保留） */
        lte_rv = par[1];
    } else {
        lte_rv = tail[3];
    }
    errs = runs = dmas = busy = 0;
    lte_crc_ok = 0;
    lte_hard_bad = 0;
    for (int i = 0; i < A_TB; i++)
        tb_bits[i] = (uint8_t)((tail[i >> 5] >> (i & 31u)) & 1u);
    return 0;
}

/* ------------------------------------------- 阶段 0：接收（TD 已由宿主放入） */
static int lte_received(void) { return 0; }

/* ------------------------------ 阶段 1：传输块（TB + CRC24A + 80 bit 打包） */
static int lte_transport_block(void)
{
    for (int i = 0; i < A_TB; i++)
        info_bits[i] = tb_bits[i];
    crc24a_bits(tb_bits, A_TB, info_bits + A_TB);
    return 0;
}

/* ------------------- 阶段 2：Turbo 编码 + 速率匹配（标量，CVA6） */
static int lte_encode(void)
{
    static uint8_t d0b[NPOL];
    uint8_t tails[3], c_int[K_INFO];
    for (int i = 0; i < K_INFO; i++)
        c_int[i] = info_bits[qpp((uint32_t)i)];
    lte_tails(info_bits, tails);
    rsc_encode(info_bits, tails, d0_bits, p1_bits);
    rsc_encode(c_int, tails, d0b, p2_bits);
    rate_match(d0_bits, p1_bits, p2_bits, (uint8_t)lte_rv);
    return 0;
}

/* ---------------------------- 阶段 3：加扰 + QPSK（c_init = n_RNTI<<14） */
static int lte_modulate(void)
{
    gold_init((N_RNTI << 14) & 0x7FFFFFFFu);
    for (int k = 0; k < E_BITS; k++)
        e_sc[k] = (uint8_t)(e_bits[k] ^ gold_bit((uint32_t)k));
    for (int k = 0; k < E_BITS / 2; k++) {
        sym[2 * k] = (1 - 2 * (int32_t)e_sc[2 * k]) * QPSK;
        sym[2 * k + 1] = (1 - 2 * (int32_t)e_sc[2 * k + 1]) * QPSK;
    }
    return 0;
}

/* ------------------------------- 阶段 4：CRS（36.211 6.10.1.1，单端口） */
static int lte_dmrs(void)
{
    for (int s = 0; s < NSYM; s++) {
        /* c_init = (7(n_s+1)+l+1)(2N_ID+1) + 2N_ID + N_CP，n_s=0, N_CP=1 */
        uint32_t cinit = (uint32_t)((7 * (0 + 1) + s + 1) * (2 * N_ID + 1)
                                    + 2 * N_ID + 1);
        gold_init(cinit & 0x7FFFFFFFu);
        for (int p = 0; p < NPILOT; p++) {
            crs_re[s][p] = (1 - 2 * (int32_t)gold_bit((uint32_t)(2 * p))) * QPSK;
            crs_im[s][p] = (1 - 2 * (int32_t)gold_bit((uint32_t)(2 * p + 1))) * QPSK;
        }
    }
    return 0;
}

/* ------------------------- 阶段 5：资源映射（CRS 每 6 个 RE 一个） */
static int lte_map_to_grid(void)
{
    int m = 0;
    /* 两个符号各占一块频域工作区（与 NR 自环同一块高区 RAM） */
    for (int s = 0; s < NSYM; s++) {
        int32_t *f = (int32_t *)(uintptr_t)(0x803A0000ul + (uint32_t)s * 0x8000u);
        for (int i = 0; i < 2 * NFFT; i++)
            f[i] = 0;
        int p = 0;
        for (int r = 0; r < NRE; r++) {
            if (r % CRS_STEP == 0) {
                f[2 * (BIN0 + r)] = crs_re[s][p];
                f[2 * (BIN0 + r) + 1] = -crs_im[s][p];     /* 共轭输入 */
                p++;
            } else {
                f[2 * (BIN0 + r)] = sym[2 * m];
                f[2 * (BIN0 + r) + 1] = -sym[2 * m + 1];
                m++;
            }
        }
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        (void)f[2 * (BIN0 + NRE) - 1];
    }
    if (m != E_BITS / 2)
        errs++;
    return 0;
}

/* -------------------------------- 阶段 6：OFDM 调制 [array]（IFFT + CP） */
static int lte_ofdm_modulate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    for (int s = 0; s < NSYM; s++) {
        int32_t *f = (int32_t *)(uintptr_t)(0x803A0000ul + (uint32_t)s * 0x8000u);
        fft4096(f);
        rt_dcache_flush();
        {
            int32_t *t = td + s * SYM_WORDS;
            for (int k = 0; k < CP_LEN; k++) {
                uint32_t tt = NFFT - CP_LEN + (uint32_t)k, rr = rev4(tt);
                t[2 * k] = f[2 * rr] * TD_SCALE;
                t[2 * k + 1] = -f[2 * rr + 1] * TD_SCALE;
            }
            for (int k = 0; k < NFFT; k++) {
                uint32_t rr = rev4((uint32_t)k);
                t[2 * (CP_LEN + k)] = f[2 * rr] * TD_SCALE;
                t[2 * (CP_LEN + k) + 1] = -f[2 * rr + 1] * TD_SCALE;
            }
        }
    }
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)td[2 * NSYM * SYM_WORDS - 1];
    return 0;
}

/* --------------------------------------------- 阶段 7：信道（自环 = 理想） */
static int lte_channel(void) { return 0; }

/* ---------------- 阶段 8：OFDM 解调 [array]（两个符号各一次 FFT + RE 抽取） */
static int32_t yre[NSYM][NRE], yim[NSYM][NRE];

static int lte_ofdm_demodulate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    for (int s = 0; s < NSYM; s++) {
        int32_t *sp = td + s * SYM_WORDS + 2 * CP_LEN;
        fft4096(sp);
        rt_dcache_flush();
        for (int r = 0; r < NRE; r++) {
            uint32_t idx = rev4((uint32_t)(BIN0 + r));
            yre[s][r] = sp[2 * idx];
            yim[s][r] = sp[2 * idx + 1];
        }
    }
    return 0;
}

/* ---------------------- 阶段 9：CRS 信道估计（36.211 6.10.1.1，单端口） */
static int32_t hre[NSYM], him[NSYM];

static int lte_estimate(void)
{
    const int64_t den = (int64_t)NPILOT * 2 * QPSK * QPSK / H_SCALE;
    for (int s = 0; s < NSYM; s++) {
        int64_t accr = 0, acci = 0;
        for (int p = 0; p < NPILOT; p++) {
            int r = p * CRS_STEP;
            int64_t yr = (int64_t)yre[s][r] << 4;
            int64_t yi = (int64_t)yim[s][r] << 4;
            int64_t dr = crs_re[s][p], di = crs_im[s][p];
            accr += yr * dr + yi * di;
            acci += yi * dr - yr * di;
        }
        hre[s] = (int32_t)((accr >= 0 ? accr + den / 2 : accr - den / 2) / den);
        him[s] = (int32_t)((acci >= 0 ? acci + den / 2 : acci - den / 2) / den);
    }
    return 0;
}

/* ---- 阶段 10：均衡 + QPSK 软解调（E 顺序，负 = 1 的仓库口径） */
static int lte_equalise_demap_main(void)
{
    int m = 0;
    for (int s = 0; s < NSYM; s++) {
        for (int r = 0; r < NRE; r++) {
            if (r % CRS_STEP == 0)
                continue;
            int64_t yr = (int64_t)yre[s][r] << 4;
            int64_t yi = (int64_t)yim[s][r] << 4;
            int64_t er = yr * hre[s] + yi * him[s];
            int64_t ei = yi * hre[s] - yr * him[s];
            llr_buf[2 * m] = (int32_t)(er >> LLR_SHIFT);
            llr_buf[2 * m + 1] = (int32_t)(ei >> LLR_SHIFT);
            m++;
        }
    }
    if (m != E_BITS / 2)
        errs++;
    return 0;
}

/* ---- 阶段 11：解扰 + 解速率匹配（E -> d0/p1/p2 的 LLR） */
static int lte_descramble_dematch(void)
{
    gold_init((N_RNTI << 14) & 0x7FFFFFFFu);
    for (int k = 0; k < E_BITS; k++)
        if (gold_bit((uint32_t)k))
            llr_buf[k] = -llr_buf[k];

    {
        static int32_t acc[3 * NPOL];
        static uint32_t pos[NPOL];
        uint32_t kw = 3 * NPOL, k0 = lte_rv * (kw / 4);
        for (uint32_t i = 0; i < kw; i++)
            acc[i] = 0;
        for (int k = 0; k < E_BITS; k++)
            acc[(k0 + (uint32_t)k) % kw] += llr_buf[k];
        subblock_pos(pos);
        for (int i = 0; i < NPOL; i++) {
            llr_d0[i] = acc[pos[i]];
            llr_p1[i] = acc[NPOL + 2 * pos[i]];
            llr_p2[i] = acc[NPOL + 2 * pos[i] + 1];
        }
    }
    return 0;
}

/* 阶段 10 的组合入口：均衡/LLR -> 解扰 -> 解速率匹配。 */
static int lte_equalise_demap(void)
{
    lte_equalise_demap_main();
    return lte_descramble_dematch();
}

/* ---------------- 阶段 10：Turbo 译码 [标量]（max-log-MAP × 8 次）+ CRC24A */
#define TURBO_NEG (-(1 << 28))
static int32_t t_alpha[NPOL][8], t_beta[NPOL][8];

static void siso(const int32_t *sys, const int32_t *par, const int32_t *apr,
                 int n, int end_zero, int32_t *ext, uint8_t *hard)
{
    for (int k = 0; k < n; k++)
        for (int s = 0; s < 8; s++) { t_alpha[k][s] = TURBO_NEG; t_beta[k][s] = TURBO_NEG; }
    for (int s = 0; s < 8; s++) t_alpha[n][s] = TURBO_NEG;
    t_alpha[0][0] = 0;
    for (int k = 0; k < n; k++) {
        for (int s = 0; s < 8; s++) {
            if (t_alpha[k][s] == TURBO_NEG) continue;
            for (uint32_t u = 0; u < 2; u++) {
                uint32_t pk, ns = rsc_step((uint32_t)s, u, &pk);
                int32_t v = t_alpha[k][s]
                          + (u ? 1 : -1) * (sys[k] + apr[k])
                          + (int32_t)(pk ? 1 : -1) * par[k];
                if (v > t_alpha[k + 1][ns]) t_alpha[k + 1][ns] = v;
            }
        }
    }
    for (int s = 0; s < 8; s++) t_beta[n][s] = end_zero ? TURBO_NEG : 0;
    if (end_zero) t_beta[n][0] = 0;
    for (int k = n - 1; k >= 0; k--) {
        for (int s = 0; s < 8; s++) {
            int32_t best = TURBO_NEG;
            for (uint32_t u = 0; u < 2; u++) {
                uint32_t pk, ns = rsc_step((uint32_t)s, u, &pk);
                int32_t v = t_beta[k + 1][ns]
                          + (u ? 1 : -1) * (sys[k] + apr[k])
                          + (int32_t)(pk ? 1 : -1) * par[k];
                if (v > best) best = v;
            }
            t_beta[k][s] = best;
        }
    }
    for (int k = 0; k < n; k++) {
        int32_t b[2] = { TURBO_NEG, TURBO_NEG };
        for (int s = 0; s < 8; s++) {
            if (t_alpha[k][s] == TURBO_NEG) continue;
            for (uint32_t u = 0; u < 2; u++) {
                uint32_t pk, ns = rsc_step((uint32_t)s, u, &pk);
                int32_t v = t_alpha[k][s] + t_beta[k + 1][ns]
                          + (u ? 1 : -1) * (sys[k] + apr[k])
                          + (int32_t)(pk ? 1 : -1) * par[k];
                if (v > b[u]) b[u] = v;
            }
        }
        int32_t post = b[1] - b[0];
        hard[k] = post > 0 ? 1 : 0;
        ext[k] = post - (sys[k] + apr[k]);
        if (ext[k] > 2000000) ext[k] = 2000000;
        if (ext[k] < -2000000) ext[k] = -2000000;
    }
}

static int lte_decode(void)
{
    static int32_t L[NPOL], par1[NPOL], par2[NPOL], e1[NPOL], e2[NPOL];
    static uint8_t hard[NPOL];
    int n = K_INFO + 3;

    /* 仓库口径（负 = 1）→ 译码器内部（正 = 1） */
    for (int i = 0; i < NPOL; i++) {
        L[i] = -llr_d0[i];
        par1[i] = -llr_p1[i];
        par2[i] = -llr_p2[i];
    }
    for (int it = 0; it < ITERS; it++) {
        static int32_t apr2[NPOL], sys2[NPOL], ext_d[NPOL], zero[NPOL];
        for (int i = 0; i < NPOL; i++) zero[i] = 0;
        siso(L, par1, zero, n, 1, e1, hard);
        for (int i = 0; i < K_INFO; i++) {
            apr2[i] = e1[qpp((uint32_t)i)];
            sys2[i] = L[qpp((uint32_t)i)];
        }
        for (int i = K_INFO; i < n; i++) { apr2[i] = 0; sys2[i] = L[i]; }
        siso(sys2, par2, apr2, n, 0, e2, hard);
        for (int i = 0; i < K_INFO; i++)
            ext_d[qpp((uint32_t)i)] = e2[i];
        for (int i = 0; i < K_INFO; i++)
            L[i] = L[i] + e1[i] + ext_d[i];
    }
    for (int i = 0; i < K_INFO; i++)
        rx_info[i] = L[i] > 0 ? 1 : 0;

    /* CRC24A */
    {
        uint8_t crc[CRC_LEN];
        crc24a_bits(rx_info, A_TB, crc);
        lte_crc_ok = 1;
        for (int i = 0; i < CRC_LEN; i++)
            if (crc[i] != rx_info[A_TB + i]) lte_crc_ok = 0;
        lte_tb_ok = 1;
        for (int i = 0; i < A_TB; i++)
            if (rx_info[i] != tb_bits[i]) lte_tb_ok = 0;
    }
    return 0;
}
