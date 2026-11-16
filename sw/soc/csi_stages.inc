/* -----------------------------------------------------------------------------
 * CSI-RS（38.211 7.4.1.5）+ 宽带 CQI 的「阶段实现」——被两个程序共用：
 *   sw/soc/csi_bm.c          人写参照基准（run-csi）
 *   sw/soc/proto_runner.c    协议运行器（docs/20 阶段 2 迁入）
 *
 * 阶段与 docs/20 §2 的动词对应：received -> dmrs -> ofdm_demodulate -> measure。
 * 这个是纯接收场景：激励就是一段时域样本（build/soc/csi.td），没有发射链。
 *
 * 使用前必须 include：gpcp_cgra.h / soc_io.h / gpcp_cgra_rt.h / csi_cqi_tab.h。
 * -------------------------------------------------------------------------- */

#define NPES          16
#define RING_DEPTH    1024
#include "../include/gpcp_ring.h"   /* 描述符环基址跟着 .bss 走（不再硬编码） */
#define DESC_BASE     GPCP_DESC_BASE

#define NFFT          4096
#define CP_LEN        288
#define BIN0          (NFFT / 2 + 1)
#define SYM_WORDS     (2 * (CP_LEN + NFFT))
#define NRE           240
#define DENSITY       3
#define NCSI          (NRE / DENSITY)
#define QPSK          2000
#define CSI_NID       1
#define CSI_NSLOT     0
#define CSI_L         0
#define N_SYMB_SLOT   14
#define GOLD_NC       1600
#define GOLD_LEN      (2 * NCSI + GOLD_NC + 31)
#define FRONT_GAIN    4          /* sp << 4, same as the other CSIs */

#define TD_RAM        0x80400000ul
#define PARAM_BASE    0x80600000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)
#define CGRA_BASE     0x50000000ul

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, dmas, busy;
static uint32_t (*desc)[8] = (uint32_t (*)[8])_ring_base;

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

/* 激励是裸的时域样本（没有 magic 头），所以没有参数要解析。 */
static int cs_setup(const uint32_t *par)
{
    (void)par;
    errs = runs = dmas = busy = 0;
    return 0;
}

static uint32_t gold_x1[GOLD_LEN], gold_x2[GOLD_LEN];

/* ------------------------------------------------------------ 38.211 Gold */
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

static uint32_t csi_cinit(void)
{
    uint64_t v = ((uint64_t)1 << 10)
                 * (uint64_t)(N_SYMB_SLOT * CSI_NSLOT + CSI_L + 1)
                 * (uint64_t)(2 * CSI_NID + 1) + (uint64_t)CSI_NID;
    return (uint32_t)(v & 0x7FFFFFFFu);
}

#include "ssb_fft.inc"          /* fft4096() + rev4() */
#ifdef PROTO_CPU_ONLY
#include "fft_cpu.inc"
#endif

/* --------------------------------------------------------- 阶段状态（结果） */
static uint32_t cs_cqi, cs_snr_q8, cs_ps40, cs_pn10, cs_cinit_v;
static int64_t cs_a_re[4], cs_a_im[4];

/* --------------------------------- 阶段 0：接收波形（激励已由宿主放进 RAM） */
static int cs_received(void) { return 0; }

/* --------------------------------------- 阶段 1：参考信号（CSI-RS 序列种子） */
static int cs_dmrs(void)
{
    gold_init(csi_cinit());
    return 0;
}

/* ------------------------------- 阶段 2：OFDM 解调 [array]（单个符号 FFT） */
static int cs_ofdm_demodulate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
#ifdef PROTO_CPU_ONLY
    fft4096_cpu(td + 2 * CP_LEN);
#else
    fft4096(td + 2 * CP_LEN);       /* X[k] at sp[rev4(k)] */
#endif
    return 0;
}

/* ---------------- 阶段 3：测量（80 个 CSI-RS RE 的 LS + 差值噪声 + CQI） */
static int cs_measure(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    int32_t *sp = td + 2 * CP_LEN;
    int64_t ar[NCSI], ai[NCSI];
    int64_t sumr = 0, sumi = 0, acc = 0, mr, mi, ps, pn, ratio, frac;

    /* ---- LS estimate on the 80 CSI-RS REs (density 3) ---- */
    for (int i = 0; i < NCSI; i++) {
        uint32_t idx = rev4(BIN0 + (uint32_t)(i * DENSITY));
        int64_t xr = (int64_t)sp[2 * idx] << FRONT_GAIN;
        int64_t xi = (int64_t)sp[2 * idx + 1] << FRONT_GAIN;
        int32_t c0 = (int32_t)gold_bit((uint32_t)(2 * i));
        int32_t c1 = (int32_t)gold_bit((uint32_t)(2 * i + 1));
        int64_t dr = (1 - 2 * c0) * QPSK;
        int64_t di = (1 - 2 * c1) * QPSK;
        /* Keep the raw scale: dividing by |r|^2 here would quantise the
           noise difference to 0/1 and destroy the estimate. */
        ar[i] = xr * dr + xi * di;             /* X * conj(r) */
        ai[i] = xi * dr - xr * di;
    }
    for (int i = 0; i < 4; i++) { cs_a_re[i] = ar[i]; cs_a_im[i] = ai[i]; }

    for (int i = 0; i < NCSI; i++) { sumr += ar[i]; sumi += ai[i]; }
    mr = sumr / NCSI;
    mi = sumi / NCSI;
    ps = mr * mr + mi * mi;

    for (int i = 1; i < NCSI; i++) {
        int64_t dr = ar[i] - ar[i - 1];
        int64_t di = ai[i] - ai[i - 1];
        acc += dr * dr + di * di;
    }
    pn = acc / (2 * (NCSI - 1));
    if (pn <= 0) pn = 1;
    /* Q8 ratio without overflowing: ps ~ 1e16, ps<<8 does not fit int64. */
    ratio = ps / pn;
    frac = (ps % pn) * 256 / pn;
    cs_snr_q8 = (uint32_t)(int32_t)(ratio * 256 + frac);

    cs_cqi = 0;
    for (int c = 1; c <= 15; c++)
        if ((int32_t)cs_snr_q8 >= csi_cqi_thr_q8[c])
            cs_cqi = (uint32_t)c;

    cs_ps40 = (uint32_t)(ps >> 40);
    cs_pn10 = (uint32_t)(pn >> 10);
    cs_cinit_v = csi_cinit();
    return 0;
}
