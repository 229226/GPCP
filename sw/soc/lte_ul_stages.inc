/* -----------------------------------------------------------------------------
 * LTE PUSCH（上行 Turbo + SC-FDMA）的阶段实现。
 *
 * 复用 lte_stages.inc 的 Turbo 编/译码、36.212 速率匹配、Gold、FFT 与缓冲；
 * 本文件只新增上行链路：
 *   - 上行 DMRS：Gold QPSK（简化，非 36.211 5.5.2 的 ZC）；
 *   - 发射：每符号 200 个 QPSK 数据符号做 200 点 DFT，映射到 200 个数据 RE；
 *   - 接收：200 个数据 RE 匹配滤波后做 200 点 IDFT，恢复 QPSK 软符号。
 *
 * 口径与工程其它 LTE 基准一致：2 符号 × 240 RE、40 导频/符号、200 数据 RE/符号、
 * E=800、QPSK、K=104 Turbo、单码块；TD 尾部 5 字（TB 3 字 + rv + 保留）。
 * -------------------------------------------------------------------------- */
#ifndef LTE_UL_STAGES_INC
#define LTE_UL_STAGES_INC

#include "lte_stages.inc"
#include "lte_ul_dft200_tab.h"

#define LU_N           200
#define LU_COS_SCALE   32768
#define LU_MUL         ((int64_t)LU_N * LU_COS_SCALE)

static int32_t lu_pil_re[NSYM][NPILOT], lu_pil_im[NSYM][NPILOT];
static int32_t lu_hre[NSYM], lu_him[NSYM];
static int32_t lu_dft[NSYM][2 * LU_N];

/* 正向 DFT：X_k = (1/N) Σ x_n e^{-j2πkn/N}，Q15 cos 表、整数归一化。 */
static void lu_dft200(const int32_t *ar, const int32_t *ai, int32_t *out)
{
    for (int k = 0; k < LU_N; k++) {
        int64_t sr = 0, si = 0;
        for (int n = 0; n < LU_N; n++) {
            int idx = (k * n) % LU_N;
            int64_t c = lu_cos200[idx];
            int64_t s = lu_cos200[(idx + 150) % LU_N];
            sr += (int64_t)ar[n] * c + (int64_t)ai[n] * s;
            si += (int64_t)ai[n] * c - (int64_t)ar[n] * s;
        }
        out[2 * k] = (int32_t)(sr / LU_MUL);
        out[2 * k + 1] = (int32_t)(si / LU_MUL);
    }
}

/* 反向 IDFT：x_n = (1/N) Σ X_k e^{+j2πkn/N}。 */
static void lu_idft200(const int32_t *ar, const int32_t *ai, int32_t *out)
{
    for (int n = 0; n < LU_N; n++) {
        int64_t sr = 0, si = 0;
        for (int k = 0; k < LU_N; k++) {
            int idx = (k * n) % LU_N;
            int64_t c = lu_cos200[idx];
            int64_t s = lu_cos200[(idx + 150) % LU_N];
            sr += (int64_t)ar[k] * c - (int64_t)ai[k] * s;
            si += (int64_t)ai[k] * c + (int64_t)ar[k] * s;
        }
        out[2 * n] = (int32_t)(sr / LU_MUL);
        out[2 * n + 1] = (int32_t)(si / LU_MUL);
    }
}

static int lu_setup(const uint32_t *par)
{
    return lte_setup(par);
}

static int lu_received(void) { return 0; }

static int lu_transport_block(void) { return lte_transport_block(); }

static int lu_encode(void) { return lte_encode(); }

static int lu_dmrs(void)
{
    for (int s = 0; s < NSYM; s++) {
        gold_init(((N_RNTI << 14) + 1u + (uint32_t)s) & 0x7FFFFFFFu);
        for (int p = 0; p < NPILOT; p++) {
            lu_pil_re[s][p] = (1 - 2 * (int32_t)gold_bit((uint32_t)(2 * p))) * QPSK;
            lu_pil_im[s][p] = (1 - 2 * (int32_t)gold_bit((uint32_t)(2 * p + 1))) * QPSK;
        }
    }
    return 0;
}

/* 加扰 + QPSK，然后逐符号 200 点 DFT（网格在 dmrs 之后由 map_to_grid 建）。 */
static int lu_modulate(void)
{
    if (lte_modulate() != 0)
        return 1;
    for (int s = 0; s < NSYM; s++) {
        int32_t ar[LU_N], ai[LU_N];
        for (int i = 0; i < LU_N; i++) {
            ar[i] = sym[2 * (s * LU_N + i)];
            ai[i] = sym[2 * (s * LU_N + i) + 1];
        }
        lu_dft200(ar, ai, lu_dft[s]);
    }
    return 0;
}

static int lu_map_to_grid(void)
{
    for (int s = 0; s < NSYM; s++) {
        int32_t *f = (int32_t *)(uintptr_t)(0x803A0000ul + (uint32_t)s * 0x8000u);
        int p = 0, m = 0;
        for (int i = 0; i < 2 * NFFT; i++)
            f[i] = 0;
        for (int r = 0; r < NRE; r++) {
            if (r % CRS_STEP == 0) {
                f[2 * (BIN0 + r)] = lu_pil_re[s][p];
                f[2 * (BIN0 + r) + 1] = -lu_pil_im[s][p];
                p++;
            } else {
                f[2 * (BIN0 + r)] = lu_dft[s][2 * m];
                f[2 * (BIN0 + r) + 1] = -lu_dft[s][2 * m + 1];
                m++;
            }
        }
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        (void)f[2 * (BIN0 + NRE) - 1];
    }
    return 0;
}

static int lu_ofdm_modulate(void) { return lte_ofdm_modulate(); }

static int lu_channel(void) { return 0; }

static int lu_ofdm_demodulate(void) { return lte_ofdm_demodulate(); }

static int lu_estimate(void)
{
    const int64_t den = (int64_t)NPILOT * 2 * QPSK * QPSK / H_SCALE;
    for (int s = 0; s < NSYM; s++) {
        int64_t accr = 0, acci = 0;
        for (int p = 0; p < NPILOT; p++) {
            int r = p * CRS_STEP;
            int64_t yr = (int64_t)yre[s][r] << 4;
            int64_t yi = (int64_t)yim[s][r] << 4;
            accr += yr * lu_pil_re[s][p] + yi * lu_pil_im[s][p];
            acci += yi * lu_pil_re[s][p] - yr * lu_pil_im[s][p];
        }
        lu_hre[s] = (int32_t)((accr >= 0 ? accr + den / 2 : accr - den / 2) / den);
        lu_him[s] = (int32_t)((acci >= 0 ? acci + den / 2 : acci - den / 2) / den);
    }
    return 0;
}

static int lu_equalise_demap(void)
{
    static int32_t ar[LU_N], ai[LU_N], soft[2 * LU_N];
    for (int s = 0; s < NSYM; s++) {
        int m = 0;
        for (int r = 0; r < NRE; r++) {
            if (r % CRS_STEP == 0)
                continue;
            int64_t yr = (int64_t)yre[s][r] << 4;
            int64_t yi = (int64_t)yim[s][r] << 4;
            ar[m] = (int32_t)(yr * lu_hre[s] + yi * lu_him[s]);
            ai[m] = (int32_t)(yi * lu_hre[s] - yr * lu_him[s]);
            m++;
        }
        lu_idft200(ar, ai, soft);
        for (int i = 0; i < LU_N; i++) {
            llr_buf[2 * (s * LU_N + i)] = soft[2 * i];
            llr_buf[2 * (s * LU_N + i) + 1] = soft[2 * i + 1];
        }
    }
    return lte_descramble_dematch();
}

static int lu_decode(void) { return lte_decode(); }

#endif /* LTE_UL_STAGES_INC */
