/* 无 libm 的确定性数学函数（纯 C11，double）
 *
 * 只实现协议模型真正用到的函数：sqrt / exp / log / log10 / pow10 /
 * sin / cos / 整数幂。全部用加减乘除与循环实现，不依赖数学库、不依赖
 * 字节序，因此同一份源码在主机与 GPCP 上语义一致（精度以内）。
 */
#ifndef WLBENCH_MATH_H
#define WLBENCH_MATH_H

#define WL_PI       3.14159265358979323846
#define WL_LN2      0.69314718055994530942
#define WL_LN10     2.30258509299404568402
#define WL_INV_LN2  1.44269504088896340736
#define WL_INV_LN10 0.43429448190325182765
#define WL_2_OVER_PI 0.63661977236758134308

static inline double wl_abs(double x)
{
    return x < 0.0 ? -x : x;
}

static inline double wl_sqrt(double x)
{
    double g;
    int i;
    if (x <= 0.0) return 0.0;
    g = x > 1.0 ? x : 1.0;
    for (i = 0; i < 48; i++)
        g = 0.5 * (g + x / g);
    return g;
}

static inline double wl_scale2(double x, int k)
{
    while (k > 0) { x *= 2.0; k--; }
    while (k < 0) { x *= 0.5; k++; }
    return x;
}

/* exp(x)：范围归约到 [-ln2/2, ln2/2] 后做 18 项 Taylor。 */
static inline double wl_exp(double x)
{
    double r, term, sum;
    int k, i;
    if (x > 700.0) x = 700.0;
    if (x < -700.0) x = -700.0;
    k = (int)(x * WL_INV_LN2 + (x >= 0.0 ? 0.5 : -0.5));
    r = x - (double)k * WL_LN2;
    sum = 1.0;
    term = 1.0;
    for (i = 1; i <= 18; i++) {
        term *= r / (double)i;
        sum += term;
    }
    return wl_scale2(sum, k);
}

/* log(x)：先规格化到 [1,2)，再用 atanh 级数。 */
static inline double wl_log(double x)
{
    double z, z2, term, sum;
    int k, i;
    if (x <= 0.0) return -700.0;
    k = 0;
    while (x >= 2.0) { x *= 0.5; k++; }
    while (x < 1.0)  { x *= 2.0; k--; }
    z = (x - 1.0) / (x + 1.0);
    z2 = z * z;
    term = z;
    sum = 0.0;
    for (i = 1; i <= 31; i += 2) {
        sum += term / (double)i;
        term *= z2;
    }
    return 2.0 * sum + (double)k * WL_LN2;
}

static inline double wl_log10(double x)
{
    return wl_log(x) * WL_INV_LN10;
}

static inline double wl_pow10(double x)
{
    return wl_exp(x * WL_LN10);
}

static inline double wl_exp2(double x)
{
    return wl_exp(x * WL_LN2);
}

/* sin/cos：按 π/2 象限归约，|y| <= π/4 上做 Taylor。 */
static inline void wl_sin_cos(double x, double *s, double *c)
{
    double y, y2, ts, tc, term;
    int k, q;
    k = (int)(x * WL_2_OVER_PI + (x >= 0.0 ? 0.5 : -0.5));
    y = x - (double)k * (WL_PI * 0.5);
    y2 = y * y;

    ts = y;
    term = y;
    for (q = 3; q <= 15; q += 2) {
        term *= -y2 / (double)((q - 1) * q);
        ts += term;
    }

    tc = 1.0;
    term = 1.0;
    for (q = 2; q <= 16; q += 2) {
        term *= -y2 / (double)((q - 1) * q);
        tc += term;
    }

    switch (((k % 4) + 4) % 4) {
    case 0: *s = ts;  *c = tc;  break;
    case 1: *s = tc;  *c = -ts; break;
    case 2: *s = -ts; *c = -tc; break;
    default: *s = -tc; *c = ts; break;
    }
}

static inline double wl_sin(double x)
{
    double s, c;
    wl_sin_cos(x, &s, &c);
    return s;
}

static inline double wl_cos(double x)
{
    double s, c;
    wl_sin_cos(x, &s, &c);
    return c;
}

/* 整数幂（协议里只用到 rank = 1/2）。 */
static inline double wl_pow_int(double b, int n)
{
    double r = 1.0;
    while (n > 0) {
        if (n & 1) r *= b;
        b *= b;
        n >>= 1;
    }
    return r;
}

static inline double wl_floor(double x)
{
    long i = (long)x;
    if ((double)i > x) i--;
    return (double)i;
}

static inline long wl_round_i(double x)
{
    return (long)(x >= 0.0 ? x + 0.5 : x - 0.5);
}

#endif /* WLBENCH_MATH_H */
