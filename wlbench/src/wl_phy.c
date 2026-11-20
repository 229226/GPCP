/* wl_phy —— CP-OFDM 物理层链路，全部是真实基带运算
 *
 * 发端：CRC24A -> 卷积编码(K=7,133/171) -> 速率匹配 -> Gold 加扰
 *       -> QAM 映射(38.211 星座) -> 资源映射 -> IFFT -> 加 CP
 * 信道：TDL 三径（分数时延线性插值）+ 逐径衰落 + AWGN
 * 收端：去 CP -> FFT -> DMRS 最小二乘估计 ->（可选 DFT 截断）
 *       -> MMSE 均衡 -> max-log LLR -> 解扰 -> 解速率匹配
 *       -> Viterbi -> CRC 校验
 *
 * 约束：只用 <stdint.h> 和 wl_math.h；固定容量数组；无 libc/libm。
 */
#include "wl_phy.h"
#include "wl_math.h"

/* ================================================================ 基础 */
static void ph_zero(void *p, uint32_t n)
{
    unsigned char *b = (unsigned char *)p;
    uint32_t i;
    for (i = 0; i < n; i++) b[i] = 0;
}

static wl_ph_c ph_mk(double re, double im)
{
    wl_ph_c z;
    z.re = re;
    z.im = im;
    return z;
}

static wl_ph_c ph_add(wl_ph_c a, wl_ph_c b)
{
    return ph_mk(a.re + b.re, a.im + b.im);
}

static wl_ph_c ph_mul(wl_ph_c a, wl_ph_c b)
{
    return ph_mk(a.re * b.re - a.im * b.im, a.re * b.im + a.im * b.re);
}

static wl_ph_c ph_scale(wl_ph_c a, double s)
{
    return ph_mk(a.re * s, a.im * s);
}

static wl_ph_c ph_conj(wl_ph_c a)
{
    return ph_mk(a.re, -a.im);
}

static double ph_abs2(wl_ph_c a)
{
    return a.re * a.re + a.im * a.im;
}

static int ph_next_pow2(int n)
{
    int p = 64;
    while (p < n) p <<= 1;
    return p > WL_PHY_MAX_FFT ? WL_PHY_MAX_FFT : p;
}

static int ph_log2i(int n)
{
    int k = 0;
    while ((1 << k) < n) k++;
    return k;
}

/* ================================================================ FFT
 * 迭代 radix-2，旋转因子按当前 n 缓存（每换一次点数重算一次）。
 */
static wl_ph_c s_tw[WL_PHY_MAX_FFT / 2];
static int     s_tw_n;

static void ph_tw_init(int n)
{
    int k;
    double step;
    if (s_tw_n == n) return;
    step = -2.0 * WL_PI / (double)n;
    for (k = 0; k < n / 2; k++) {
        double a = step * (double)k;
        s_tw[k] = ph_mk(wl_cos(a), wl_sin(a));
    }
    s_tw_n = n;
}

void wl_phy_fft(wl_ph_c *x, int n, int inverse)
{
    int i, j, len, half, k;
    if (n < 2) return;
    ph_tw_init(n);

    for (i = 1, j = 0; i < n; i++) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            wl_ph_c t = x[i];
            x[i] = x[j];
            x[j] = t;
        }
    }

    for (len = 2; len <= n; len <<= 1) {
        half = len >> 1;
        for (i = 0; i < n; i += len) {
            for (k = 0; k < half; k++) {
                wl_ph_c w = s_tw[k * (n / len)];
                wl_ph_c u = x[i + k];
                wl_ph_c v;
                if (inverse) w.im = -w.im;   /* 反变换用共轭旋转因子 */
                v = ph_mul(x[i + k + half], w);
                x[i + k] = ph_add(u, v);
                x[i + k + half] = ph_mk(u.re - v.re, u.im - v.im);
            }
        }
    }

    if (inverse) {
        double s = 1.0 / (double)n;
        for (i = 0; i < n; i++) x[i] = ph_scale(x[i], s);
    }
}

/* ================================================================ 随机数
 * 局部 PCG32 + Marsaglia 极坐标高斯，不碰 wl_state 的随机流。
 */
typedef struct {
    uint64_t state;
    uint64_t inc;
    double   spare;
    int      has_spare;
} ph_rng;

static void ph_seed(ph_rng *r, uint64_t seed)
{
    r->state = 0;
    r->inc = (seed << 1u) | 1u;
    r->has_spare = 0;
    r->spare = 0.0;
    r->state = r->state * 6364136223846793005ULL + r->inc;
    r->state += seed;
    r->state = r->state * 6364136223846793005ULL + r->inc;
}

static uint32_t ph_u32(ph_rng *r)
{
    uint64_t old = r->state;
    uint32_t xs, rot;
    r->state = old * 6364136223846793005ULL + r->inc;
    xs = (uint32_t)(((old >> 18u) ^ old) >> 27u);
    rot = (uint32_t)(old >> 59u);
    return (xs >> rot) | (xs << ((-rot) & 31u));
}

static double ph_uni(ph_rng *r)
{
    return (double)ph_u32(r) * (1.0 / 4294967296.0);
}

static double ph_gauss(ph_rng *r)
{
    double u, v, s, f;
    if (r->has_spare) {
        r->has_spare = 0;
        return r->spare;
    }
    do {
        u = 2.0 * ph_uni(r) - 1.0;
        v = 2.0 * ph_uni(r) - 1.0;
        s = u * u + v * v;
    } while (s >= 1.0 || s < 1e-12);
    f = wl_sqrt(-2.0 * wl_log(s) / s);
    r->spare = v * f;
    r->has_spare = 1;
    return u * f;
}

/* ================================================================ Gold 序列
 * 38.211 5.2.1：两个 31 级 m 序列逐位异或。
 */
typedef struct {
    uint32_t x1;
    uint32_t x2;
} ph_gold;

static void ph_gold_init(ph_gold *g, uint32_t c_init)
{
    int i;
    g->x1 = 1u;
    g->x2 = c_init & 0x7FFFFFFFu;
    for (i = 0; i < 1600; i++) {
        uint32_t b1 = (g->x1 ^ (g->x1 >> 3)) & 1u;
        uint32_t b2 = (g->x2 ^ (g->x2 >> 1) ^ (g->x2 >> 2) ^ (g->x2 >> 3)) & 1u;
        g->x1 = (g->x1 >> 1) | (b1 << 30);
        g->x2 = (g->x2 >> 1) | (b2 << 30);
    }
}

static uint32_t ph_gold_next(ph_gold *g)
{
    uint32_t b1 = (g->x1 ^ (g->x1 >> 3)) & 1u;
    uint32_t b2 = (g->x2 ^ (g->x2 >> 1) ^ (g->x2 >> 2) ^ (g->x2 >> 3)) & 1u;
    uint32_t out = (g->x1 ^ g->x2) & 1u;
    g->x1 = (g->x1 >> 1) | (b1 << 30);
    g->x2 = (g->x2 >> 1) | (b2 << 30);
    return out;
}

/* ================================================================ 星座
 * 每轴 M-PAM，Gray 映射，总符号功率归一化为 1。
 */
static const double s_norm[5] = {
    0.0,
    1.4142135623730951,     /* QPSK   : sqrt(2)   */
    3.1622776601683795,     /* 16QAM  : sqrt(10)  */
    6.480740698407860,      /* 64QAM  : sqrt(42)  */
    13.038404810405298      /* 256QAM : sqrt(170) */
};

static uint8_t s_ginv[5][16];

static void ph_build_tables(void)
{
    static int ready;
    int m, k, v;
    if (ready) return;
    for (m = 1; m <= 4; m++) {
        for (k = 0; k < (1 << m); k++) {
            s_ginv[m][k] = 0;
            for (v = 0; v < (1 << m); v++)
                if ((v ^ (v >> 1)) == k) s_ginv[m][k] = (uint8_t)v;
        }
    }
    ready = 1;
}

static double ph_axis_amp(int m, int k)
{
    return (2.0 * (double)k - (double)((1 << m) - 1)) / s_norm[m];
}

void wl_phy_map(const uint8_t *bits, int nbits, int qm, wl_ph_c *out)
{
    int m = qm / 2;
    int ns = nbits / qm;
    int i, j;
    ph_build_tables();
    for (i = 0; i < ns; i++) {
        int vi = 0, vq = 0;
        for (j = 0; j < m; j++) vi = (vi << 1) | (bits[i * qm + j] & 1);
        for (j = 0; j < m; j++) vq = (vq << 1) | (bits[i * qm + m + j] & 1);
        out[i] = ph_mk(ph_axis_amp(m, vi ^ (vi >> 1)),
                       ph_axis_amp(m, vq ^ (vq >> 1)));
    }
}

void wl_phy_demap(const wl_ph_c *sym, int nsym, int qm, double sigma2,
                  int16_t *llr)
{
    int m = qm / 2;
    int M = 1 << m;
    int i, j, k;
    double inv = sigma2 > 1e-12 ? 1.0 / sigma2 : 1e12;
    ph_build_tables();

    for (i = 0; i < nsym; i++) {
        double yv[2];
        yv[0] = sym[i].re;
        yv[1] = sym[i].im;
        for (k = 0; k < 2; k++) {
            for (j = 0; j < m; j++) {
                double best0 = 1e30, best1 = 1e30;
                int lv;
                for (lv = 0; lv < M; lv++) {
                    double d = yv[k] - ph_axis_amp(m, lv);
                    int bit;
                    d = d * d;
                    bit = (s_ginv[m][lv] >> (m - 1 - j)) & 1;
                    if (bit == 0) {
                        if (d < best0) best0 = d;
                    } else {
                        if (d < best1) best1 = d;
                    }
                }
                {
                    double v = (best1 - best0) * inv;
                    if (v > 64.0) v = 64.0;
                    if (v < -64.0) v = -64.0;
                    llr[i * qm + k * m + j] = (int16_t)wl_round_i(v * 64.0);
                }
            }
        }
    }
}

void wl_phy_slice(wl_ph_c *v, int qm)
{
    int m = qm / 2;
    int M = 1 << m;
    double nrm = s_norm[m];
    int i;
    for (i = 0; i < 2; i++) {
        double a = (i == 0) ? v->re : v->im;
        int k = (int)wl_round_i((a * nrm + (double)(M - 1)) * 0.5);
        double amp;
        if (k < 0) k = 0;
        if (k > M - 1) k = M - 1;
        amp = ph_axis_amp(m, k);
        if (i == 0) v->re = amp;
        else v->im = amp;
    }
}

/* ================================================================ CRC24A */
static uint32_t ph_crc24(const uint8_t *bits, int n)
{
    uint32_t crc = 0;
    int i;
    for (i = 0; i < n; i++) {
        int fb = (int)((crc >> 23) & 1u) ^ (bits[i] & 1);
        crc = (crc << 1) & 0xFFFFFFu;
        if (fb) crc ^= 0x864CFBu;
    }
    for (i = 0; i < 24; i++) {
        int fb = (int)((crc >> 23) & 1u);
        crc = (crc << 1) & 0xFFFFFFu;
        if (fb) crc ^= 0x864CFBu;
    }
    return crc & 0xFFFFFFu;
}

/* ================================================================ 卷积码
 * K=7, rate 1/2, G0=133, G1=171（WiFi/LTE 广播信道同款）。
 * state 的 bit5 是最新入比特，bit0 是最老。
 */
static uint8_t s_o0[64][2], s_o1[64][2], s_nx[64][2];

static void ph_trellis(void)
{
    static int ready;
    int s, b;
    if (ready) return;
    for (s = 0; s < 64; s++) {
        for (b = 0; b < 2; b++) {
            uint32_t reg = ((uint32_t)b << 6) | (uint32_t)s;
            uint32_t p0 = reg & 0x5Bu;          /* 1011011 */
            uint32_t p1 = reg & 0x79u;          /* 1111001 */
            uint32_t v0 = 0, v1 = 0;
            int i;
            for (i = 0; i < 7; i++) {
                v0 ^= (p0 >> i) & 1u;
                v1 ^= (p1 >> i) & 1u;
            }
            s_o0[s][b] = (uint8_t)v0;
            s_o1[s][b] = (uint8_t)v1;
            s_nx[s][b] = (uint8_t)(((b << 5) | (s >> 1)) & 63);
        }
    }
    ready = 1;
}

static int ph_conv_encode(const uint8_t *in, int n, uint8_t *out)
{
    int s = 0, i, m = 0;
    ph_trellis();
    for (i = 0; i < n + 6; i++) {
        int b = i < n ? (in[i] & 1) : 0;   /* 末尾 6 个零尾比特 */
        out[m++] = s_o0[s][b];
        out[m++] = s_o1[s][b];
        s = s_nx[s][b];
    }
    return m;
}

/* 软判决 Viterbi：输入 LLR（正数偏向 0），输出硬判比特。 */
static void ph_viterbi(const int16_t *llr, int n, uint8_t *out, wl_phy_ctx *p)
{
    int32_t pm[64], npm[64];
    int t, s;

    ph_trellis();
    for (s = 0; s < 64; s++) pm[s] = (s == 0) ? 0 : -1000000000;

    for (t = 0; t < n; t++) {
        int32_t l0 = llr[2 * t];
        int32_t l1 = llr[2 * t + 1];
        uint64_t d = 0;
        for (s = 0; s < 64; s++) {
            int b = (s >> 5) & 1;
            int p0 = (s & 31) << 1;
            int p1 = p0 | 1;
            int32_t m0 = pm[p0] + (s_o0[p0][b] ? -l0 : l0) + (s_o1[p0][b] ? -l1 : l1);
            int32_t m1 = pm[p1] + (s_o0[p1][b] ? -l0 : l0) + (s_o1[p1][b] ? -l1 : l1);
            if (m1 > m0) {
                npm[s] = m1;
                d |= (uint64_t)1 << s;
            } else {
                npm[s] = m0;
            }
        }
        for (s = 0; s < 64; s++) pm[s] = npm[s];
        p->dec[t] = d;
    }

    {
        int cur = 0;   /* 零尾比特保证终止在状态 0 */
        for (t = n - 1; t >= 0; t--) {
            int bit = (cur >> 5) & 1;
            int prev = ((cur & 31) << 1) | (int)((p->dec[t] >> cur) & 1u);
            out[t] = (uint8_t)bit;
            cur = prev;
        }
    }
}

/* ================================================================ 速率匹配
 * 环形缓冲的均匀抽取/重复：目标长度大于编码长度时重复，反之打孔。
 */
static void ph_rate_match(const uint8_t *in, int nin, uint8_t *out, int nout)
{
    int i;
    for (i = 0; i < nout; i++) {
        int j = (int)(((int64_t)i * (int64_t)nin) / (int64_t)nout);
        if (j >= nin) j = nin - 1;
        out[i] = in[j];
    }
}

/* ================================================================ 信道
 * 三径 TDL，分数时延用线性插值落到采样点上。
 */
static int ph_ch_build(wl_ph_c *h, int n_fft, const wl_phy_cfg *c, ph_rng *r)
{
    double fs = (double)n_fft * (c->scs_khz > 0.0 ? c->scs_khz : 15.0) * 1e3;
    double ds = c->ds_ns > 0.0 ? c->ds_ns : 500.0;
    double fd = c->speed_kmh / 3.6 * (c->fc_ghz * 1e9) / 3e8;
    double amp[WL_PHY_TAPS], dly[WL_PHY_TAPS];
    double sum = 0.0;
    int l, L = 1;

    ph_zero(h, (uint32_t)sizeof(wl_ph_c) * WL_PHY_MAX_FFT);

    for (l = 0; l < WL_PHY_TAPS; l++) {
        dly[l] = (double)l * 1.5 * ds;          /* ns */
        amp[l] = wl_exp(-dly[l] / ds);
        sum += amp[l] * amp[l];
    }
    if (sum <= 0.0) sum = 1.0;
    for (l = 0; l < WL_PHY_TAPS; l++) amp[l] /= wl_sqrt(sum);

    for (l = 0; l < WL_PHY_TAPS; l++) {
        double pos = dly[l] * 1e-9 * fs;
        int i0 = (int)pos;
        double frac = pos - (double)i0;
        double ph = 2.0 * WL_PI * ph_uni(r);
        double gain = amp[l] * (0.75 + 0.5 * ph_uni(r));
        wl_ph_c g = ph_mk(gain * wl_cos(ph), gain * wl_sin(ph));
        if (i0 >= WL_PHY_MAX_FFT - 4) i0 = WL_PHY_MAX_FFT - 4;
        h[i0] = ph_add(h[i0], ph_scale(g, 1.0 - frac));
        h[i0 + 1] = ph_add(h[i0 + 1], ph_scale(g, frac));
        if (i0 + 2 > L) L = i0 + 2;
    }
    if (fd > 0.0) {
        double rot = 2.0 * WL_PI * fd * 1e-3;
        wl_ph_c w = ph_mk(wl_cos(rot), wl_sin(rot));
        for (l = 0; l < L; l++) h[l] = ph_mul(h[l], w);
    }
    return L;
}

static void ph_conv_channel(wl_ph_c *x, int n, const wl_ph_c *h, int L,
                            double sigma, ph_rng *r)
{
    int i, l;
    for (i = n - 1; i >= 0; i--) {
        wl_ph_c acc = ph_mk(0.0, 0.0);
        for (l = 0; l < L && l <= i; l++) acc = ph_add(acc, ph_mul(h[l], x[i - l]));
        if (sigma > 0.0) {
            acc.re += sigma * ph_gauss(r);
            acc.im += sigma * ph_gauss(r);
        }
        x[i] = acc;
    }
}

/* ================================================================ 主链路 */
void wl_phy_init(wl_phy_ctx *p, const wl_phy_cfg *c)
{
    ph_zero(p, (uint32_t)sizeof(*p));
    (void)c;
}

int wl_phy_link(wl_phy_ctx *p, const wl_phy_cfg *c, double snr_db,
                int qm, double rate, uint64_t seed)
{
    ph_rng rng;
    ph_gold gold;
    int n_sym, n_data_sym;
    int prb, n_sc, n_fft, off, cp, L, i, k, sym;
    int coded_cap, info_len, enc_len, ns, bitpos, llr_pos;
    int target = c->target_coded > 0 ? c->target_coded : 2048;
    int dmrs_sym[2];
    double snr_lin, nstd, hpow = 0.0, nmse, evm_err = 0.0, evm_ref = 0.0;
    uint32_t crc_tx, crc_rx;
    int ok, dec_err = 0, raw_err = 0, raw_bits = 0;

    ph_build_tables();
    ph_trellis();

    if (qm != 2 && qm != 4 && qm != 6 && qm != 8) qm = 2;
    n_sym = c->n_sym > 0 ? c->n_sym : 14;
    if (n_sym > WL_PHY_MAX_SYM) n_sym = WL_PHY_MAX_SYM;
    if (n_sym < 4) n_sym = 4;
    n_data_sym = n_sym - 2;

    /* 带宽由目标编码比特数反推，保证码率与调度给的 MCS 一致 */
    prb = (target + 12 * n_data_sym * qm - 1) / (12 * n_data_sym * qm);
    if (prb < 1) prb = 1;
    if (c->max_prb > 0 && prb > c->max_prb) prb = c->max_prb;
    if (prb > WL_PHY_MAX_PRB) prb = WL_PHY_MAX_PRB;

    n_sc = prb * 12;
    n_fft = ph_next_pow2(2 * n_sc);
    off = (n_fft - n_sc) / 2;
    cp = n_fft / 8;

    coded_cap = n_sc * n_data_sym * qm;
    if (coded_cap > WL_PHY_MAX_CODED) coded_cap = WL_PHY_MAX_CODED;

    info_len = (int)((double)coded_cap * rate);
    if (info_len > WL_PHY_MAX_INFO - 32) info_len = WL_PHY_MAX_INFO - 32;
    if (info_len < 64) info_len = 64;

    enc_len = 2 * (info_len + 6);
    if (enc_len > WL_PHY_MAX_ENC) enc_len = WL_PHY_MAX_ENC;

    /* ---- 发端：载荷 + CRC ---- */
    ph_seed(&rng, seed);
    for (i = 0; i < info_len - 24; i++) p->info[i] = (uint8_t)(ph_u32(&rng) & 1u);
    crc_tx = ph_crc24(p->info, info_len - 24);
    for (i = 0; i < 24; i++)
        p->info[info_len - 24 + i] = (uint8_t)((crc_tx >> (23 - i)) & 1u);

    ph_conv_encode(p->info, info_len, p->enc);
    ph_rate_match(p->enc, enc_len, p->coded, coded_cap);

    ph_gold_init(&gold, (uint32_t)(seed ^ 0x5A5Au));
    for (i = 0; i < coded_cap; i++) p->coded[i] ^= (uint8_t)ph_gold_next(&gold);

    /* ---- 信道准备 ---- */
    snr_lin = wl_pow10((snr_db - c->noise_fig_db) / 10.0);
    if (snr_lin < 1e-9) snr_lin = 1e-9;

    L = ph_ch_build(p->h_ir, n_fft, c, &rng);

    /* 真实频响：补零后一次 FFT */
    {
        wl_ph_c hh[WL_PHY_MAX_FFT];
        for (i = 0; i < n_fft; i++) hh[i] = i < L ? p->h_ir[i] : ph_mk(0.0, 0.0);
        wl_phy_fft(hh, n_fft, 0);
        for (i = 0; i < n_fft; i++) p->h_true[i] = hh[i];
    }
    for (i = 0; i < n_sc; i++) hpow += ph_abs2(p->h_true[off + i]);
    hpow /= (double)n_sc;

    /* FFT 后每维噪声方差 = hpow/(2*snr)。频域加噪与"时域白噪过 FFT"
     * 统计等价，而 CP 段的噪声本来就被丢弃，所以省下 6 倍高斯采样。 */
    nstd = wl_sqrt(hpow / (2.0 * snr_lin));

    dmrs_sym[0] = 2;
    dmrs_sym[1] = n_sym >= 12 ? 11 : n_sym - 2;

    /* ---- 逐符号：映射 -> IFFT -> CP -> 多径 -> 噪声 -> 去 CP -> FFT ---- */
    bitpos = 0;
    for (sym = 0; sym < n_sym; sym++) {
        int is_dmrs = (sym == dmrs_sym[0] || sym == dmrs_sym[1]);
        wl_ph_c grid_row[WL_PHY_MAX_SC];

        for (i = 0; i < n_sc; i++) grid_row[i] = ph_mk(0.0, 0.0);

        if (is_dmrs) {
            ph_gold_init(&gold, (uint32_t)(seed + (uint32_t)sym * 7919u));
            for (i = 0; i < n_sc; i += 2) {
                double re = 1.0 - 2.0 * (double)ph_gold_next(&gold);
                double im = 1.0 - 2.0 * (double)ph_gold_next(&gold);
                grid_row[i] = ph_mk(re * 0.70710678118654752,
                                    im * 0.70710678118654752);
            }
        } else {
            int nb = n_sc * qm;
            if (bitpos + nb > coded_cap) nb = coded_cap - bitpos;
            if (nb > 0) {
                wl_ph_c tmp[WL_PHY_MAX_SC];
                wl_phy_map(&p->coded[bitpos], nb, qm, tmp);
                ns = nb / qm;
                for (i = 0; i < ns && i < n_sc; i++) grid_row[i] = tmp[i];
            }
            bitpos += n_sc * qm;
        }

        for (i = 0; i < n_fft; i++) p->tx[i] = ph_mk(0.0, 0.0);
        for (i = 0; i < n_sc; i++) p->tx[off + i] = grid_row[i];

        wl_phy_fft(p->tx, n_fft, 1);
        p->st.fft_ops += (int64_t)(n_fft / 2) * ph_log2i(n_fft);

        for (i = 0; i < cp; i++) p->td[i] = p->tx[n_fft - cp + i];
        for (i = 0; i < n_fft; i++) p->td[cp + i] = p->tx[i];
        ph_conv_channel(p->td, n_fft + cp, p->h_ir, L, 0.0, &rng);

        for (i = 0; i < n_fft; i++) p->rx[i] = p->td[cp + i];
        wl_phy_fft(p->rx, n_fft, 0);
        p->st.fft_ops += (int64_t)(n_fft / 2) * ph_log2i(n_fft);

        for (i = 0; i < n_sc; i++) {
            p->rx[off + i].re += nstd * ph_gauss(&rng);
            p->rx[off + i].im += nstd * ph_gauss(&rng);
            p->grid[sym][i] = p->rx[off + i];
        }
    }

    /* ---- 收端：DMRS 最小二乘 + 频域插值 ---- */
    {
        wl_ph_c acc[WL_PHY_MAX_SC];
        int have = 0;

        for (i = 0; i < n_sc; i++) acc[i] = ph_mk(0.0, 0.0);

        for (k = 0; k < 2; k++) {
            int sy = dmrs_sym[k];
            ph_gold_init(&gold, (uint32_t)(seed + (uint32_t)sy * 7919u));
            for (i = 0; i < n_sc; i++) {
                if ((i & 1) == 0) {
                    double re = 1.0 - 2.0 * (double)ph_gold_next(&gold);
                    double im = 1.0 - 2.0 * (double)ph_gold_next(&gold);
                    /* DMRS 是单位功率 QPSK，|X|^2 = 1，LS 直接 Y*conj(X) */
                    acc[i] = ph_add(acc[i], ph_mul(p->grid[sy][i],
                                   ph_mk(re * 0.70710678118654752,
                                         -im * 0.70710678118654752)));
                }
            }
            have++;
        }
        if (have > 1)
            for (i = 0; i < n_sc; i += 2) acc[i] = ph_scale(acc[i], 1.0 / (double)have);

        for (i = 0; i < n_sc; i++) p->h_ls[i] = acc[i];
        for (i = 1; i < n_sc; i += 2) {
            wl_ph_c a = p->h_ls[i - 1];
            wl_ph_c bb = (i + 1 < n_sc) ? p->h_ls[i + 1] : p->h_ls[i - 1];
            p->h_ls[i] = ph_scale(ph_add(a, bb), 0.5);
        }
        for (i = 0; i < n_sc; i++) p->h_est[i] = p->h_ls[i];

        if (c->ce_mode > 0) {
            /* 一阶频域平滑：信道在相邻 3 个子载波上近似线性，
             * 3 抽头 [1/4 1/2 1/4] 把估计噪声方差压到 3/8，偏差很小。 */
            wl_ph_c sm[WL_PHY_MAX_SC];
            for (i = 0; i < n_sc; i++) {
                wl_ph_c a = p->h_est[i > 0 ? i - 1 : 0];
                wl_ph_c b = p->h_est[i];
                wl_ph_c d = p->h_est[i + 1 < n_sc ? i + 1 : n_sc - 1];
                sm[i].re = 0.25 * a.re + 0.5 * b.re + 0.25 * d.re;
                sm[i].im = 0.25 * a.im + 0.5 * b.im + 0.25 * d.im;
            }
            for (i = 0; i < n_sc; i++) p->h_est[i] = sm[i];
        }

        /* 噪声估计：DMRS 处二阶差分 */
        {
            double s2 = 0.0;
            int cnt = 0;
            for (i = 2; i + 2 < n_sc; i += 2) {
                wl_ph_c d = ph_add(p->h_ls[i],
                                   ph_scale(ph_add(p->h_ls[i - 2], p->h_ls[i + 2]), -0.5));
                s2 += ph_abs2(d);
                cnt++;
            }
            if (cnt > 0) s2 /= (double)cnt;
            p->st.sigma2 = s2 * 0.6666666666666666;
            if (p->st.sigma2 < 1e-9) p->st.sigma2 = 1e-9;
        }

        nmse = 0.0;
        for (i = 0; i < n_sc; i++) {
            wl_ph_c e = ph_add(p->h_est[i], ph_scale(p->h_true[off + i], -1.0));
            nmse += ph_abs2(e);
        }
        nmse /= (hpow * (double)n_sc > 0.0 ? hpow * (double)n_sc : 1.0);
        p->st.nmse_acc += nmse;
        p->st.nmse_n++;
        p->st.snr_acc += 10.0 * wl_log10(hpow / p->st.sigma2);
        p->st.snr_n++;
    }

    /* ---- 均衡 + 解调 ---- */
    bitpos = 0;
    llr_pos = 0;
    for (sym = 0; sym < n_sym; sym++) {
        int is_dmrs = (sym == dmrs_sym[0] || sym == dmrs_sym[1]);
        double s2sum = 0.0;
        int nb, nleft, si;

        if (is_dmrs) continue;
        nb = n_sc * qm;
        nleft = coded_cap - bitpos;
        if (nb > nleft) nb = nleft;
        if (nb <= 0) break;

        for (i = 0; i < n_sc; i++) {
            wl_ph_c hh = p->h_est[i];
            double p2 = ph_abs2(hh);
            double mu = p2 / (p2 + p->st.sigma2);
            wl_ph_c w = ph_scale(ph_conj(hh), 1.0 / (p2 + p->st.sigma2));
            p->eq[i] = ph_mul(w, p->grid[sym][i]);
            if (mu > 1e-6) p->eq[i] = ph_scale(p->eq[i], 1.0 / mu);
            /* 去偏后每 RE 的剩余噪声 = sigma2/|H|^2，等效 SNR = |H|^2/sigma2 */
            s2sum += p->st.sigma2 / (p2 > 1e-9 ? p2 : 1e-9);
        }

        wl_phy_demap(p->eq, nb / qm, qm, s2sum / (double)n_sc, &p->llr[llr_pos]);

        for (si = 0; si < nb; si++) {
            int hard = p->llr[llr_pos + si] < 0 ? 1 : 0;
            if (hard != (p->coded[bitpos + si] & 1)) raw_err++;
            raw_bits++;
        }
        for (si = 0; si < nb / qm; si++) {
            wl_ph_c ref = p->eq[si];
            wl_phy_slice(&ref, qm);
            evm_err += ph_abs2(ph_add(p->eq[si], ph_scale(ref, -1.0)));
            evm_ref += ph_abs2(ref);
        }

        llr_pos += nb;
        bitpos += nb;
    }

    /* ---- 解扰 ---- */
    ph_gold_init(&gold, (uint32_t)(seed ^ 0x5A5Au));
    for (i = 0; i < llr_pos; i++) {
        if (ph_gold_next(&gold)) p->llr[i] = (int16_t)(-p->llr[i]);
    }

    /* ---- 解速率匹配 ---- */
    ph_zero(p->llr_dec, (uint32_t)sizeof(int16_t) * (uint32_t)enc_len);
    for (i = 0; i < llr_pos; i++) {
        int j = (int)(((int64_t)i * (int64_t)enc_len) / (int64_t)coded_cap);
        int32_t v;
        if (j >= enc_len) j = enc_len - 1;
        v = (int32_t)p->llr_dec[j] + (int32_t)p->llr[i];
        if (v > 8191) v = 8191;
        if (v < -8191) v = -8191;
        p->llr_dec[j] = (int16_t)v;
    }

    ph_viterbi(p->llr_dec, info_len + 6, p->rx_info, p);
    p->st.viterbi_ops += (int64_t)(info_len + 6) * 64;

    for (i = 0; i < info_len; i++)
        if ((p->rx_info[i] & 1) != (p->info[i] & 1)) dec_err++;
    p->st.dec_err += dec_err;
    p->st.dec_bits += info_len;
    p->st.raw_err += raw_err;
    p->st.raw_bits += raw_bits;
    if (evm_ref > 1e-12) {
        p->st.evm_acc += wl_sqrt(evm_err / evm_ref);
        p->st.evm_n++;
    }

    crc_rx = ph_crc24(p->rx_info, info_len - 24);
    {
        uint32_t rx = 0;
        for (i = 0; i < 24; i++) rx = (rx << 1) | (p->rx_info[info_len - 24 + i] & 1);
        ok = (rx == crc_rx);
    }

    p->st.rate_acc += rate;
    p->st.rate_n++;
    p->st.prb_sum += prb;
    if (ok) {
        p->st.tb_ok++;
        p->st.bits_ok += (uint64_t)(info_len - 24);
    } else {
        p->st.tb_fail++;
    }
    return ok;
}
