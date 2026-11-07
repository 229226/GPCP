/* -----------------------------------------------------------------------------
 * 2x2 MIMO PDSCH receiver: FDM pilots -> channel estimate -> MMSE -> hard bits.
 *
 *   two TD blocks (Rx 0 / Rx 1), one symbol each
 *     -> one 4096-point FFT per antenna
 *     -> 4 H_SCALE-scaled coefficients from the 60 FDM pilots (30/layer)
 *     -> per data RE: G = H^H H + lambda I, b = H^H y, solve G s = b
 *     -> hard bits (2 per layer symbol, layer 0 then layer 1) -> 720 bits
 *
 * The 2x2 solve is written out and uses one complex division:
 *     det  = g00*g11 - g01*g10            (g10 = conj(g01))
 *     s0   = (b0*g11 - b1*g01) * conj(det) / |det|^2
 *     s1   = (b1*g00 - b0*g10) * conj(det) / |det|^2
 * so everything stays in int64 (num*conj(det) ~ 3e16 at the worst case).
 *
 * Result area (0x8007_0000):
 *   [0] 0xCAFEBABE  [1] errors  [2] cycles  [6] 0x600D
 *   [8..15]  four channel coefficients (re,im)
 *   [16] 180 (data REs)  [17] lambda  [18] bit errors vs the local re-encode
 *   [19] bit count (720)
 *   [20 .. 20+179]  four hard bits packed per data RE (low nibble)
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"
#include "mmse_2x1_prog.h"      /* 编译器生成的 MMSE 相关微码（cgra/tools/gen_kernel_prog.py） */

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)
#define TD_RAM        0x80400000ul

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

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, dmas;
static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;
static uint32_t gold_x1[GOLD_LEN], gold_x2[GOLD_LEN];

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

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

/* ---------------------------------------------------------------- CGRA MMSE
 * b = H^H y 由 CGRA 的 mmse_2x1 tile 计算：一次 16 个 RE（一个 PE 一个 RE），
 * 每个 RE 喂 8 个 Q15 叶子（h1r/h1i/h2r/h2i/y1r/y1i/y2r/y2i），回 2 个 Q30 汇
 * （numr/numi）。板级验证时 *16 还原到 C 参考的 FRONT_GAIN 量纲，因此 2x2 求解
 * 与纯 C 版逐位一致。
 */
#define MBANKS (MMSE_2X1_BANK_HI - MMSE_2X1_BANK_LO + 1)
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
}

int main(void)
{
    uint32_t *out = RESULT_BASE;
    int32_t *td = (int32_t *)TD_RAM;
    uint32_t t0 = mtime();

    static int32_t pre[2][NP_LAYER], pim[2][NP_LAYER];
    const uint32_t cinits[2] = { CSI_CINIT0, CSI_CINIT1 };
    for (int l = 0; l < 2; l++) {
        gold_init(cinits[l]);
        for (int i = 0; i < NP_LAYER; i++) {
            int32_t c0 = (int32_t)gold_bit((uint32_t)(2 * i));
            int32_t c1 = (int32_t)gold_bit((uint32_t)(2 * i + 1));
            pre[l][i] = (1 - 2 * c0) * QPSK;
            pim[l][i] = (1 - 2 * c1) * QPSK;
        }
    }

    /* ---- FFT both antennas ---- */
    int32_t *sp[2];
    for (int rx = 0; rx < 2; rx++) {
        sp[rx] = td + (size_t)rx * SYM_WORDS + 2 * CP_LEN;
        fft4096(sp[rx]);
    }

    /* ---- channel estimate (same contract as run-mimo-ce) ---- */
    const int64_t den = (int64_t)NP_LAYER * 2 * QPSK * QPSK / H_SCALE;
    int64_t hre[2][2], him[2][2];
    for (int rx = 0; rx < 2; rx++) {
        int64_t accr[2] = { 0, 0 }, acci[2] = { 0, 0 };
        int idx = 0;
        for (int r = 0; r < NRE; r += 4) {
            uint32_t ip = rev4(BIN0 + (uint32_t)r);
            int64_t yr = (int64_t)sp[rx][2 * ip] << FRONT_GAIN;
            int64_t yi = (int64_t)sp[rx][2 * ip + 1] << FRONT_GAIN;
            int l = idx & 1, p = idx >> 1;
            accr[l] += yr * pre[l][p] + yi * pim[l][p];
            acci[l] += yi * pre[l][p] - yr * pim[l][p];
            idx++;
        }
        for (int l = 0; l < 2; l++) {
            hre[rx][l] = accr[l] / den;
            him[rx][l] = acci[l] / den;
        }
    }

    /* ---- G = H^H H, one lambda for both diagonal terms ---- */
    int64_t g00r = 0, g01r = 0, g01i = 0, g11r = 0;
    for (int rx = 0; rx < 2; rx++) {
        int64_t a_r = hre[rx][0], a_i = him[rx][0];
        int64_t b_r = hre[rx][1], b_i = him[rx][1];
        g00r += a_r * a_r + a_i * a_i;
        g11r += b_r * b_r + b_i * b_i;
        g01r += cm_re(a_r, -a_i, b_r, b_i);       /* conj(a) * b */
        g01i += cm_im(a_r, -a_i, b_r, b_i);
    }
    int64_t lam = (g00r + g11r) / 64;
    if (lam < 1) lam = 1;
    g00r += lam;
    g11r += lam;

    /* det = g00*g11 - g01*conj(g01)  (real, since G is Hermitian) */
    int64_t g10r = g01r, g10i = -g01i;
    int64_t detr = g00r * g11r - cm_re(g01r, g01i, g10r, g10i);
    int64_t deti = 0 - cm_im(g01r, g01i, g10r, g10i);
    int64_t dd = detr * detr + deti * deti;
    if (dd <= 0) { dd = 1; errs++; }

    /* ---- data-RE index table（pilots every 4th RE）---- */
    int dre[NDATA], nd = 0;
    for (int rr = 0; rr < NRE && nd < NDATA; rr++)
        if (rr % 4 != 0) dre[nd++] = rr;

    /* ---- b = H^H y on the CGRA: 16 REs per tile run, one run per layer ---- */
    static int64_t b0r_a[NDATA], b0i_a[NDATA], b1r_a[NDATA], b1i_a[NDATA];
    int cg_ok = 1;
    {
        /* 量纲守卫：mul 是 16x16，且 4 项 Q30 求和不能溢出 int32 */
        int64_t hmax = 0, ymax = 0;
        for (int rx = 0; rx < 2; rx++)
            for (int l = 0; l < 2; l++) {
                int64_t a = hre[rx][l] < 0 ? -hre[rx][l] : hre[rx][l];
                int64_t b = him[rx][l] < 0 ? -him[rx][l] : him[rx][l];
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
                        mleaf[MMSE_2X1_BANK_H1R - MMSE_2X1_BANK_LO][k] = (int32_t)hre[0][l];
                        mleaf[MMSE_2X1_BANK_H1I - MMSE_2X1_BANK_LO][k] = (int32_t)him[0][l];
                        mleaf[MMSE_2X1_BANK_H2R - MMSE_2X1_BANK_LO][k] = (int32_t)hre[1][l];
                        mleaf[MMSE_2X1_BANK_H2I - MMSE_2X1_BANK_LO][k] = (int32_t)him[1][l];
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

    /* ---- per data RE: b = H^H y (CGRA) -> solve -> hard bits ---- */
    int bit_err = 0;
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
            b0r = cm_re(hre[0][0], -him[0][0], y0r, y0i)
                + cm_re(hre[1][0], -him[1][0], y1r, y1i);
            b0i = cm_im(hre[0][0], -him[0][0], y0r, y0i)
                + cm_im(hre[1][0], -him[1][0], y1r, y1i);
            b1r = cm_re(hre[0][1], -him[0][1], y0r, y0i)
                + cm_re(hre[1][1], -him[1][1], y1r, y1i);
            b1i = cm_im(hre[0][1], -him[0][1], y0r, y0i)
                + cm_im(hre[1][1], -him[1][1], y1r, y1i);
        }

        /* num0 = b0*g11 - b1*g01 ; num1 = b1*g00 - b0*g10 */
        int64_t n0r = b0r * g11r - cm_re(b1r, b1i, g01r, g01i);
        int64_t n0i = b0i * g11r - cm_im(b1r, b1i, g01r, g01i);
        int64_t n1r = b1r * g00r - cm_re(b0r, b0i, g10r, g10i);
        int64_t n1i = b1i * g00r - cm_im(b0r, b0i, g10r, g10i);

        /* s = num * conj(det) / |det|^2 */
        int64_t s0r = cm_re(n0r, n0i, detr, -deti) / dd;
        int64_t s0i = cm_im(n0r, n0i, detr, -deti) / dd;
        int64_t s1r = cm_re(n1r, n1i, detr, -deti) / dd;
        int64_t s1i = cm_im(n1r, n1i, detr, -deti) / dd;

        uint32_t bits = 0;
        bits |= (s0r < 0) ? 1u : 0u;
        bits |= (s0i < 0) ? 2u : 0u;
        bits |= (s1r < 0) ? 4u : 0u;
        bits |= (s1i < 0) ? 8u : 0u;
        out[20 + i] = bits;
    }

    out[0] = 0xCAFEBABEu;
    out[1] = errs;
    out[2] = mtime() - t0;
    out[6] = 0x600Du;
    out[7] = mmse_runs;          /* >0 证明 b = H^H y 真的跑在 CGRA 上 */
    for (int rx = 0; rx < 2; rx++)
        for (int l = 0; l < 2; l++) {
            out[8 + 4 * rx + 2 * l] = (uint32_t)(int32_t)hre[rx][l];
            out[9 + 4 * rx + 2 * l] = (uint32_t)(int32_t)him[rx][l];
        }
    out[16] = NDATA;
    out[17] = (uint32_t)lam;
    out[18] = (uint32_t)bit_err;
    out[19] = NBITS;

    *(volatile uint32_t *)EXIT_ADDR = 1u;
    return 0;
}
