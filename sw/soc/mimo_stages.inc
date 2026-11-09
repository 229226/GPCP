/* -----------------------------------------------------------------------------
 * 2x2 MIMO PDSCH 接收的「阶段实现」——被两个程序共用：
 *   sw/soc/mimo_bm.c          人写参照基准（run-mimo）
 *   sw/soc/proto_runner.c     协议运行器（docs/20 阶段 2 迁入）
 *
 * 阶段与 §2 的动词对应：received -> dmrs -> ofdm_demodulate -> estimate
 * -> mimo_equalise。b = H^H y 由 mmse_2x1 tile 在阵列上算，2x2 求解与硬判在 CPU。
 *
 * 使用前必须 include：gpcp_cgra.h / soc_io.h / gpcp_cgra_rt.h /
 * mmse_2x1_prog.h / mmse_2x1_layout.h（由 gen_kernel_prog.py 生成）。
 * -------------------------------------------------------------------------- */

#define NPES          16
#define RING_DEPTH    1024
#define DESC_BASE     0x80020000ul

#define NFFT          4096
#define CP_LEN        288
#define BIN0          (NFFT / 2 + 1)
#define SYM_WORDS     (2 * (CP_LEN + NFFT))
#define NRE           240
#define NPILOT        60
#define NP_LAYER      (NPILOT / 2)
#define NDATA         180
#define QPSK          2000
#define H_SCALE       16
#define FRONT_GAIN    4
#define GOLD_NC       1600
#define GOLD_LEN      (2 * NP_LAYER + GOLD_NC + 31)
#define CSI_CINIT0    0xC01u
#define CSI_CINIT1    0xC02u
#define NBITS         (NDATA * 4)

#define TD_RAM        0x80400000ul
#define PARAM_BASE    0x80600000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)
#define CGRA_BASE     0x50000000ul

#define MBANKS (MMSE_2X1_BANK_HI - MMSE_2X1_BANK_LO + 1)

static inline uint32_t mtime(void) { return *(volatile uint32_t *)0x0200BFF8ul; }

static uint32_t errs, runs, dmas, busy;
static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
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

#include "ssb_fft.inc"          /* fft4096() + rev4() */

/* complex helpers on (re, im) pairs */
static int64_t cm_re(int64_t ar, int64_t ai, int64_t br, int64_t bi)
{ return ar * br - ai * bi; }
static int64_t cm_im(int64_t ar, int64_t ai, int64_t br, int64_t bi)
{ return ar * bi + ai * br; }

/* ---------------------------------------------------------------- CGRA MMSE */
/* b = H^H y 由 CGRA 的 mmse_2x1 tile 计算：一次 16 个 RE（一个 PE 一个 RE），
 * 每个 RE 喂 8 个 Q15 叶子，回 2 个 Q30 汇（numr/numi）。板级验证时 *16 还原到
 * C 参考的 FRONT_GAIN 量纲，因此 2x2 求解与纯 C 版逐位一致。 */
/* DMA 直接读 RAM，编译器看不见 -> 必须 volatile */
static volatile int32_t mleaf[MBANKS][16];
static uint32_t mmse_runs, mmse_dmas;

static void mmse_prog_load(void)
{
    if (gpcp_cgra_imem_load(CGRA_BASE, mmse_2x1_prog,
                            MMSE_2X1_PROG_CYCLES * NPES) != 0)
        errs++;
}

static void mmse_dma(uintptr_t ram, uint32_t spad, uint32_t words, uint32_t dir)
{
    /* 显式序列：清 done -> 回读确认 -> 等 busy 落 -> 启动 -> 轮询本次 done。
     * rt_dma_run 会在 "start 被 busy 忽略" 时误判成功，所以不用它。 */
    uint32_t g = 0;
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_DMA_CTRL, GPCP_CGRA_DMA_CLRDONE);
    while (mmio_r(CGRA_BASE + GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_DONE)
        if (++g > 100000u) { errs++; return; }
    g = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_BUSY)
        if (++g > 200000u) { errs++; return; }
    gpcp_cgra_dma_start(CGRA_BASE, ram, spad, words, 0, dir, 32, 16);
    g = 0;
    while (!(mmio_r(CGRA_BASE + GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_DONE))
        if (++g > 200000u) { errs++; return; }
    mmse_dmas++;
}

static void mmse_run(void)
{
    /* 32 拍的阵列太短：必须"清 DONE + 回读确认 + 轮询本次 DONE"；
     * 否则会读到上一轮遗留的 done 而提前读 spad（实测 l=1 的 b 恒等于 l=0）。 */
    uint32_t g = 0;
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_CTRL, GPCP_CGRA_CTRL_CLRDONE);
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE)
        if (++g > 100000u) { errs++; break; }
    gpcp_cgra_array_start(CGRA_BASE, 0, MMSE_2X1_PROG_CYCLES);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    g = 0;
    while (!(mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE))
        if (++g > 200000u) { errs++; break; }
    mmse_runs++;
    runs++;
    busy += MMSE_2X1_PROG_CYCLES;
}

/* --------------------------------------------------------- 阶段状态与激励 */
static int32_t ms_pre[2][NP_LAYER], ms_pim[2][NP_LAYER];
static int64_t ms_hre[2][2], ms_him[2][2];
static int64_t ms_g00r, ms_g01r, ms_g01i, ms_g11r, ms_lam, ms_detr, ms_deti, ms_dd;
static uint32_t ms_bits[NDATA];
static uint32_t ms_bit_err;

/* 激励是裸的两块时域样本（没有 magic 头）。 */
static int ms_setup(const uint32_t *par)
{
    (void)par;
    errs = runs = dmas = mmse_runs = mmse_dmas = busy = 0;
    ms_bit_err = 0;
    return 0;
}

/* ------------------------------------------------ 阶段 0：接收（两块 TD 常驻） */
static int ms_received(void) { return 0; }

/* ------------------------------- 阶段 1：解调参考信号（两层 FDM 导频序列） */
static int ms_dmrs(void)
{
    const uint32_t cinits[2] = { CSI_CINIT0, CSI_CINIT1 };
    for (int l = 0; l < 2; l++) {
        gold_init(cinits[l]);
        for (int i = 0; i < NP_LAYER; i++) {
            int32_t c0 = (int32_t)gold_bit((uint32_t)(2 * i));
            int32_t c1 = (int32_t)gold_bit((uint32_t)(2 * i + 1));
            ms_pre[l][i] = (1 - 2 * c0) * QPSK;
            ms_pim[l][i] = (1 - 2 * c1) * QPSK;
        }
    }
    return 0;
}

/* --------------------------- 阶段 2：OFDM 解调 [array]（两个天线各一次 FFT） */
static int ms_ofdm_demodulate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    for (int rx = 0; rx < 2; rx++)
        fft4096(td + (size_t)rx * SYM_WORDS + 2 * CP_LEN);
    return 0;
}

/* ------------------------- 阶段 3：信道估计（每层 30 个 FDM 导频的 LS） */
static int ms_estimate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    int32_t *sp[2];
    const int64_t den = (int64_t)NP_LAYER * 2 * QPSK * QPSK / H_SCALE;

    for (int rx = 0; rx < 2; rx++)
        sp[rx] = td + (size_t)rx * SYM_WORDS + 2 * CP_LEN;

    for (int rx = 0; rx < 2; rx++) {
        int64_t accr[2] = { 0, 0 }, acci[2] = { 0, 0 };
        int idx = 0;
        for (int r = 0; r < NRE; r += 4) {
            uint32_t ip = rev4(BIN0 + (uint32_t)r);
            int64_t yr = (int64_t)sp[rx][2 * ip] << FRONT_GAIN;
            int64_t yi = (int64_t)sp[rx][2 * ip + 1] << FRONT_GAIN;
            int l = idx & 1, p = idx >> 1;
            accr[l] += yr * ms_pre[l][p] + yi * ms_pim[l][p];
            acci[l] += yi * ms_pre[l][p] - yr * ms_pim[l][p];
            idx++;
        }
        for (int l = 0; l < 2; l++) {
            ms_hre[rx][l] = accr[l] / den;
            ms_him[rx][l] = acci[l] / den;
        }
    }

    /* G = H^H H, one lambda for both diagonal terms */
    ms_g00r = ms_g01r = ms_g01i = ms_g11r = 0;
    for (int rx = 0; rx < 2; rx++) {
        int64_t a_r = ms_hre[rx][0], a_i = ms_him[rx][0];
        int64_t b_r = ms_hre[rx][1], b_i = ms_him[rx][1];
        ms_g00r += a_r * a_r + a_i * a_i;
        ms_g11r += b_r * b_r + b_i * b_i;
        ms_g01r += cm_re(a_r, -a_i, b_r, b_i);       /* conj(a) * b */
        ms_g01i += cm_im(a_r, -a_i, b_r, b_i);
    }
    ms_lam = (ms_g00r + ms_g11r) / 64;
    if (ms_lam < 1) ms_lam = 1;
    ms_g00r += ms_lam;
    ms_g11r += ms_lam;

    /* det = g00*g11 - g01*conj(g01)  (real, since G is Hermitian) */
    {
        int64_t g10r = ms_g01r, g10i = -ms_g01i;
        ms_detr = ms_g00r * ms_g11r - cm_re(ms_g01r, ms_g01i, g10r, g10i);
        ms_deti = 0 - cm_im(ms_g01r, ms_g01i, g10r, g10i);
        ms_dd = ms_detr * ms_detr + ms_deti * ms_deti;
        if (ms_dd <= 0) { ms_dd = 1; errs++; }
    }
    return 0;
}

/* --------- 阶段 4：多天线均衡 [array]（b = H^H y on CGRA + 2x2 solve） */
static int ms_mimo_equalise(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    int32_t *sp[2];
    int dre[NDATA], nd = 0;
    static int64_t b0r_a[NDATA], b0i_a[NDATA], b1r_a[NDATA], b1i_a[NDATA];
    int cg_ok = 1;

    for (int rx = 0; rx < 2; rx++)
        sp[rx] = td + (size_t)rx * SYM_WORDS + 2 * CP_LEN;

    /* data-RE index table（pilots every 4th RE） */
    for (int rr = 0; rr < NRE && nd < NDATA; rr++)
        if (rr % 4 != 0) dre[nd++] = rr;

    /* 量纲守卫：mul 是 16x16，且 4 项 Q30 求和不能溢出 int32 */
    {
        int64_t hmax = 0, ymax = 0;
        for (int rx = 0; rx < 2; rx++)
            for (int l = 0; l < 2; l++) {
                int64_t a = ms_hre[rx][l] < 0 ? -ms_hre[rx][l] : ms_hre[rx][l];
                int64_t b = ms_him[rx][l] < 0 ? -ms_him[rx][l] : ms_him[rx][l];
                if (a > hmax) hmax = a;
                if (b > hmax) hmax = b;
            }
        for (int i = 0; i < NDATA; i++) {
            uint32_t ip = rev4(BIN0 + (uint32_t)dre[i]);
            for (int rx = 0; rx < 2; rx++)
                for (int q = 0; q < 2; q++) {
                    int64_t v = sp[rx][2 * ip + q];
                    if (v < 0) v = -v;
                    if (v > ymax) ymax = v;
                }
        }
        if (hmax > 32767 || ymax > 32767 || 4 * hmax * ymax > 0x7FFFFFFFLL)
            cg_ok = 0;
    }
    if (cg_ok) {
        mmse_prog_load();
        for (int base = 0; base < NDATA; base += 16) {
            int n = NDATA - base; if (n > 16) n = 16;
            for (int l = 0; l < 2; l++) {
                for (int k = 0; k < 16; k++) {
                    if (k < n) {
                        uint32_t ip = rev4(BIN0 + (uint32_t)dre[base + k]);
                        mleaf[MMSE_2X1_BANK_H1R - MMSE_2X1_BANK_LO][k] = (int32_t)ms_hre[0][l];
                        mleaf[MMSE_2X1_BANK_H1I - MMSE_2X1_BANK_LO][k] = (int32_t)ms_him[0][l];
                        mleaf[MMSE_2X1_BANK_H2R - MMSE_2X1_BANK_LO][k] = (int32_t)ms_hre[1][l];
                        mleaf[MMSE_2X1_BANK_H2I - MMSE_2X1_BANK_LO][k] = (int32_t)ms_him[1][l];
                        mleaf[MMSE_2X1_BANK_Y1R - MMSE_2X1_BANK_LO][k] = (int32_t)sp[0][2 * ip];
                        mleaf[MMSE_2X1_BANK_Y1I - MMSE_2X1_BANK_LO][k] = (int32_t)sp[0][2 * ip + 1];
                        mleaf[MMSE_2X1_BANK_Y2R - MMSE_2X1_BANK_LO][k] = (int32_t)sp[1][2 * ip];
                        mleaf[MMSE_2X1_BANK_Y2I - MMSE_2X1_BANK_LO][k] = (int32_t)sp[1][2 * ip + 1];
                    } else {
                        for (int j = 0; j < MBANKS; j++) mleaf[j][k] = 0;
                    }
                }
                mmse_dma((uintptr_t)&mleaf[0][0],
                         GPCP_SPAD_IDX(MMSE_2X1_BANK_LO, 0), MBANKS * 16, 0);
                mmse_run();
                /* 直接从 spad 主机窗口读汇：MMIO 非缓存，且 mmse_run 已等到 DONE，
                 * 绕开 "DMA 写 RAM -> CPU 命中旧 D$ 行" 的异步清空竞态。 */
                for (int k = 0; k < n; k++) {
                    int64_t br = ((int64_t)(int32_t)mmio_r(
                                      CGRA_BASE + GPCP_SPAD_ADDR(MMSE_2X1_BANK_NUMR, k)))
                                 << FRONT_GAIN;
                    int64_t bi = ((int64_t)(int32_t)mmio_r(
                                      CGRA_BASE + GPCP_SPAD_ADDR(MMSE_2X1_BANK_NUMI, k)))
                                 << FRONT_GAIN;
                    if (l == 0) { b0r_a[base + k] = br; b0i_a[base + k] = bi; }
                    else        { b1r_a[base + k] = br; b1i_a[base + k] = bi; }
                }
            }
        }
    }

    for (int i = 0; i < NDATA; i++) {
        int r = dre[i];
        uint32_t ip = rev4(BIN0 + (uint32_t)r);
        int64_t y0r = (int64_t)sp[0][2 * ip] << FRONT_GAIN;
        int64_t y0i = (int64_t)sp[0][2 * ip + 1] << FRONT_GAIN;
        int64_t y1r = (int64_t)sp[1][2 * ip] << FRONT_GAIN;
        int64_t y1i = (int64_t)sp[1][2 * ip + 1] << FRONT_GAIN;

        int64_t b0r, b0i, b1r, b1i;
        if (cg_ok) {
            b0r = b0r_a[i]; b0i = b0i_a[i]; b1r = b1r_a[i]; b1i = b1i_a[i];
        } else {                              /* 量纲越界时回退到纯 C，保证仍可诊断 */
            b0r = cm_re(ms_hre[0][0], -ms_him[0][0], y0r, y0i)
                + cm_re(ms_hre[1][0], -ms_him[1][0], y1r, y1i);
            b0i = cm_im(ms_hre[0][0], -ms_him[0][0], y0r, y0i)
                + cm_im(ms_hre[1][0], -ms_him[1][0], y1r, y1i);
            b1r = cm_re(ms_hre[0][1], -ms_him[0][1], y0r, y0i)
                + cm_re(ms_hre[1][1], -ms_him[1][1], y1r, y1i);
            b1i = cm_im(ms_hre[0][1], -ms_him[0][1], y0r, y0i)
                + cm_im(ms_hre[1][1], -ms_him[1][1], y1r, y1i);
        }

        /* num0 = b0*g11 - b1*g01 ; num1 = b1*g00 - b0*g10 */
        int64_t g10r = ms_g01r, g10i = -ms_g01i;
        int64_t n0r = b0r * ms_g11r - cm_re(b1r, b1i, ms_g01r, ms_g01i);
        int64_t n0i = b0i * ms_g11r - cm_im(b1r, b1i, ms_g01r, ms_g01i);
        int64_t n1r = b1r * ms_g00r - cm_re(b0r, b0i, g10r, g10i);
        int64_t n1i = b1i * ms_g00r - cm_im(b0r, b0i, g10r, g10i);

        /* s = num * conj(det) / |det|^2 */
        int64_t s0r = cm_re(n0r, n0i, ms_detr, -ms_deti) / ms_dd;
        int64_t s0i = cm_im(n0r, n0i, ms_detr, -ms_deti) / ms_dd;
        int64_t s1r = cm_re(n1r, n1i, ms_detr, -ms_deti) / ms_dd;
        int64_t s1i = cm_im(n1r, n1i, ms_detr, -ms_deti) / ms_dd;

        uint32_t bits = 0;
        bits |= (s0r < 0) ? 1u : 0u;
        bits |= (s0i < 0) ? 2u : 0u;
        bits |= (s1r < 0) ? 4u : 0u;
        bits |= (s1i < 0) ? 8u : 0u;
        ms_bits[i] = bits;
    }
    return 0;
}
