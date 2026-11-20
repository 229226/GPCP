/* -----------------------------------------------------------------------------
 * ZC 循环相关搜索（PRACH Msg1 / PUCCH 格式 0 共用）——docs/protocols/README.md 阶段 2。
 *
 * 微码 prach_corr（cgra/kernels/prach_corr.yaml，129 拍 / 16 PE）：
 *   候选 d 的参考序列 -> spad REF；阵列算 16 个 PE 的部分和并在阵列内
 *   butterfly 归约成 (re,im)；CPU 再从主机窗口读回 16 个分部复核（nchk）。
 * 一次候选 = 一次阵列运行（PRACH 139 个移位、PUCCH 2 个 m_CS）。
 *
 * 激励（两场景同构，由 tools/models/nr_rach.py / nr_pucch.py dump）：
 *   word 0 .. 277            : 139 个接收 (re,im)
 *   word 278 + 254*d ..      : 候选 d 的 127 个参考抽头
 *
 * 使用前必须 include：gpcp_cgra.h / gpcp_cgra_rt.h / prach_corr_prog.h /
 * prach_corr_layout.h。
 * -------------------------------------------------------------------------- */

#define ZC_L_RA       139u
#define ZC_TAPS       127u
#define ZC_PAIR_WORDS (2u * ZC_TAPS)     /* 254: 一个复数抽头块 */
#define ZC_PART_WORDS 64u
#define ZC_TD_BASE    0x80400000ul
#define ZC_PART_BASE  0x80300000ul
#define ZC_EXIT_ADDR  0x40010000ul
#define CGRA_BASE     0x50000000ul
/* 运行器契约（手写基准另用自己的 RESULT 宏，互不影响） */
#define PARAM_BASE    0x80600000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)
#define ZC_Y_SPAD     (GPCP_LAYOUT_REGION_IN * GPCP_LAYOUT_WORDS)
#define ZC_REF_SPAD   (GPCP_LAYOUT_REGION_REF * GPCP_LAYOUT_WORDS)
#define ZC_PART_SPAD  (GPCP_LAYOUT_REGION_OUT * GPCP_LAYOUT_WORDS)

static uint32_t errs, runs, dmas, nchk, busy;
static int64_t zc_re[ZC_L_RA], zc_im[ZC_L_RA], zc_pw[ZC_L_RA];
static uint32_t zc_best_d, zc_second_d, zc_win;
static int64_t zc_best_pw, zc_second_pw;

static inline uint32_t mtime(void) { return *(volatile uint32_t *)0x0200BFF8ul; }

static int zc_setup(const uint32_t *par)
{
    (void)par;                          /* 激励没有头，参数都在样本里 */
    errs = runs = dmas = nchk = busy = 0;
    zc_best_d = zc_second_d = zc_win = 0;
    zc_best_pw = zc_second_pw = -1;
    return 0;
}

static int zc_dma(uintptr_t ram, uint32_t spad, uint32_t words, uint32_t dir)
{
    if (rt_dma_run(CGRA_BASE, ram, spad, words, 0, dir, 0, 0)) {
        errs++;
        return -1;
    }
    dmas++;
    return 0;
}

/* 阶段 received：装微码 + 接收序列常驻 spad（139 个 chip）。 */
static int zc_received(void)
{
    if (gpcp_cgra_imem_load(CGRA_BASE, prach_corr_prog,
                            PRACH_CORR_PROG_CYCLES * GPCP_LAYOUT_NPES) != 0)
        errs++;
    return zc_dma(ZC_TD_BASE, ZC_Y_SPAD, ZC_PAIR_WORDS, 0) == 0 ? 0 : 1;
}

/* 阶段 dmrs：候选参考序列随激励一起进来，这里不重算。 */
static int zc_dmrs(void) { return 0; }

#ifdef PROTO_CPU_ONLY
/* 与阵列同一口径：MUL 取 sext16，累加按 32-bit 回卷；每个 PE 8 tap。 */
static int zc_sx16(int32_t v)
{
    v &= 0xFFFF;
    return (v & 0x8000) ? v - 0x10000 : v;
}

static void zc_corr_cpu(uint32_t n_cand)
{
    const int32_t *y = (const int32_t *)ZC_TD_BASE;
    const int32_t *refs = (const int32_t *)(ZC_TD_BASE + 4u * (2u * ZC_L_RA));
    for (uint32_t d = 0; d < n_cand; d++) {
        int32_t *part = (int32_t *)(ZC_PART_BASE + d * ZC_PART_WORDS * 4u);
        for (uint32_t p = 0; p < GPCP_LAYOUT_NPES; p++) {
            int32_t re = 0, im = 0;
            for (uint32_t k = 0; k < 8; k++) {
                uint32_t t = 8u * p + k;
                int32_t rr = (t < ZC_TAPS) ? refs[d * ZC_PAIR_WORDS + 2u * t] : 0;
                int32_t ri = (t < ZC_TAPS) ? refs[d * ZC_PAIR_WORDS + 2u * t + 1u] : 0;
                int32_t yr = y[2u * t], yi = y[2u * t + 1u];
                int32_t pr = (int32_t)(zc_sx16(yr) * zc_sx16(rr)
                                     + zc_sx16(yi) * zc_sx16(ri));
                int32_t pi = (int32_t)(zc_sx16(yi) * zc_sx16(rr)
                                     - zc_sx16(yr) * zc_sx16(ri));
                re = (int32_t)((uint32_t)re + (uint32_t)pr);
                im = (int32_t)((uint32_t)im + (uint32_t)pi);
            }
            part[2u * p] = re;
            part[2u * p + 1u] = im;
        }
    }
}
#endif

/* 阶段 correlate：n_cand 个候选，每个一次阵列运行 + 一次分部读回。 */
static int zc_correlate(uint32_t n_cand)
{
#ifdef PROTO_CPU_ONLY
    zc_corr_cpu(n_cand);
    return 0;
#else
    uintptr_t refs = ZC_TD_BASE + 4u * (2u * ZC_L_RA);
    for (uint32_t d = 0; d < n_cand; d++) {
        if (zc_dma(refs + 4u * d * ZC_PAIR_WORDS, ZC_REF_SPAD, ZC_PAIR_WORDS, 0) != 0)
            return 1;
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(
                            PRACH_CORR_SPAD_LEAF_R7(GPCP_LAYOUT_NPES - 1u)), 0);
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(
                            PRACH_CORR_SPAD_LEAF_RI7(GPCP_LAYOUT_NPES - 1u)), 0);
        if (rt_array_run(CGRA_BASE, 0, PRACH_CORR_PROG_CYCLES) != 0) {
            errs++;
            return 1;
        }
        runs++;
        busy += PRACH_CORR_PROG_CYCLES;
        if (zc_dma(ZC_PART_BASE + d * ZC_PART_WORDS * 4u, ZC_PART_SPAD,
                   ZC_PART_WORDS, 1) != 0)
            return 1;
    }
    return 0;
#endif
}

/* CPU 复核 16 个 PE 分部并求和；part_out != 0 时逐分部导出（run-pucch 的
 * 64 个分部逐位对账）。 */
static void zc_reduce(uint32_t n_cand, uint32_t *part_out)
{
    for (uint32_t d = 0; d < n_cand; d++) {
        const int32_t *s = (const int32_t *)(ZC_PART_BASE + d * ZC_PART_WORDS * 4u);
        int64_t re = 0, im = 0;
        for (uint32_t p = 0; p < GPCP_LAYOUT_NPES; p++) {
            re += s[2 * p];
            im += s[2 * p + 1];
            nchk += 2;
            if (part_out) {
                part_out[32 * d + 2 * p] = (uint32_t)s[2 * p];
                part_out[32 * d + 2 * p + 1] = (uint32_t)s[2 * p + 1];
            }
        }
        zc_re[d] = re;
        zc_im[d] = im;
        zc_pw[d] = re * re + im * im;
    }
}

/* PRACH：最大 / 次大能量（裕度用）。 */
static void zc_verdict_argmax(uint32_t n_cand)
{
    zc_best_pw = -1;
    zc_second_pw = -1;
    for (uint32_t d = 0; d < n_cand; d++) {
        if (zc_pw[d] > zc_best_pw) {
            zc_second_pw = zc_best_pw;
            zc_second_d = zc_best_d;
            zc_best_pw = zc_pw[d];
            zc_best_d = d;
        } else if (zc_pw[d] > zc_second_pw) {
            zc_second_pw = zc_pw[d];
            zc_second_d = d;
        }
    }
}

/* PUCCH：两个候选比能量，候选 0（m_CS = 0）= ACK/SR。 */
static uint32_t zc_verdict_two(void)
{
    zc_win = zc_pw[0] >= zc_pw[1] ? 0u : 1u;
    return zc_win;
}

/* 完整搜索：相关 + 归约 + 判决。argmax=1 走 PRACH 的最大/次大。 */
static int zc_search(uint32_t n_cand, uint32_t *part_out, int argmax)
{
    if (zc_correlate(n_cand) != 0)
        return 1;
    zc_reduce(n_cand, part_out);
    if (argmax)
        zc_verdict_argmax(n_cand);
    else
        (void)zc_verdict_two();
    return 0;
}
