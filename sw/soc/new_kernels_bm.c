/* -----------------------------------------------------------------------------
 * 新增 CGRA kernel 的 SoC 自检 bench（docs/22 待补项的硬件验收）
 *
 * 把 7 张由 cgra/tools/gen_kernel_prog.py 生成的镜像依次装进 IMEM，
 * 按各自 *_layout.h 的逻辑槽位写 spad，跑一次阵列，读回全部汇值，
 * 与 C 侧按算法规格独立算出的黄金值逐值比对。
 *
 * 结果区 0x80010000：
 *   [0] marker 'GKCN'  [1] errors  [2] cases  [3] array_runs  [4] 0x600D
 *   [8 + s]        = 第 s 个汇的实测值 (got)
 *   [8 + 1024 + s] = 第 s 个汇的黄金值 (want)
 * tools/check_new_kernels_hw.py 读 dump 复核 got==want 且 errors==0。
 *
 * 铁律：微码只 include 生成物，绝不手写；地址只引用生成的 *_layout.h
 *       与 gpcp_cgra.h 的 ABI 宏，不写死物理坐标。
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"

#include "qam_qpsk_prog.h"
#include "qam_16qam_prog.h"
#include "qam_64qam_prog.h"
#include "ldpc_enc_acc_prog.h"
#include "turbo_acs_prog.h"
#include "wifi_vn_prog.h"
#include "wifi_cn24_prog.h"
#include "qam_qpsk_layout.h"
#include "qam_16qam_layout.h"
#include "qam_64qam_layout.h"
#include "ldpc_enc_acc_layout.h"
#include "turbo_acs_layout.h"
#include "wifi_vn_layout.h"

#define CGRA_BASE   0x50000000ul
#define EXIT_ADDR   0x40010000ul
#define RESULT_BASE ((volatile uint32_t *)0x80010000ul)
#define NPES        16u
#define WANT_OFF    1024u

static uint32_t errors, cases_done, runs;

static inline void spad_w(uint32_t flat, int32_t v)
{
    mmio_w(CGRA_BASE + GPCP_CGRA_SPAD_BASE + flat * 4u, (uint32_t)v);
}
static inline int32_t spad_r(uint32_t flat)
{
    return (int32_t)mmio_r(CGRA_BASE + GPCP_CGRA_SPAD_BASE + flat * 4u);
}
static inline uint32_t spad_flat(uint32_t bank, uint32_t word)
{
    return GPCP_SPAD_IDX(bank, word);
}

/* 确定性 LCG，保证 C 侧激励可复算（与 Python 侧用不同激励，互为交叉） */
static uint32_t lcg;
static void seed(uint32_t s) { lcg = s; }
static uint32_t rnd(void)
{
    lcg = lcg * 1664525u + 1013904223u;
    return lcg;
}

static void clear_spad(void)
{
    for (uint32_t i = 0; i < 1024u; i++)
        spad_w(i, 0);
}

static void array_run(const uint32_t *prog, uint32_t cycles)
{
    if (gpcp_cgra_imem_load(CGRA_BASE, prog, cycles * NPES) != 0) {
        errors++;
        return;
    }
    /* 短镜像必须清 DONE + 回读确认，否则会读到上一轮的完成位 */
    uint32_t g = 0;
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_CTRL, GPCP_CGRA_CTRL_CLRDONE);
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE)
        if (++g > 100000u) { errors++; break; }
    gpcp_cgra_array_start(CGRA_BASE, 0, cycles);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    g = 0;
    while (!(mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE))
        if (++g > 200000u) { errors++; break; }
    runs++;
}

static void expect(uint32_t slot, int32_t got, int32_t want)
{
    RESULT_BASE[8u + slot] = (uint32_t)got;
    RESULT_BASE[8u + WANT_OFF + slot] = (uint32_t)want;
    if (got != want)
        errors++;
}

/* ------------------------------------------------------------ QAM（38.211） */
static int branch_level(const uint8_t *b, uint32_t n)
{
    /* 与 tools/nr_qam.py 的 MAGNITUDE 表一致，但按标准公式独立写出 */
    int mag;
    if (n == 1)      mag = 1;
    else if (n == 2) mag = 3 - 2 * b[1];
    else {
        uint32_t gxi = b[1] ^ b[2];
        uint32_t val = 2u * b[1] + gxi;
        mag = (int)(1 + 2 * val);
    }
    return (b[0] ? -1 : 1) * mag;
}

static void case_qam(const uint32_t *prog, uint32_t cycles,
                     uint32_t n, int32_t amp,
                     uint32_t leaf_b0, uint32_t leaf_b1, uint32_t leaf_b2,
                     uint32_t leaf_q0, uint32_t leaf_q1, uint32_t leaf_q2,
                     uint32_t leaf_one, uint32_t leaf_three, uint32_t leaf_amp,
                     uint32_t sink_i, uint32_t sink_q, uint32_t idx)
{
    clear_spad();
    seed(0xC0FFEEu + 2u * n);
    uint8_t bits[NPES][6];
    for (uint32_t pe = 0; pe < NPES; pe++) {
        for (uint32_t k = 0; k < 2u * n; k++)
            bits[pe][k] = (uint8_t)(rnd() & 1u);
        spad_w(leaf_one + 32u * pe, 1);
        if (n == 2) spad_w(leaf_three + 32u * pe, 3);
        spad_w(leaf_amp + 32u * pe, amp);
        spad_w(leaf_b0 + 32u * pe, bits[pe][0]);
        spad_w(leaf_q0 + 32u * pe, bits[pe][n]);
        if (n >= 2) {
            spad_w(leaf_b1 + 32u * pe, bits[pe][1]);
            spad_w(leaf_q1 + 32u * pe, bits[pe][n + 1]);
        }
        if (n >= 3) {
            spad_w(leaf_b2 + 32u * pe, bits[pe][2]);
            spad_w(leaf_q2 + 32u * pe, bits[pe][n + 2]);
        }
    }
    array_run(prog, cycles);
    for (uint32_t pe = 0; pe < NPES; pe++) {
        int32_t wi = branch_level(bits[pe], n) * amp;
        int32_t wq = branch_level(&bits[pe][n], n) * amp;
        expect(idx * 32u + 2u * pe,
               spad_r(sink_i + 32u * pe), wi);
        expect(idx * 32u + 2u * pe + 1u,
               spad_r(sink_q + 32u * pe), wq);
    }
    cases_done++;
}

/* ------------------------------------------------------------ LDPC 编码 acc */
static void case_ldpc_acc(void)
{
    clear_spad();
    seed(0x1D9Cu);
    uint32_t want[NPES];
    for (uint32_t pe = 0; pe < NPES; pe++) {
        uint32_t v = 0;
        spad_w(LDPC_ENC_ACC_SPAD_LEAF_ZERO(pe), 0);
        for (uint32_t k = 0; k < 20u; k++) {
            uint32_t e = rnd();
            v ^= e;
            spad_w(LDPC_ENC_ACC_SPAD_LEAF_E0_BASE + k + 32u * pe, (int32_t)e);
        }
        want[pe] = v;
    }
    array_run(ldpc_enc_acc_prog, LDPC_ENC_ACC_PROG_CYCLES);
    for (uint32_t pe = 0; pe < NPES; pe++)
        expect(3u * 32u + pe,
               spad_r(LDPC_ENC_ACC_SPAD_SINK_ACC(pe)),
               (int32_t)want[pe]);
    cases_done++;
}

/* ------------------------------------------------------------ Turbo 单步 ACS */
static void case_turbo(void)
{
    clear_spad();
    seed(0x7B0u);
    int32_t v[NPES][4], want[NPES];
    for (uint32_t pe = 0; pe < NPES; pe++) {
        for (uint32_t k = 0; k < 4u; k++) {
            v[pe][k] = (int32_t)(rnd() & 0x1FFFFFu) - (1 << 20);
            /* a0,g0,a1,g1 依次占 slot 0..3 */
            spad_w(TURBO_ACS_SPAD_LEAF_A0_BASE + k + 32u * pe, v[pe][k]);
        }
        int32_t a = v[pe][0] + v[pe][1];
        int32_t b = v[pe][2] + v[pe][3];
        want[pe] = a > b ? a : b;
    }
    array_run(turbo_acs_prog, TURBO_ACS_PROG_CYCLES);
    for (uint32_t pe = 0; pe < NPES; pe++)
        expect(4u * 32u + pe,
               spad_r(TURBO_ACS_SPAD_SINK_M(pe)), want[pe]);
    cases_done++;
}

/* ------------------------------------------------------------ WiFi 变量节点 */
static void case_wifi_vn(void)
{
    clear_spad();
    seed(0xF1Eu);
    int32_t m[NPES][24], total[NPES];
    for (uint32_t pe = 0; pe < NPES; pe++) {
        int32_t t = (int32_t)(rnd() % 6001u) - 3000;
        spad_w(WIFI_VN_SPAD_LEAF_CH(pe), t);
        spad_w(WIFI_VN_SPAD_LEAF_ZERO(pe), 0);
        for (uint32_t k = 0; k < 24u; k++) {
            int32_t x = (int32_t)(rnd() % 6001u) - 3000;
            m[pe][k] = x;
            spad_w(WIFI_VN_SPAD_LEAF_M0_BASE + k + 32u * pe, x);
            t += x;
        }
        total[pe] = t;
    }
    array_run(wifi_vn_prog, WIFI_VN_PROG_CYCLES);
    for (uint32_t pe = 0; pe < NPES; pe++) {
        for (uint32_t k = 0; k < 24u; k++)
            expect(5u * 32u + pe * 25u + k,
                   spad_r(WIFI_VN_SPAD_SINK_O0_BASE + k + 32u * pe),
                   total[pe] - m[pe][k]);
        expect(5u * 32u + pe * 25u + 24u,
               spad_r(WIFI_VN_SPAD_SINK_SG(pe)),
               total[pe] >> 31);
    }
    cases_done++;
}

/* ------------------------------------------------------------ WiFi 校验节点 */
static void case_wifi_cn(void)
{
    clear_spad();
    seed(0xC4u);
    int32_t want[20][3];
    for (uint32_t w = 0; w < 20u; w++) {
        uint32_t neg = 0;
        int32_t a = 32767, b = 32767;
        for (uint32_t k = 0; k < CN_EDGES; k++) {
            int32_t v = (int32_t)(rnd() % 6001u) - 3000;
            spad_w(spad_flat(k, w), v);       /* 边 k 在 bank k、word w */
            int32_t av = v < 0 ? (neg++, -v) : v;
            if (av < a) { b = a; a = av; }
            else if (av < b) { b = av; }
        }
        spad_w(spad_flat(CN_SENTINEL_BANK, w), 32767);
        want[w][0] = a;
        want[w][1] = b;
        want[w][2] = (neg & 1u) ? -1 : 0;
    }
    array_run(cn_prog, CN_PROG_CYCLES);
    const uint32_t rb[3] = { CN_MIN1_BANK, CN_MIN2_BANK, CN_SIGN_BANK };
    for (uint32_t w = 0; w < 20u; w++)
        for (uint32_t k = 0; k < 3u; k++)
            expect(21u * 32u + w * 3u + k,
                   spad_r(spad_flat(rb[k], w)), want[w][k]);
    cases_done++;
}

int main(void)
{
    case_qam(qam_qpsk_prog, QAM_QPSK_PROG_CYCLES, 1, 2000,
             QAM_QPSK_SPAD_LEAF_B0_BASE, 0, 0,
             QAM_QPSK_SPAD_LEAF_Q0_BASE, 0, 0,
             QAM_QPSK_SPAD_LEAF_ONE_BASE, 0, QAM_QPSK_SPAD_LEAF_AMP_BASE,
             QAM_QPSK_SPAD_SINK_ILEV_BASE, QAM_QPSK_SPAD_SINK_QLEV_BASE, 0);
    case_qam(qam_16qam_prog, QAM_16QAM_PROG_CYCLES, 2, 894,
             QAM_16QAM_SPAD_LEAF_B0_BASE, QAM_16QAM_SPAD_LEAF_B1_BASE, 0,
             QAM_16QAM_SPAD_LEAF_Q0_BASE, QAM_16QAM_SPAD_LEAF_Q1_BASE, 0,
             QAM_16QAM_SPAD_LEAF_ONE_BASE, QAM_16QAM_SPAD_LEAF_THREE_BASE,
             QAM_16QAM_SPAD_LEAF_AMP_BASE,
             QAM_16QAM_SPAD_SINK_ILEV_BASE, QAM_16QAM_SPAD_SINK_QLEV_BASE, 1);
    case_qam(qam_64qam_prog, QAM_64QAM_PROG_CYCLES, 3, 436,
             QAM_64QAM_SPAD_LEAF_B0_BASE, QAM_64QAM_SPAD_LEAF_B1_BASE,
             QAM_64QAM_SPAD_LEAF_B2_BASE,
             QAM_64QAM_SPAD_LEAF_Q0_BASE, QAM_64QAM_SPAD_LEAF_Q1_BASE,
             QAM_64QAM_SPAD_LEAF_Q2_BASE,
             QAM_64QAM_SPAD_LEAF_ONE_BASE, 0, QAM_64QAM_SPAD_LEAF_AMP_BASE,
             QAM_64QAM_SPAD_SINK_ILEV_BASE, QAM_64QAM_SPAD_SINK_QLEV_BASE, 2);
    case_ldpc_acc();
    case_turbo();
    case_wifi_vn();
    case_wifi_cn();

    RESULT_BASE[0] = 0x4E4B4347u;      /* 'GKCN' */
    RESULT_BASE[1] = errors;
    RESULT_BASE[2] = cases_done;
    RESULT_BASE[3] = runs;
    RESULT_BASE[4] = 0x600Du;
    mmio_w(EXIT_ADDR, 0x600Du);
    return 0;
}
