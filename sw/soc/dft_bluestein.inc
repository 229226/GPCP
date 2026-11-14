/* -----------------------------------------------------------------------------
 * Bluestein DFT/IDFT（整数，复用 ssb_fft.inc 的 1024 点 radix-4 阵列引擎）
 *
 * 依赖：内容包括 ssb_fft.inc（fft_n/rev4n/FFT_LUT_1024）、CGRA_BASE、desc[]；
 *       调用方在 include 前定义 DFT_N（200 或 360）。
 *
 * 变换定义与现有标量 DFT 一致：
 *   正向 X_k = (1/N) Σ x_n e^{-j2πkn/N}
 *   反向 x_n = (1/N) Σ X_k e^{+j2πkn/N}
 * 输出缩放由 setup 时对旧标量函数做一次冲激校准得到，保证下游 LLR 尺度不变。
 *
 * 内存（固定 RAM，避开 .bss/结果区/FFT 数据区）：
 *   0x80310000  a[2*M]   （M=1024）
 *   0x80320000  B_pos[2*M]（FFT(chirp)）
 *   0x80330000  B_neg[2*M]（FFT(conj(chirp))）
 * -------------------------------------------------------------------------- */
#ifndef GPCP_DFT_BLUESTEIN_INC
#define GPCP_DFT_BLUESTEIN_INC

#ifndef DFT_N
#error "dft_bluestein.inc 需要先定义 DFT_N = 200 或 360"
#endif

#include "dft_bluestein_tab.h"

#define DFT_M       1024u
#define DFT_LOG4M   5u
#define DFT_A_RAM   0x80310000ul
#define DFT_BP_RAM  0x80320000ul
#define DFT_BN_RAM  0x80330000ul
#define DFT_TMP_RAM 0x80340000ul

#if DFT_N == 200
#define DFT_COS dft_chirp200_cos
#define DFT_SIN dft_chirp200_sin
#elif DFT_N == 360
#define DFT_COS dft_chirp360_cos
#define DFT_SIN dft_chirp360_sin
#else
#error "DFT_N 只支持 200 / 360"
#endif

static int32_t *dft_a  = (int32_t *)DFT_A_RAM;
static int32_t *dft_bp = (int32_t *)DFT_BP_RAM;
static int32_t *dft_bn = (int32_t *)DFT_BN_RAM;
static int32_t *dft_tmp = (int32_t *)DFT_TMP_RAM;
static uint32_t dft_scale_fwd_q16 = 1u << 16;
static uint32_t dft_scale_inv_q16 = 1u << 16;
static uint32_t dft_shift_fwd_cal = 17u, dft_shift_inv_cal = 17u;
static uint32_t dft_gain_fwd_cal, dft_gain_inv_cal;
static uint32_t dft_raw_shift = 17u, dft_raw_gain;
static uint32_t dft_ready;

/* FFT 引擎的输入是 int16；按本次输入幅度选一个右移，避免蝶形溢出。 */
static uint32_t dft_input_shift(const int32_t *ar, const int32_t *ai)
{
    uint32_t i, mx = 0, sh = 15;
    for (i = 0; i < DFT_N; i++) {
        int32_t x = ar[i] < 0 ? -ar[i] : ar[i];
        int32_t y = ai[i] < 0 ? -ai[i] : ai[i];
        if ((uint32_t)x > mx) mx = (uint32_t)x;
        if ((uint32_t)y > mx) mx = (uint32_t)y;
    }
    /* CGRA 的 MUL 是 sx16(a)*sx16(b)：radix-4 蝶形先做 4 路求和，
     * 求和结果必须仍能放进 int16，所以输入上限取 32767/4 ≈ 8000。 */
    while (sh < 29u && (mx >> (sh - 15u)) > 7000u)
        sh++;
    return sh;
}

/* 一次 Bluestein 卷积；inverse=0 正向，1 反向。输出未做最终缩放校准。 */
static void dft_bluestein_raw(const int32_t *ar, const int32_t *ai,
                              int32_t *out, int inverse)
{
    uint32_t i, k, p;
    const int16_t *co = DFT_COS;
    const int16_t *si = DFT_SIN;
    int32_t *B = inverse ? dft_bn : dft_bp;
    uint32_t sh = dft_input_shift(ar, ai);

    dft_raw_shift = sh;
    for (i = 0; i < 2u * DFT_M; i++)
        dft_a[i] = 0;

    for (k = 0; k < DFT_N; k++) {
        int64_t xr = ar[k], xi = ai[k];
        int64_t c = co[k], s = si[k];
        if (!inverse) {
            /* x * conj(chirp) = (xr*c + xi*s) + j(xi*c - xr*s) */
            dft_a[2 * k]     = (int32_t)((xr * c + xi * s) >> sh);
            dft_a[2 * k + 1] = (int32_t)((xi * c - xr * s) >> sh);
        } else {
            /* x * chirp = (xr*c - xi*s) + j(xi*c + xr*s) */
            dft_a[2 * k]     = (int32_t)((xr * c - xi * s) >> sh);
            dft_a[2 * k + 1] = (int32_t)((xi * c + xr * s) >> sh);
        }
    }

    fft_n(dft_a, DFT_M, FFT_LUT_1024);
    rt_dcache_flush();               /* 引擎写 RAM，CPU 读前必须失效 D$ */

    /* 点积 A*B 先按幅度选右移，避免固定 >>15 把小幅值量化掉。 */
    {
        int64_t maxp = 0;
        uint32_t shp = 15;
        for (p = 0; p < DFT_M; p++) {
            int64_t ar_ = dft_a[2 * p], ai_ = dft_a[2 * p + 1];
            int64_t br = B[2 * p], bi = B[2 * p + 1];
            int64_t pr = ar_ * br - ai_ * bi;
            int64_t pi = ar_ * bi + ai_ * br;
            if (pr < 0) pr = -pr;
            if (pi < 0) pi = -pi;
            if (pr > maxp) maxp = pr;
            if (pi > maxp) maxp = pi;
        }
        /* 同上：点乘结果也要先缩到 int16 安全区，再送第二次 FFT。 */
        while (shp > 0 && (maxp >> shp) < 4000)
            shp--;
        while ((maxp >> shp) > 7500)
            shp++;
        for (p = 0; p < DFT_M; p++) {
            int64_t ar_ = dft_a[2 * p], ai_ = dft_a[2 * p + 1];
            int64_t br = B[2 * p], bi = B[2 * p + 1];
            int64_t pr = ar_ * br - ai_ * bi;
            int64_t pi = ar_ * bi + ai_ * br;
            dft_a[2 * p]     = (int32_t)(pr >> shp);
            dft_a[2 * p + 1] = (int32_t)(pi >> shp);
        }
        dft_raw_gain = 15u - shp;
    }

    /* A/B 都在 digit-reversed 频率序；送进下一次 FFT 前必须回到自然序。 */
    for (k = 0; k < DFT_M; k++) {
        p = rev4n(k, DFT_LOG4M);
        dft_tmp[2 * k]     = dft_a[2 * p];
        dft_tmp[2 * k + 1] = dft_a[2 * p + 1];
    }
    for (i = 0; i < 2u * DFT_M; i++)
        dft_a[i] = dft_tmp[i];

    /* IFFT(P) = conj(FFT(conj(P))) */
    for (p = 0; p < DFT_M; p++)
        dft_a[2 * p + 1] = -dft_a[2 * p + 1];
    fft_n(dft_a, DFT_M, FFT_LUT_1024);
    rt_dcache_flush();
    for (p = 0; p < DFT_M; p++)
        dft_a[2 * p + 1] = -dft_a[2 * p + 1];

    /* 卷积结果 c 可能很小；左移后再乘 chirp，减少 Q15 量化损失。 */
    {
        int32_t maxc = 0;
        uint32_t sc = 0;
        for (p = 0; p < DFT_M; p++) {
            int32_t vr = dft_a[2 * p], vi = dft_a[2 * p + 1];
            if (vr < 0) vr = -vr;
            if (vi < 0) vi = -vi;
            if (vr > maxc) maxc = vr;
            if (vi > maxc) maxc = vi;
        }
        while (maxc > 0 && maxc < 12000 && sc < 4) {
            maxc <<= 1;
            sc++;
        }
        if (sc) {
            for (p = 0; p < DFT_M; p++) {
                dft_a[2 * p]     <<= sc;
                dft_a[2 * p + 1] <<= sc;
            }
        }
        dft_raw_gain += sc;
    }

    for (k = 0; k < DFT_N; k++) {
        int64_t cr, ci;
        p = rev4n(k, DFT_LOG4M);
        cr = dft_a[2 * p];
        ci = dft_a[2 * p + 1];
        int64_t c = co[k], s = si[k];
        if (!inverse) {
            /* c * conj(chirp) */
            out[2 * k]     = (int32_t)((cr * c + ci * s) >> 15);
            out[2 * k + 1] = (int32_t)((ci * c - cr * s) >> 15);
        } else {
            /* c * chirp */
            out[2 * k]     = (int32_t)((cr * c - ci * s) >> 15);
            out[2 * k + 1] = (int32_t)((ci * c + cr * s) >> 15);
        }
    }
}

static void dft_apply_scale(int32_t *out, int64_t scale_q16)
{
    uint32_t i;
    for (i = 0; i < 2u * DFT_N; i++)
        out[i] = (int32_t)(((int64_t)out[i] * scale_q16) >> 16);
}

/* 用常量输入校准比例：正/反向都只有 DC（或 x[0]）非零，期望值就是 A。
 * 这样不需要跑旧的 O(N²) 参考，setup 只花 2 次 FFT + 2 次 raw。 */
static void dft_bluestein_calibrate(int inverse)
{
    static int32_t xr[DFT_N], xi[DFT_N], gb[2 * DFT_N];
    uint32_t i;
    int32_t g;
    int64_t q;

    for (i = 0; i < DFT_N; i++) {
        xr[i] = 1 << 16;
        xi[i] = 0;
    }
    dft_bluestein_raw(xr, xi, gb, inverse);
    g = gb[0];
    if (g == 0)
        return;
    q = ((int64_t)(1 << 16) << 16) / g;
    if (q <= 0 || q > 0xFFFFFFFFll)
        q = 1 << 16;
    if (inverse) {
        dft_scale_inv_q16 = (uint32_t)q;
        dft_shift_inv_cal = dft_raw_shift;
        dft_gain_inv_cal = dft_raw_gain;
    } else {
        dft_scale_fwd_q16 = (uint32_t)q;
        dft_shift_fwd_cal = dft_raw_shift;
        dft_gain_fwd_cal = dft_raw_gain;
    }
}

static void dft_bluestein_setup(void)
{
    uint32_t m;
    const int16_t *co = DFT_COS;
    const int16_t *si = DFT_SIN;

    if (dft_ready)
        return;

    for (m = 0; m < 2u * DFT_M; m++)
        dft_bp[m] = dft_bn[m] = 0;
    /* B 表的 FFT 也走 int16 蝶形：chirp 先缩 1/4，保证 4 路求和
     * 仍在 int16 内（比例由后面的冲激校准自动补回）。 */
    dft_bp[0] = dft_bn[0] = 32767 >> 2;

    for (m = 1; m < DFT_N; m++) {
        int32_t c = co[m] >> 2, s = si[m] >> 2;
        dft_bp[2 * m] = c;
        dft_bp[2 * m + 1] = s;
        dft_bn[2 * m] = c;
        dft_bn[2 * m + 1] = -s;
        dft_bp[2 * (DFT_M - m)] = c;
        dft_bp[2 * (DFT_M - m) + 1] = s;
        dft_bn[2 * (DFT_M - m)] = c;
        dft_bn[2 * (DFT_M - m) + 1] = -s;
    }

    fft_n(dft_bp, DFT_M, FFT_LUT_1024);
    fft_n(dft_bn, DFT_M, FFT_LUT_1024);
    rt_dcache_flush();

    dft_bluestein_calibrate(0);
    dft_bluestein_calibrate(1);
    dft_ready = 1;
}

static void dft_bluestein_fwd(const int32_t *ar, const int32_t *ai, int32_t *out)
{
    int extra;
    int64_t sc;
    dft_bluestein_raw(ar, ai, out, 0);
    extra = (int)dft_raw_shift - (int)dft_shift_fwd_cal
          + (int)dft_gain_fwd_cal - (int)dft_raw_gain;
    sc = (int64_t)dft_scale_fwd_q16;
    if (extra > 0) sc <<= extra; else if (extra < 0) sc >>= -extra;
    dft_apply_scale(out, sc);
}

static void dft_bluestein_inv(const int32_t *ar, const int32_t *ai, int32_t *out)
{
    int extra;
    int64_t sc;
    dft_bluestein_raw(ar, ai, out, 1);
    extra = (int)dft_raw_shift - (int)dft_shift_inv_cal
          + (int)dft_gain_inv_cal - (int)dft_raw_gain;
    sc = (int64_t)dft_scale_inv_q16;
    if (extra > 0) sc <<= extra; else if (extra < 0) sc >>= -extra;
    dft_apply_scale(out, sc);
}

#endif /* GPCP_DFT_BLUESTEIN_INC */
