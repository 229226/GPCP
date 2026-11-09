/* -----------------------------------------------------------------------------
 * LTE 小区搜索（PSS + SSS）的阶段实现。
 *
 * 36.211 6.11.1.1 / 6.11.2.1，全部 CVA6 标量：
 *   PSS：3 个根 u={25,29,34}，62 点 ZC，相关能量最大 -> N_ID2；
 *   SSS：168×3 = 504 个假设，62 点 m 序列相关，最大 -> N_ID1。
 *
 * 激励（0x80400000）：
 *   [0..123]   PSS 62 个复数（re,im 交错）
 *   [124..247] SSS 62 个复数
 *   [248] n_id1（诊断） [249] n_id2（诊断）
 * 锚点：7 个标量 + 3 个 PSS 能量 + 504 个 SSS 能量。
 * -------------------------------------------------------------------------- */
#ifndef LTE_CELL_STAGES_INC
#define LTE_CELL_STAGES_INC

#include "lte_pss_tab.h"

#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)
#define PARAM_BASE    0x80600000ul
#define CELL_IN       ((const uint32_t *)0x80400000ul)
#define CELL_N        62
#define CELL_NSSS     504

static uint32_t errs, runs, dmas, busy;

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static int32_t cell_pss_re[3][CELL_N], cell_pss_im[3][CELL_N];
static int32_t cell_sss_re[CELL_NSSS][CELL_N], cell_sss_im[CELL_NSSS][CELL_N];
static int64_t cell_pss_e[3], cell_sss_e[CELL_NSSS];
static uint32_t cell_nid2, cell_nid1, cell_pss_best, cell_sss_best;
static uint32_t cell_pss_second, cell_sss_second;
static uint32_t cell_corr_idx;

static void cell_ms(uint8_t *x0, uint8_t *x1, uint8_t *x2,
                    uint8_t *x3, uint8_t *x4, uint8_t *x5)
{
    for (int i = 0; i < 31; i++) {
        x0[i] = x1[i] = x2[i] = x3[i] = x4[i] = x5[i] = 0;
    }
    x0[4] = x1[4] = x2[4] = x3[4] = x4[4] = x5[4] = 1;
    for (int i = 0; i < 26; i++) {
        x0[i + 5] = (uint8_t)((x0[i + 2] + x0[i]) & 1u);
        x1[i + 5] = (uint8_t)((x1[i + 3] + x1[i]) & 1u);
        x2[i + 5] = (uint8_t)((x2[i + 4] + x2[i + 2] + x2[i + 1] + x2[i]) & 1u);
        x3[i + 5] = (uint8_t)((x3[i + 4] + x3[i + 2] + x3[i + 1] + x3[i]) & 1u);
        x4[i + 5] = (uint8_t)((x4[i + 4] + x4[i + 3] + x4[i + 1] + x4[i]) & 1u);
        x5[i + 5] = (uint8_t)((x5[i + 4] + x5[i + 3] + x5[i + 1] + x5[i]) & 1u);
    }
}

static void cell_build_sss(uint8_t *x0, uint8_t *x1, uint8_t *x2,
                           uint8_t *x3, uint8_t *x4, uint8_t *x5)
{
    for (uint32_t nid1 = 0; nid1 < 168u; nid1++) {
        uint32_t m0 = nid1 % 31u;
        uint32_t m1 = (nid1 / 31u + m0 + 1u) % 31u;
        for (uint32_t nid2 = 0; nid2 < 3u; nid2++) {
            uint32_t h = nid1 * 3u + nid2;
            for (int n = 0; n < 31; n++) {
                int s0 = 1 - 2 * (int)x0[(n + (int)m0) % 31];
                int s1 = 1 - 2 * (int)x1[(n + (int)m1) % 31];
                int c0 = 1 - 2 * (int)x2[(n + (int)nid2) % 31];
                int c1 = 1 - 2 * (int)x3[(n + (int)nid2 + 3) % 31];
                int z0 = 1 - 2 * (int)x4[(n + (int)(m0 % 8u)) % 31];
                cell_sss_re[h][2 * n] = s0 * c0;
                cell_sss_im[h][2 * n] = 0;
                cell_sss_re[h][2 * n + 1] = s1 * c1 * z0;
                cell_sss_im[h][2 * n + 1] = 0;
            }
        }
    }
}

static int cell_setup(const uint32_t *par)
{
    uint8_t x0[31], x1[31], x2[31], x3[31], x4[31], x5[31];
    (void)par;
    for (int u = 0; u < 3; u++)
        for (int n = 0; n < CELL_N; n++) {
            cell_pss_re[u][n] = lte_pss_re[u][n];
            cell_pss_im[u][n] = lte_pss_im[u][n];
        }
    cell_ms(x0, x1, x2, x3, x4, x5);
    cell_build_sss(x0, x1, x2, x3, x4, x5);
    cell_nid2 = cell_nid1 = 0;
    cell_pss_best = cell_sss_best = 0;
    cell_pss_second = cell_sss_second = 0;
    cell_corr_idx = 0;
    for (int i = 0; i < 3; i++) cell_pss_e[i] = 0;
    for (int i = 0; i < CELL_NSSS; i++) cell_sss_e[i] = 0;
    return 0;
}

static int cell_received(void) { return 0; }

static int cell_dmrs(void) { return 0; }

static int cell_correlate_pss(void)
{
    int64_t best = -1, second = -1;
    for (int u = 0; u < 3; u++) {
        int64_t ar = 0, ai = 0;
        for (int n = 0; n < CELL_N; n++) {
            int64_t yr = (int32_t)CELL_IN[2 * n];
            int64_t yi = (int32_t)CELL_IN[2 * n + 1];
            int64_t dr = cell_pss_re[u][n], di = cell_pss_im[u][n];
            ar += yr * dr + yi * di;
            ai += yi * dr - yr * di;
        }
        cell_pss_e[u] = ar * ar + ai * ai;
        if (cell_pss_e[u] > best) {
            second = best;
            best = cell_pss_e[u];
            cell_nid2 = (uint32_t)u;
        } else if (cell_pss_e[u] > second) {
            second = cell_pss_e[u];
        }
    }
    cell_pss_best = (uint32_t)(best >> 24);
    cell_pss_second = (uint32_t)(second >> 24);
    return 0;
}

static int cell_correlate_sss(void)
{
    int64_t best = -1, second = -1;
    for (uint32_t h = 0; h < CELL_NSSS; h++) {
        int64_t ar = 0, ai = 0;
        for (int n = 0; n < CELL_N; n++) {
            int64_t yr = (int32_t)CELL_IN[124 + 2 * n];
            int64_t yi = (int32_t)CELL_IN[124 + 2 * n + 1];
            int64_t dr = cell_sss_re[h][n];
            ar += yr * dr;
            ai += yi * dr;
        }
        cell_sss_e[h] = ar * ar + ai * ai;
        if (cell_sss_e[h] > best) {
            second = best;
            best = cell_sss_e[h];
            cell_sss_best = h;
        } else if (cell_sss_e[h] > second) {
            second = cell_sss_e[h];
        }
    }
    cell_nid1 = cell_sss_best / 3u;
    cell_sss_second = (uint32_t)(second >> 24);
    return 0;
}

static int cell_correlate(void)
{
    if (cell_corr_idx++ == 0)
        return cell_correlate_pss();
    return cell_correlate_sss();
}

#endif /* LTE_CELL_STAGES_INC */
