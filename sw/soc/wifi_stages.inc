/* -----------------------------------------------------------------------------
 * IEEE 802.11be EHT SU 20 MHz（MCS 0）接收链的阶段实现。
 *
 * 帧：HE-STF(1) + HE-LTF(2) + EHT-SIG(1) + DATA(3)，共 7 个 256 点 OFDM 符号，
 * CP=16。数据子载波 234 个（-117..-1, 1..117）。
 *
 *   EHT-SIG：36 信息位 + 6 零尾，K=7 BCC（0o133/0o171），BPSK 1/2；
 *   DATA   ：324 信息位，LDPC(648, 1/2, Z=27)，BPSK；
 *   HE-STF/HE-LTF：固定 Gold QPSK（c_init=0x57F/0x11BE）——本仓库的确定性
 *   参考，不是 IEEE 表（报告里标注为简化）。
 *
 * 所有运算在 CVA6 上标量执行：256 点 DFT（O(N^2)，Q15 cos 表）、LS、
 * BPSK 软解调、K=7 Viterbi、802.11 N648 R1/2 min-sum LDPC。
 * -------------------------------------------------------------------------- */
#ifndef WIFI_STAGES_INC
#define WIFI_STAGES_INC

#include "wifi_cos256_tab.h"
#include "wifi_ldpc_648_r12.h"

#define EXIT_ADDR       0x40010000ul
#define RESULT_BASE     ((uint32_t *)0x80070000ul)
#define PARAM_BASE      0x80600000ul
#define WIFI_NFFT       256
#define WIFI_CP         16
#define WIFI_NSYM       7
#define WIFI_NDATA      234
#define WIFI_SYM_WORDS  (2 * (WIFI_CP + WIFI_NFFT))
#define WIFI_TD         0x80400000ul
#define WIFI_QPSK       2000
#define WIFI_H_SCALE    16
#define WIFI_COS_SCALE  32768
#define WIFI_MUL        ((int64_t)WIFI_NFFT * WIFI_COS_SCALE)

#define WIFI_STF_SYM    0
#define WIFI_LTF0       1
#define WIFI_LTF1       2
#define WIFI_SIG_SYM    3
#define WIFI_DATA0      4
#define WIFI_SIG_INFO   36
#define WIFI_SIG_TRELLIS 42
#define WIFI_DATA_INFO  324
#define WIFI_DATA_CODED 648

static uint32_t errs, runs, dmas, busy;

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

#ifndef PROTO_CPU_ONLY
/* 256 点 FFT 复用 ssb_fft.inc 的 radix-4 引擎（stage + tail 两张镜像）。 */
#define CGRA_BASE   0x50000000ul
#define NPES        16
#define RING_DEPTH  1024
#include "../include/gpcp_ring.h"   /* 描述符环基址跟着 .bss 走（不再硬编码） */
#define DESC_BASE   GPCP_DESC_BASE
static uint32_t (*desc)[8] = (uint32_t (*)[8])_ring_base;
static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}
#include "ssb_fft.inc"
#endif

static int32_t wifi_y[WIFI_NSYM][WIFI_NDATA][2];
static int32_t wifi_h[WIFI_NDATA][2];
static int32_t wifi_llr[WIFI_NSYM][WIFI_NDATA];
static int32_t wifi_ltf_re[WIFI_NDATA], wifi_ltf_im[WIFI_NDATA];
static uint8_t wifi_sig[WIFI_SIG_TRELLIS], wifi_data[WIFI_DATA_INFO];
static uint32_t wifi_sig_ok, wifi_data_ok, wifi_iters, wifi_syn;

/* ------------------------------------------------------------ Gold QPSK */
#define WIFI_GOLD_LEN  (2 * WIFI_NDATA + 1600 + 31)
static uint32_t wgold_x1[WIFI_GOLD_LEN], wgold_x2[WIFI_GOLD_LEN];

static void wifi_gold_init(uint32_t c_init)
{
    for (uint32_t i = 0; i < WIFI_GOLD_LEN; i++) {
        wgold_x1[i] = 0;
        wgold_x2[i] = 0;
    }
    wgold_x1[0] = 1;
    for (uint32_t i = 0; i < 31u; i++)
        wgold_x2[i] = (c_init >> i) & 1u;
    for (uint32_t i = 0; i + 31u < WIFI_GOLD_LEN; i++) {
        wgold_x1[i + 31] = wgold_x1[i + 3] ^ wgold_x1[i];
        wgold_x2[i + 31] = wgold_x2[i + 3] ^ wgold_x2[i + 2]
                         ^ wgold_x2[i + 1] ^ wgold_x2[i];
    }
}

static uint32_t wifi_gold_bit(uint32_t n)
{
    return wgold_x1[1600u + n] ^ wgold_x2[1600u + n];
}

static void wifi_ref_qpsk(uint32_t c_init, int32_t *re, int32_t *im)
{
    wifi_gold_init(c_init);
    for (int k = 0; k < WIFI_NDATA; k++) {
        re[k] = (1 - 2 * (int32_t)wifi_gold_bit((uint32_t)(2 * k))) * WIFI_QPSK;
        im[k] = (1 - 2 * (int32_t)wifi_gold_bit((uint32_t)(2 * k + 1))) * WIFI_QPSK;
    }
}

/* ------------------------------------------------------- 256 点 DFT（CVA6） */
static void wifi_dft256(const int32_t *ar, const int32_t *ai, int32_t *out)
{
    for (int k = 0; k < WIFI_NFFT; k++) {
        int64_t sr = 0, si = 0;
        for (int n = 0; n < WIFI_NFFT; n++) {
            int idx = (k * n) & (WIFI_NFFT - 1);
            int64_t c = wifi_cos256[idx];
            int64_t s = wifi_cos256[(idx + 192) & (WIFI_NFFT - 1)];
            sr += (int64_t)ar[n] * c + (int64_t)ai[n] * s;
            si += (int64_t)ai[n] * c - (int64_t)ar[n] * s;
        }
        out[2 * k] = (int32_t)(sr / WIFI_MUL);
        out[2 * k + 1] = (int32_t)(si / WIFI_MUL);
    }
}

static int wifi_bin(int k) { return (k + WIFI_NFFT) & (WIFI_NFFT - 1); }

/* --------------------------------------------------------------- stages */
static int wifi_setup(const uint32_t *par)
{
    (void)par;
    wifi_sig_ok = wifi_data_ok = 0;
    wifi_iters = wifi_syn = 0;
    for (int s = 0; s < WIFI_NSYM; s++)
        for (int k = 0; k < WIFI_NDATA; k++) {
            wifi_y[s][k][0] = wifi_y[s][k][1] = 0;
            wifi_llr[s][k] = 0;
        }
    for (int k = 0; k < WIFI_NDATA; k++) {
        wifi_h[k][0] = wifi_h[k][1] = 0;
        wifi_ltf_re[k] = wifi_ltf_im[k] = 0;
    }
    for (int i = 0; i < WIFI_SIG_TRELLIS; i++) wifi_sig[i] = 0;
    for (int i = 0; i < WIFI_DATA_INFO; i++) wifi_data[i] = 0;
    return 0;
}

static int wifi_received(void) { return 0; }

static int wifi_dmrs(void)
{
    wifi_ref_qpsk(0x11BEu, wifi_ltf_re, wifi_ltf_im);
    return 0;
}

static uint32_t wifi_rev8(uint32_t x)
{
    x = ((x & 0x55u) << 1) | ((x & 0xAAu) >> 1);
    x = ((x & 0x33u) << 2) | ((x & 0xCCu) >> 2);
    x = ((x & 0x0Fu) << 4) | ((x & 0xF0u) >> 4);
    return x & 0xFFu;
}

/* 256 点 radix-2 DIT FFT（Q15 twiddle），就地运算；输出与直接 DFT 同序。 */
static void wifi_fft256(int32_t *ar, int32_t *ai)
{
    for (int i = 0; i < WIFI_NFFT; i++) {
        int j = (int)wifi_rev8((uint32_t)i);
        if (i < j) {
            int32_t t = ar[i]; ar[i] = ar[j]; ar[j] = t;
            t = ai[i]; ai[i] = ai[j]; ai[j] = t;
        }
    }
    for (int len = 2; len <= WIFI_NFFT; len <<= 1) {
        int half = len >> 1;
        int step = WIFI_NFFT / len;
        for (int i = 0; i < WIFI_NFFT; i += len) {
            for (int j = 0; j < half; j++) {
                int idx = j * step;
                int64_t c = wifi_cos256[idx];
                int64_t s = wifi_cos256[(idx + 192) & (WIFI_NFFT - 1)];
                int64_t br = ar[i + j + half], bi = ai[i + j + half];
                int64_t tr = (br * c + bi * s) / WIFI_COS_SCALE;
                int64_t ti = (bi * c - br * s) / WIFI_COS_SCALE;
                int64_t ur = ar[i + j], ui = ai[i + j];
                ar[i + j] = (int32_t)(ur + tr);
                ai[i + j] = (int32_t)(ui + ti);
                ar[i + j + half] = (int32_t)(ur - tr);
                ai[i + j + half] = (int32_t)(ui - ti);
            }
        }
    }
    for (int i = 0; i < WIFI_NFFT; i++) {
        ar[i] /= WIFI_NFFT;
        ai[i] /= WIFI_NFFT;
    }
}

static int wifi_ofdm_demodulate_impl(int use_fft)
{
    static int32_t ar[WIFI_NFFT], ai[WIFI_NFFT], dft[2 * WIFI_NFFT];
    const int32_t *td = (const int32_t *)WIFI_TD;
    for (int s = 0; s < WIFI_NSYM; s++) {
        const int32_t *sp = td + s * WIFI_SYM_WORDS + 2 * WIFI_CP;
        for (int n = 0; n < WIFI_NFFT; n++) {
            ar[n] = sp[2 * n];
            ai[n] = sp[2 * n + 1];
        }
        if (use_fft == 2) {
#ifdef PROTO_CPU_ONLY
            wifi_fft256(ar, ai);
            for (int n = 0; n < WIFI_NFFT; n++) {
                dft[2 * n] = ar[n];
                dft[2 * n + 1] = ai[n];
            }
#else
            static int32_t fbuf[2 * WIFI_NFFT];
            for (int n = 0; n < WIFI_NFFT; n++) {
                fbuf[2 * n] = ar[n];
                fbuf[2 * n + 1] = ai[n];
            }
            fft_n(fbuf, WIFI_NFFT, FFT_LUT_256);
            rt_dcache_flush();
            for (int n = 0; n < WIFI_NFFT; n++) {
                uint32_t r = rev4n((uint32_t)n, 4);
                dft[2 * n] = fbuf[2 * r];
                dft[2 * n + 1] = fbuf[2 * r + 1];
            }
#endif
        } else if (use_fft) {
            wifi_fft256(ar, ai);
            for (int n = 0; n < WIFI_NFFT; n++) {
                dft[2 * n] = ar[n];
                dft[2 * n + 1] = ai[n];
            }
        } else {
            wifi_dft256(ar, ai, dft);
        }
        {
            int idx = 0;
            for (int k = -117; k <= 117; k++) {
                int b;
                if (k == 0)
                    continue;
                b = wifi_bin(k);
                wifi_y[s][idx][0] = dft[2 * b] >> 8;
                wifi_y[s][idx][1] = dft[2 * b + 1] >> 8;
                idx++;
            }
        }
    }
    return 0;
}

static int wifi_ofdm_demodulate_cgra(void)
{
    return wifi_ofdm_demodulate_impl(2);
}

static int wifi_ofdm_demodulate(void) { return wifi_ofdm_demodulate_impl(0); }
static int wifi_ofdm_demodulate_fft(void) { return wifi_ofdm_demodulate_impl(1); }

static int wifi_estimate(void)
{
    const int64_t den = (int64_t)2 * 2 * WIFI_QPSK * WIFI_QPSK / WIFI_H_SCALE;
    for (int k = 0; k < WIFI_NDATA; k++) {
        int64_t accr = 0, acci = 0;
        for (int s = WIFI_LTF0; s <= WIFI_LTF1; s++) {
            int64_t yr = wifi_y[s][k][0];
            int64_t yi = wifi_y[s][k][1];
            accr += yr * wifi_ltf_re[k] + yi * wifi_ltf_im[k];
            acci += yi * wifi_ltf_re[k] - yr * wifi_ltf_im[k];
        }
        wifi_h[k][0] = (int32_t)((accr >= 0 ? accr + den / 2 : accr - den / 2) / den);
        wifi_h[k][1] = (int32_t)((acci >= 0 ? acci + den / 2 : acci - den / 2) / den);
    }
    return 0;
}

static int64_t wifi_eq[WIFI_NSYM][WIFI_NDATA];

static int wifi_equalise(void)
{
    for (int s = 0; s < WIFI_NSYM; s++)
        for (int k = 0; k < WIFI_NDATA; k++)
            wifi_eq[s][k] = (int64_t)wifi_y[s][k][0] * wifi_h[k][0]
                          + (int64_t)wifi_y[s][k][1] * wifi_h[k][1];
    return 0;
}

static int wifi_demap(void)
{
    for (int s = 0; s < WIFI_NSYM; s++)
        for (int k = 0; k < WIFI_NDATA; k++)
            wifi_llr[s][k] = (int32_t)(wifi_eq[s][k] >> 4);   /* H_SCALE=16 */
    return 0;
}

/* 兼容入口：老的单片调用路径（bm 程序 / 逐阶段调试）仍可用。 */
static int wifi_equalise_demap(void)
{
    wifi_equalise();
    wifi_demap();
    return 0;
}

/* ------------------------------------------------------ K=7 Viterbi (BCC) */
static int wifi_parity(uint32_t x)
{
    x ^= x >> 4;
    x ^= x >> 2;
    x ^= x >> 1;
    return (int)(x & 1u);
}

static void wifi_viterbi(const int32_t *llr, int nsteps, uint8_t *out)
{
    int32_t metric[64], next[64];
    uint8_t prev[WIFI_SIG_TRELLIS][64];
    const int32_t NEG = -(1 << 29);

    for (int s = 0; s < 64; s++) metric[s] = NEG;
    metric[0] = 0;
    for (int t = 0; t < nsteps; t++) {
        for (int s = 0; s < 64; s++) next[s] = NEG;
        for (int s = 0; s < 64; s++) {
            if (metric[s] == NEG)
                continue;
            for (int u = 0; u < 2; u++) {
                uint32_t reg = ((uint32_t)s << 1) | (uint32_t)u;
                int c0 = wifi_parity(reg & 0x5Bu);    /* 0o133 */
                int c1 = wifi_parity(reg & 0x79u);    /* 0o171 */
                int32_t m = metric[s]
                          + (c0 ? -llr[2 * t] : llr[2 * t])
                          + (c1 ? -llr[2 * t + 1] : llr[2 * t + 1]);
                int ns = (int)(reg & 0x3Fu);
                if (m > next[ns]) {
                    next[ns] = m;
                    prev[t][ns] = (uint8_t)s;
                }
            }
        }
        for (int s = 0; s < 64; s++) metric[s] = next[s];
    }
    {
        int s = 0;                       /* 零尾：终点状态 0 */
        for (int t = nsteps - 1; t >= 0; t--) {
            out[t] = (uint8_t)(s & 1);
            s = prev[t][s];
        }
    }
}

/* ------------------------------------------ 802.11 N648 R1/2 min-sum LDPC */
static uint16_t wifi_edge_var[324][24];
static uint8_t wifi_edge_deg[324];
static int16_t wifi_c2v[324][24];
static int32_t wifi_var[648], wifi_chan[648];
static uint8_t wifi_hard[648];

static void wifi_ldpc_init(void)
{
    for (int r = 0; r < WIFI_LDPC_MB; r++)
        for (int i = 0; i < WIFI_LDPC_Z; i++) {
            int check = r * WIFI_LDPC_Z + i;
            int e = 0;
            for (int c = 0; c < WIFI_LDPC_NB; c++) {
                int sh = wifi_ldpc_base[r][c];
                if (sh < 0)
                    continue;
                wifi_edge_var[check][e] =
                    (uint16_t)(c * WIFI_LDPC_Z + ((i + sh) % WIFI_LDPC_Z));
                e++;
            }
            wifi_edge_deg[check] = (uint8_t)e;
        }
}

static int wifi_ldpc_decode(const int32_t *llr)
{
    for (int v = 0; v < WIFI_LDPC_N; v++) {
        int32_t x = llr[v];
        if (x > 30000) x = 30000;
        if (x < -30000) x = -30000;
        wifi_chan[v] = x;
    }
    for (int r = 0; r < 324; r++)
        for (int e = 0; e < 24; e++)
            wifi_c2v[r][e] = 0;

    for (int it = 0; it < 10; it++) {
        for (int v = 0; v < WIFI_LDPC_N; v++)
            wifi_var[v] = wifi_chan[v];
        for (int r = 0; r < 324; r++)
            for (int e = 0; e < wifi_edge_deg[r]; e++)
                wifi_var[wifi_edge_var[r][e]] += wifi_c2v[r][e];

        for (int r = 0; r < 324; r++) {
            int deg = wifi_edge_deg[r];
            int32_t min1 = 1 << 29, min2 = 1 << 29;
            int idx = 0, sign = 1;
            for (int e = 0; e < deg; e++) {
                int32_t x = wifi_var[wifi_edge_var[r][e]] - wifi_c2v[r][e];
                if (x < 0) { sign = -sign; x = -x; }
                if (x < min1) { min2 = min1; min1 = x; idx = e; }
                else if (x < min2) { min2 = x; }
            }
            for (int e = 0; e < deg; e++) {
                int32_t x = wifi_var[wifi_edge_var[r][e]] - wifi_c2v[r][e];
                int32_t val = (e == idx) ? min2 : min1;
                int s_e = sign * (x < 0 ? -1 : 1);   /* 去掉本边自己的符号 */
                if (val > 32767) val = 32767;
                wifi_c2v[r][e] = (int16_t)((s_e < 0) ? -val : val);
            }
        }

        for (int v = 0; v < WIFI_LDPC_N; v++)
            wifi_var[v] = wifi_chan[v];
        for (int r = 0; r < 324; r++)
            for (int e = 0; e < wifi_edge_deg[r]; e++)
                wifi_var[wifi_edge_var[r][e]] += wifi_c2v[r][e];

        for (int v = 0; v < WIFI_LDPC_N; v++)
            wifi_hard[v] = (wifi_var[v] < 0) ? 1u : 0u;

        {
            int bad = 0;
            for (int r = 0; r < 324; r++) {
                int p = 0;
                for (int e = 0; e < wifi_edge_deg[r]; e++)
                    p ^= wifi_hard[wifi_edge_var[r][e]];
                bad |= p;
            }
            wifi_syn = (uint32_t)bad;
            wifi_iters = (uint32_t)(it + 1);
            if (!bad)
                break;
        }
    }
    return (int)wifi_syn;
}

static int wifi_decode(void)
{
    int32_t sig_llr[2 * WIFI_SIG_TRELLIS];
    int32_t data_llr[WIFI_DATA_CODED];

    for (int i = 0; i < WIFI_SIG_TRELLIS; i++) {
        sig_llr[2 * i] = wifi_llr[WIFI_SIG_SYM][2 * i];
        sig_llr[2 * i + 1] = wifi_llr[WIFI_SIG_SYM][2 * i + 1];
    }
    wifi_viterbi(sig_llr, WIFI_SIG_TRELLIS, wifi_sig);
    wifi_sig_ok = 1;
    for (int i = WIFI_SIG_INFO; i < WIFI_SIG_TRELLIS; i++)
        if (wifi_sig[i] != 0)
            wifi_sig_ok = 0;

    {
        int m = 0;
        for (int s = WIFI_DATA0; s < WIFI_NSYM; s++)
            for (int k = 0; k < WIFI_NDATA && m < WIFI_DATA_CODED; k++)
                data_llr[m++] = wifi_llr[s][k];
        while (m < WIFI_DATA_CODED)
            data_llr[m++] = 0;
    }
    wifi_ldpc_init();
    wifi_ldpc_decode(data_llr);
    for (int i = 0; i < WIFI_DATA_INFO; i++)
        wifi_data[i] = wifi_hard[i];
    wifi_data_ok = (wifi_syn == 0) ? 1u : 0u;
    return 0;
}

#endif /* WIFI_STAGES_INC */
