/* -----------------------------------------------------------------------------
 * 2x2 MIMO PDSCH 接收的「阶段实现」——被两个程序共用：
 *   sw/soc/mimo_bm.c          人写参照基准（run-mimo）
 *   sw/soc/proto_runner.c     协议运行器（docs/20 阶段 2 迁入）
 *
 * 阶段与 §2 的动词对应：received -> dmrs -> ofdm_demodulate -> estimate
 * -> mimo_equalise。b = H^H y 由 mmse_2x1 tile 在阵列上算，2x2 求解与硬判在 CPU。
 *
 * 使用前必须 include：gpcp_cgra.h / soc_io.h / gpcp_cgra_rt.h /
 * mmse_2x1_prog.h / mmse_2x1_layout.h（由 gen_kernel_prog.py 生成）。
 * -------------------------------------------------------------------------- */

#ifndef MMSE_2X1_LEAF_MAX_ABS
#error "mmse_2x1_prog.h 缺契约宏：先跑 make kernels（数值契约由编译器生成）"
#endif

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

#define TD_RAM        0x80400000ul
#define PARAM_BASE    0x80600000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)
#define CGRA_BASE     0x50000000ul

#include "mmse_full_prog.h"
#include "mmse_full_layout.h"

/* 与 cgra/kernels/mmse_full.yaml::contract 一致（mmse_full 是 kind:image，
   check_contract 只支持 kind:dfg，所以这个上界在这里显式声明。 */
#define MS_LEAF_MAX_ABS 20000

static inline uint32_t mtime(void) { return *(volatile uint32_t *)0x0200BFF8ul; }

static uint32_t errs, runs, dmas, busy;
static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

static uint32_t gold_x1[GOLD_LEN], gold_x2[GOLD_LEN];

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
#ifdef PROTO_CPU_ONLY
#include "fft_cpu.inc"
#endif

/* complex helpers on (re, im) pairs */
static int64_t cm_re(int64_t ar, int64_t ai, int64_t br, int64_t bi)
{ return ar * br - ai * bi; }
static int64_t cm_im(int64_t ar, int64_t ai, int64_t br, int64_t bi)
{ return ar * bi + ai * br; }

/* ---------------------------------------------------------------- CGRA MMSE */
/* b = H^H y 由 CGRA 的 mmse_2x1 tile 计算：一次 16 个 RE（一个 PE 一个 RE），
 * 每个 RE 喂 8 个 Q15 叶子，回 2 个 Q30 汇（numr/numi）。板级验证时 *16 还原到
 * C 参考的 FRONT_GAIN 量纲，因此 2x2 求解与纯 C 版逐位一致。 */
static uint32_t mmse_runs, mmse_dmas;

/* 描述符环：把 180 个 RE 的接收样本直接搬进 spad，CPU 不再经手数据。
 *
 * 地址在整段运行里固定（sp[] 由 TD_RAM 决定），描述符表只建一次；之后每趟只写
 * 一次 RING_TAIL。搬运引擎按 64 位 beat 取数，所以一条描述符搬 I+Q 两个**8 字节
 * 对齐**的连续字（只搬奇数地址的 Q 会拿回低半部分 = I，实测虚部全错）。
 * spad 侧对应 stride 2 布局（YAML 的 pe_per_bank: 16）：PE k 占 2k / 2k+1。
 * 奇偶趟分别落 page 1 / page 0，阵列读另一页时引擎写这一页。 */
#define MS_TILES   ((NDATA + 15) / 16)
static void mmse_prog_load(void)
{
    if (gpcp_cgra_imem_load(CGRA_BASE, mmse_full_prog,
                            MMSE_FULL_PROG_CYCLES * NPES) != 0)
        errs++;
}

/* 88 拍的阵列太短：必须"清 DONE + 回读确认 + 轮询本次 DONE"。 */
static void mmse_array_start(uint32_t page)
{
    uint32_t g = 0;
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_CTRL, GPCP_CGRA_CTRL_CLRDONE);
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE)
        if (++g > 100000u) { errs++; break; }
    /* CVA6 有写缓冲：START 会抢在前面的 store 之前到达 */
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    gpcp_cgra_array_start(CGRA_BASE, page, MMSE_FULL_PROG_CYCLES);
}

static void mmse_array_wait(void)
{
    uint32_t g = 0;
    while (!(mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE))
        if (++g > 200000u) { errs++; break; }
    mmse_runs++;
    runs++;
    busy += MMSE_FULL_PROG_CYCLES;
}

/* --------------------------------------------------------- 阶段状态与激励 */
static int32_t ms_pre[2][NP_LAYER], ms_pim[2][NP_LAYER];
static int64_t ms_hre[2][2], ms_him[2][2];

/* 13 个广播叶子：8 个信道系数 + g00/g11/gr/gi + 常量 1。
   全部 PE 读同一个字，两页各写一份。g 元素也必须 <= 32767，否则 mul 会
   把高 16 位静默截掉（硬件没有溢出标志）。 */
static int64_t ms_g00r, ms_g01r, ms_g01i, ms_g11r, ms_lam, ms_detr, ms_deti, ms_dd;
static int64_t ms_hmax;
static int ms_leaf_ovf;
static void ms_h_bcast(void)
{
    const uint32_t b0 = MMSE_FULL_SPAD_CORR_LEAVES_H00R_BASE;
    int32_t v[13];
    v[0] = (int32_t)ms_hre[0][0]; v[1] = (int32_t)ms_him[0][0];
    v[2] = (int32_t)ms_hre[1][0]; v[3] = (int32_t)ms_him[1][0];
    v[4] = (int32_t)ms_hre[0][1]; v[5] = (int32_t)ms_him[0][1];
    v[6] = (int32_t)ms_hre[1][1]; v[7] = (int32_t)ms_him[1][1];
    v[8] = (int32_t)ms_g00r;  v[9] = (int32_t)ms_g11r;
    v[10] = (int32_t)ms_g01r; v[11] = (int32_t)ms_g01i;
    v[12] = 1;
    ms_hmax = 0;
    ms_leaf_ovf = 0;
    for (uint32_t j = 0; j < 13u; j++) {
        int64_t a = v[j] < 0 ? -(int64_t)v[j] : (int64_t)v[j];
        if (j < 8u && a > ms_hmax) ms_hmax = a;   /* 仅 h 计入 |b| 上界 */
        if (a > 32767) ms_leaf_ovf = 1;   /* 16 位 mul 输入截断，回退 */
    }
    for (uint32_t pg = 0; pg < 2u; pg++) {
        uint32_t off = pg * 1024u;
        for (uint32_t j = 0; j < 13u; j++)
            mmio_w(CGRA_BASE + GPCP_SPAD_FLAT_ADDR(b0 + j + off), (uint32_t)v[j]);
    }
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
}
static uint32_t ms_bits[NDATA];
static uint32_t ms_bit_err;

/* 激励是裸的两块时域样本（没有 magic 头）。 */
static int ms_setup(const uint32_t *par)
{
    (void)par;
    errs = runs = dmas = mmse_runs = mmse_dmas = busy = 0;
    ms_bit_err = 0;
    return 0;
}

/* ------------------------------------------------ 阶段 0：接收（两块 TD 常驻） */
static int ms_received(void) { return 0; }

/* ------------------------------- 阶段 1：解调参考信号（两层 FDM 导频序列） */
static int ms_dmrs(void)
{
    const uint32_t cinits[2] = { CSI_CINIT0, CSI_CINIT1 };
    for (int l = 0; l < 2; l++) {
        gold_init(cinits[l]);
        for (int i = 0; i < NP_LAYER; i++) {
            int32_t c0 = (int32_t)gold_bit((uint32_t)(2 * i));
            int32_t c1 = (int32_t)gold_bit((uint32_t)(2 * i + 1));
            ms_pre[l][i] = (1 - 2 * c0) * QPSK;
            ms_pim[l][i] = (1 - 2 * c1) * QPSK;
        }
    }
    return 0;
}

/* --------------------------- 阶段 2：OFDM 解调 [array]（两个天线各一次 FFT） */
static int ms_ofdm_demodulate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    for (int rx = 0; rx < 2; rx++) {
#ifdef PROTO_CPU_ONLY
        fft4096_cpu(td + (size_t)rx * SYM_WORDS + 2 * CP_LEN);
#else
        fft4096(td + (size_t)rx * SYM_WORDS + 2 * CP_LEN);
#endif
    }
    return 0;
}

/* ------------------------- 阶段 3：信道估计（每层 30 个 FDM 导频的 LS） */
static int ms_estimate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    int32_t *sp[2];
    const int64_t den = (int64_t)NP_LAYER * 2 * QPSK * QPSK / H_SCALE;

    for (int rx = 0; rx < 2; rx++)
        sp[rx] = td + (size_t)rx * SYM_WORDS + 2 * CP_LEN;

    for (int rx = 0; rx < 2; rx++) {
        int64_t accr[2] = { 0, 0 }, acci[2] = { 0, 0 };
        int idx = 0;
        for (int r = 0; r < NRE; r += 4) {
            uint32_t ip = rev4(BIN0 + (uint32_t)r);
            int64_t yr = (int64_t)sp[rx][2 * ip] << FRONT_GAIN;
            int64_t yi = (int64_t)sp[rx][2 * ip + 1] << FRONT_GAIN;
            int l = idx & 1, p = idx >> 1;
            accr[l] += yr * ms_pre[l][p] + yi * ms_pim[l][p];
            acci[l] += yi * ms_pre[l][p] - yr * ms_pim[l][p];
            idx++;
        }
        for (int l = 0; l < 2; l++) {
            ms_hre[rx][l] = accr[l] / den;
            ms_him[rx][l] = acci[l] / den;
        }
    }

    /* G = H^H H, one lambda for both diagonal terms */
    ms_g00r = ms_g01r = ms_g01i = ms_g11r = 0;
    for (int rx = 0; rx < 2; rx++) {
        int64_t a_r = ms_hre[rx][0], a_i = ms_him[rx][0];
        int64_t b_r = ms_hre[rx][1], b_i = ms_him[rx][1];
        ms_g00r += a_r * a_r + a_i * a_i;
        ms_g11r += b_r * b_r + b_i * b_i;
        ms_g01r += cm_re(a_r, -a_i, b_r, b_i);       /* conj(a) * b */
        ms_g01i += cm_im(a_r, -a_i, b_r, b_i);
    }
    ms_lam = (ms_g00r + ms_g11r) / 64;
    if (ms_lam < 1) ms_lam = 1;
    ms_g00r += ms_lam;
    ms_g11r += ms_lam;

    /* det = g00*g11 - g01*conj(g01)  (real, since G is Hermitian) */
    {
        int64_t g10r = ms_g01r, g10i = -ms_g01i;
        ms_detr = ms_g00r * ms_g11r - cm_re(ms_g01r, ms_g01i, g10r, g10i);
        ms_deti = 0 - cm_im(ms_g01r, ms_g01i, g10r, g10i);
        ms_dd = ms_detr * ms_detr + ms_deti * ms_deti;
        if (ms_dd <= 0) { ms_dd = 1; errs++; }
    }
    return 0;
}

/* --------- 阶段 4：多天线均衡 [array]（b = H^H y on CGRA + 2x2 solve） */
static int ms_mimo_equalise(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    int32_t *sp[2];
    int dre[NDATA], nd = 0;
    int cg_ok = 1;

    for (int rx = 0; rx < 2; rx++)
        sp[rx] = td + (size_t)rx * SYM_WORDS + 2 * CP_LEN;

    /* data-RE index table（pilots every 4th RE） */
    for (int rr = 0; rr < NRE && nd < NDATA; rr++)
        if (rr % 4 != 0) dre[nd++] = rr;

    /* 数值契约：MMSE_2X1_LEAF_MAX_ABS 由 gen_kernel_prog 从 YAML contract 静态
     * 证明（|leaf| <= 该值时 mul 不截断、4 项 Q30 求和不回绕）。这里只查 4 个
     * 信道系数（几乎免费）；180 个数据位置的检查合并进下面的 gather —— 同一批
     * 数不再按 rev4 乱序重扫一遍（那段实测约 23,415 周期）。 */
    {
        int64_t hmax = 0;
        for (int rx = 0; rx < 2; rx++)
            for (int l = 0; l < 2; l++) {
                int64_t a = ms_hre[rx][l] < 0 ? -ms_hre[rx][l] : ms_hre[rx][l];
                int64_t b = ms_him[rx][l] < 0 ? -ms_him[rx][l] : ms_him[rx][l];
                if (a > hmax) hmax = a;
                if (b > hmax) hmax = b;
            }
        if (hmax > MS_LEAF_MAX_ABS)
            cg_ok = 0;
    }
#ifdef PROTO_CPU_ONLY
    cg_ok = 0;                  /* CPU 对照路径：直接走下面的标量相关 */
#endif
    if (cg_ok) {
        mmse_prog_load();
        ms_h_bcast();
        if (ms_leaf_ovf) cg_ok = 0;   /* g/h 叶子越 16 位，回退 CPU 标量相关 */

        for (int t = 0; t < MS_TILES; t++) {
            int base = t * 16;
            int n = NDATA - base; if (n > 16) n = 16;
            const uint32_t pg = 0u, off = 0u;
            int bad = 0;

            /* y 由 CPU 直接写进 spad（I/Q 交错：PE k 占 2k / 2k+1）。
             * 不使用搬运引擎：它的描述符环会静默漏做一段描述符（见
             * docs/wip-mmse-ring-notes.md），而这里每趟只有 64 个字要写。
             * 契约检查搭顺风车——这 4 个数本来就在寄存器里。 */
            int32_t ymax = 0;
            for (int k = 0; k < n; k++) {
                uint32_t ip = BIN0 + (uint32_t)dre[base + k];
                int32_t y1r = sp[0][2 * ip], y1i = sp[0][2 * ip + 1];
                int32_t y2r = sp[1][2 * ip], y2i = sp[1][2 * ip + 1];
                int32_t m = y1r < 0 ? -y1r : y1r, x;
                x = y1i < 0 ? -y1i : y1i; if (x > m) m = x;
                x = y2r < 0 ? -y2r : y2r; if (x > m) m = x;
                x = y2i < 0 ? -y2i : y2i; if (x > m) m = x;
                if (m > ymax) ymax = m;
                if (m > MS_LEAF_MAX_ABS) { bad = 1; break; }

                mmio_w(CGRA_BASE + GPCP_SPAD_FLAT_ADDR(
                           MMSE_FULL_SPAD_CORR_LEAVES_Y1R_BASE + 2u * (uint32_t)k), (uint32_t)y1r);
                mmio_w(CGRA_BASE + GPCP_SPAD_FLAT_ADDR(
                           MMSE_FULL_SPAD_CORR_LEAVES_Y1I_BASE + 2u * (uint32_t)k), (uint32_t)y1i);
                mmio_w(CGRA_BASE + GPCP_SPAD_FLAT_ADDR(
                           MMSE_FULL_SPAD_CORR_LEAVES_Y2R_BASE + 2u * (uint32_t)k), (uint32_t)y2r);
                mmio_w(CGRA_BASE + GPCP_SPAD_FLAT_ADDR(
                           MMSE_FULL_SPAD_CORR_LEAVES_Y2I_BASE + 2u * (uint32_t)k), (uint32_t)y2i);
            }
            if (bad) { cg_ok = 0; break; }
            /* |b_arr| <= 4*max|h|*max|y|，且 |g| <= 32767（ms_h_bcast 已查）。
               保证 16 位 mul 输入不截断、乘积与两项之差都在 int32 内。 */
            if (4 * ms_hmax * (int64_t)ymax > 32767) { cg_ok = 0; break; }
            __asm__ __volatile__("fence iorw, iorw" ::: "memory");

            mmse_array_start(pg);
            mmse_array_wait();

            for (int k = 0; k < n; k++)
                ms_bits[base + k] = mmio_r(CGRA_BASE + GPCP_SPAD_FLAT_ADDR(
                    MMSE_FULL_SPAD_PACK_SINKS_BITS_BASE + off + (uint32_t)k));
        }
    }

    /* 阵列成功时 ms_bits 已由 pack 段打包读回；只有回退或 CPU 对照路径
       才需要在 CVA6 上把 180 个 RE 重算一遍。 */
    if (!cg_ok) {
      for (int i = 0; i < NDATA; i++) {
          int r = dre[i];
          uint32_t ip = BIN0 + (uint32_t)r;
          int64_t y0r = (int64_t)sp[0][2 * ip] << FRONT_GAIN;
          int64_t y0i = (int64_t)sp[0][2 * ip + 1] << FRONT_GAIN;
          int64_t y1r = (int64_t)sp[1][2 * ip] << FRONT_GAIN;
          int64_t y1i = (int64_t)sp[1][2 * ip + 1] << FRONT_GAIN;

          int64_t b0r = cm_re(ms_hre[0][0], -ms_him[0][0], y0r, y0i)
                      + cm_re(ms_hre[1][0], -ms_him[1][0], y1r, y1i);
          int64_t b0i = cm_im(ms_hre[0][0], -ms_him[0][0], y0r, y0i)
                      + cm_im(ms_hre[1][0], -ms_him[1][0], y1r, y1i);
          int64_t b1r = cm_re(ms_hre[0][1], -ms_him[0][1], y0r, y0i)
                      + cm_re(ms_hre[1][1], -ms_him[1][1], y1r, y1i);
          int64_t b1i = cm_im(ms_hre[0][1], -ms_him[0][1], y0r, y0i)
                      + cm_im(ms_hre[1][1], -ms_him[1][1], y1r, y1i);

          /* num0 = b0*g11 - b1*g01 ; num1 = b1*g00 - b0*g10 */
          int64_t g10r = ms_g01r, g10i = -ms_g01i;
          int64_t n0r = b0r * ms_g11r - cm_re(b1r, b1i, ms_g01r, ms_g01i);
          int64_t n0i = b0i * ms_g11r - cm_im(b1r, b1i, ms_g01r, ms_g01i);
          int64_t n1r = b1r * ms_g00r - cm_re(b0r, b0i, g10r, g10i);
          int64_t n1i = b1i * ms_g00r - cm_im(b0r, b0i, g10r, g10i);

          /* 归一化整段省掉：判决只看符号，而符号不会被这一步改变。
           *
           *   ms_deti = 0 - Im(g01 * conj(g01)) = 0      （整数里恰好为 0）
           *   det     = g00*g11 - |g01|^2 > 0            （Cauchy-Schwarz + lambda*I）
           *   s = num * conj(det) / |det|^2 = num / det
           *
           * 乘一个正实数、再除一个正实数，都不会把正变负、把负变正。所以
           * sign(s) == sign(num)，这一步的 8 次乘和 4 次 64 位除法对硬判零贡献。
           * 三档实测与原式逐位一致：25/15/10 dB = 720/711/618 bit。 */
          int64_t s0r = n0r, s0i = n0i, s1r = n1r, s1i = n1i;

          uint32_t bits = 0;
          bits |= (s0r < 0) ? 1u : 0u;
          bits |= (s0i < 0) ? 2u : 0u;
          bits |= (s1r < 0) ? 4u : 0u;
          bits |= (s1i < 0) ? 8u : 0u;
          ms_bits[i] = bits;
      }
    }
    return 0;
}
