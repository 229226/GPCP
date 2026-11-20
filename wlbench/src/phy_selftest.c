/* wl_phy 自检：证明物理层运算在数值上是对的，不是"跑起来就算过"
 *
 *   1. FFT 与朴素 DFT 对拍（复数，正/反变换）
 *   2. 四个星座的平均符号功率归一化
 *   3. 无编码 QPSK 在 AWGN 下的 BER 与理论 Q 函数对照
 *   4. 完整链路 BLER 随 SNR 单调下降，且两端饱和
 *   5. 编码增益：同 SNR 下 rate-1/2 链路明显优于未编码
 */
#include <stdio.h>
#include <string.h>

#include "wl_phy.h"
#include "wl_math.h"

static int g_fail;

static void check(const char *name, int ok, const char *fmt, double a, double b)
{
    printf("[%s] %-34s", ok ? " OK " : "FAIL", name);
    if (fmt) printf(fmt, a, b);
    printf("\n");
    if (!ok) g_fail++;
}

/* Abramowitz-Stegun 7.1.26，误差 < 1.5e-7 */
static double erfc_approx(double x)
{
    double t, p, e;
    if (x < 0.0) return 2.0 - erfc_approx(-x);
    t = 1.0 / (1.0 + 0.3275911 * x);
    p = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 +
        t * (-1.453152027 + t * 1.061405429))));
    e = wl_exp(-x * x) * p;
    return e;
}

/* ---------------------------------------------------------------- 1. FFT */
static void test_fft(void)
{
    enum { N = 32 };
    wl_ph_c x[N], ref[N];
    double maxerr = 0.0;
    int i, k;

    for (i = 0; i < N; i++) {
        x[i].re = wl_sin(0.37 * (double)i) + 0.3 * wl_cos(0.11 * (double)i);
        x[i].im = wl_cos(0.23 * (double)i) - 0.2 * wl_sin(0.07 * (double)i);
        ref[i] = x[i];
    }
    wl_phy_fft(x, N, 0);
    for (k = 0; k < N; k++) {
        double re = 0.0, im = 0.0;
        for (i = 0; i < N; i++) {
            double a = -2.0 * WL_PI * (double)((i * k) % N) / (double)N;
            double c = wl_cos(a), s = wl_sin(a);
            re += ref[i].re * c - ref[i].im * s;
            im += ref[i].re * s + ref[i].im * c;
        }
        {
            double dr = x[k].re - re, di = x[k].im - im;
            double e = wl_sqrt(dr * dr + di * di);
            if (e > maxerr) maxerr = e;
        }
    }
    check("FFT 32 点 vs 朴素 DFT", maxerr < 1e-9, "max|d| = %.3e (阈值 %.0e)",
          maxerr, 1e-9);

    wl_phy_fft(x, N, 1);
    maxerr = 0.0;
    for (i = 0; i < N; i++) {
        double dr = x[i].re - ref[i].re, di = x[i].im - ref[i].im;
        double e = wl_sqrt(dr * dr + di * di);
        if (e > maxerr) maxerr = e;
    }
    check("IFFT 往返", maxerr < 1e-9, "max|d| = %.3e (阈值 %.0e)", maxerr, 1e-9);
}

/* ---------------------------------------------------------------- 2. 星座 */
static void test_constellation(void)
{
    static const int qms[4] = {2, 4, 6, 8};
    int qi;
    for (qi = 0; qi < 4; qi++) {
        int qm = qms[qi];
        wl_ph_c sym[256];
        uint8_t bits[256 * 8];
        double p = 0.0;
        int i, nb;
        for (i = 0; i < 256; i++) {
            int j;
            for (j = 0; j < qm; j++) bits[i * qm + j] = (uint8_t)((i >> j) & 1);
        }
        nb = 256 * qm;
        wl_phy_map(bits, nb, qm, sym);
        for (i = 0; i < nb / qm; i++) p += sym[i].re * sym[i].re + sym[i].im * sym[i].im;
        p /= (double)(nb / qm);
        check(qm == 2 ? "星座功率 QPSK" : qm == 4 ? "星座功率 16QAM" :
              qm == 6 ? "星座功率 64QAM" : "星座功率 256QAM",
              p > 0.98 && p < 1.02, "E|s|^2 = %.4f (期望 1.000)", p, 1.0);
    }
}

/* ---------------------------------------------------------------- 3. 未编码 BER */
static void test_awgn_ber(void)
{
    enum { NSYM = 20000 };
    wl_ph_c sym[NSYM];
    uint8_t bits[NSYM * 2];
    int16_t llr[NSYM * 2];
    static const double snrs[3] = {0.0, 4.0, 8.0};
    uint32_t st = 12345u;
    int t;

    for (t = 0; t < 3; t++) {
        double snr = snrs[t];
        double sigma2 = 1.0 / wl_pow10(snr / 10.0);
        double var = sigma2 * 0.5;
        double err = 0.0, the;
        int i;

        for (i = 0; i < NSYM * 2; i++) {
            st = st * 1664525u + 1013904223u;
            bits[i] = (uint8_t)((st >> 16) & 1u);
        }
        wl_phy_map(bits, NSYM * 2, 2, sym);
        for (i = 0; i < NSYM; i++) {
            /* Box-Muller 用同一 LCG，够用且确定 */
            double u1, u2, r, a, n1, n2;
            st = st * 1664525u + 1013904223u; u1 = (double)(st >> 8) / 16777216.0;
            st = st * 1664525u + 1013904223u; u2 = (double)(st >> 8) / 16777216.0;
            if (u1 < 1e-9) u1 = 1e-9;
            r = wl_sqrt(-2.0 * wl_log(u1));
            a = 2.0 * WL_PI * u2;
            n1 = r * wl_cos(a) * wl_sqrt(var);
            n2 = r * wl_sin(a) * wl_sqrt(var);
            sym[i].re += n1;
            sym[i].im += n2;
        }
        wl_phy_demap(sym, NSYM, 2, sigma2, llr);
        for (i = 0; i < NSYM * 2; i++) {
            int hard = llr[i] < 0 ? 1 : 0;
            if (hard != bits[i]) err += 1.0;
        }
        err /= (double)(NSYM * 2);
        the = 0.5 * erfc_approx(wl_sqrt(1.0 / (2.0 * sigma2)));
        check(t == 0 ? "QPSK BER @ 0 dB" : t == 1 ? "QPSK BER @ 4 dB"
                                                  : "QPSK BER @ 8 dB",
              err < the * 1.25 + 2e-4 && err > the * 0.6 - 2e-4,
              "实测 %.4f / 理论 %.4f", err, the);
    }
}

/* ---------------------------------------------------------------- 4/5. 链路 */
static void test_link(void)
{
    static wl_phy_ctx ctx;
    wl_phy_cfg c;
    static const double snrs[9] = {-2.0, 0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0};
    double bler[9];
    int i, t, mono = 1;

    memset(&c, 0, sizeof(c));
    c.enable = 1;
    c.max_prb = 8;
    c.target_coded = 2048;
    c.n_sym = 14;
    c.ce_mode = 1;
    c.ds_ns = 500.0;
    c.speed_kmh = 3.0;
    c.fc_ghz = 3.5;
    c.scs_khz = 15.0;

    wl_phy_init(&ctx, &c);

    printf("\n  SNR(dB)  BLER      raw-BER      decoded-BER   EVM(dB)   CE-NMSE(dB)\n");
    for (i = 0; i < 9; i++) {
        int n = 40, ok = 0;
        int64_t e0 = ctx.st.dec_err, b0 = ctx.st.dec_bits;
        int64_t r0 = ctx.st.raw_err, rb0 = ctx.st.raw_bits;
        double ev0 = ctx.st.evm_acc, nm0 = ctx.st.nmse_acc;
        int64_t evn0 = ctx.st.evm_n, nmn0 = ctx.st.nmse_n;
        for (t = 0; t < n; t++)
            ok += wl_phy_link(&ctx, &c, snrs[i], 2, 0.5, 1000u + (uint32_t)t);
        bler[i] = 1.0 - (double)ok / (double)n;
        {
            int64_t de = ctx.st.dec_err - e0, db = ctx.st.dec_bits - b0;
            int64_t re = ctx.st.raw_err - r0, rbb = ctx.st.raw_bits - rb0;
            int64_t evn = ctx.st.evm_n - evn0, nmn = ctx.st.nmse_n - nmn0;
            double ber = db > 0 ? (double)de / (double)db : 0.0;
            double rber = rbb > 0 ? (double)re / (double)rbb : 0.0;
            double evm = evn > 0 ? 20.0 * wl_log10((ctx.st.evm_acc - ev0) / (double)evn) : -99.0;
            double nmse = nmn > 0 ? 10.0 * wl_log10((ctx.st.nmse_acc - nm0) / (double)nmn) : -99.0;
            printf("  %6.1f   %6.3f   %9.2e   %9.2e     %7.2f   %8.2f\n",
                   snrs[i], bler[i], rber, ber, evm, nmse);
        }
        if (i > 0 && bler[i] > bler[i - 1] + 1e-9) mono = 0;
    }

    check("BLER 随 SNR 单调下降", mono, "（9 个点）", 0.0, 0.0);
    check("低 SNR 全失败", bler[0] > 0.95, "BLER(-2dB) = %.3f", bler[0], 0.0);
    check("高 SNR 全通过", bler[8] < 0.02, "BLER(14dB) = %.3f", bler[8], 0.0);

    /* 编码增益：6 dB 上未编码 QPSK 还有 ~2% 误比特，编码链路应全对 */
    {
        int n = 200, ok = 0;
        int64_t de0 = ctx.st.dec_err, db0 = ctx.st.dec_bits;
        double ber, unc;
        for (t = 0; t < n; t++)
            ok += wl_phy_link(&ctx, &c, 6.0, 2, 0.5, 7000u + (uint32_t)t);
        ber = (double)(ctx.st.dec_err - de0) / (double)(ctx.st.dec_bits - db0);
        unc = 0.5 * erfc_approx(wl_sqrt(1.0 / (2.0 / wl_pow10(0.6))));
        check("编码增益 @6dB", ber < 1e-3 && (double)ok > 0.95 * (double)n,
              "post-FEC %.1e vs 未编码 %.1e", ber, unc);
    }
}

int main(void)
{
    printf("wl_phy selftest\n");
    printf("------------ 1. 变换 ------------\n");
    test_fft();
    printf("------------ 2. 星座 ------------\n");
    test_constellation();
    printf("------------ 3. AWGN BER --------\n");
    test_awgn_ber();
    printf("------------ 4. 完整链路 --------\n");
    test_link();
    printf("--------------------------------\n");
    printf("%s\n", g_fail ? "SELFTEST FAIL" : "SELFTEST PASS");
    return g_fail ? 1 : 0;
}
