/* -----------------------------------------------------------------------------
 * SSB 接收链的「阶段实现」——被两个程序共用：
 *   sw/soc/bench/ssb_fid_bm.c       人写参照基准（run-ssb-fid，结果区 0x8001_0000）
 *   sw/soc/bench/proto_runner.c     协议运行器（docs/protocols/README.md 阶段 2 迁入，结果区 0x8007_0000）
 *
 * 阶段与 §2 的动词对应：
 *   received -> ofdm_demodulate -> correlate(PSS,3) -> correlate(SSS,336)
 *   -> dmrs -> estimate -> equalise_demap -> decode(Polar SC)
 *
 * 结果区地址不在这里定义：两个调用者各自定义 RESULT_BASE（手写基准在
 * 0x8001_0000，运行器统一 0x8007_0000）。
 *
 * 使用前必须 include：gpcp_cgra.h / soc_io.h / pss_corr_prog.h /
 * pss_corr_layout.h / polar_node_prog.h / ssb_fid_luts.h。
 * -------------------------------------------------------------------------- */

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul

#define NPES             16
#define CORR_CYCLES   PSS_CORR_PROG_CYCLES
#define RING_DEPTH       1024
#define NREP             127
#define NPSS_RE0         56
#define NSYM             4

#include "../../include/gpcp_ring.h"   /* 描述符环基址跟着 .bss 走（不再硬编码） */
#include "../../include/gpcp_desc64.h"  /* 字段契约生成物：setter 是唯一的位定义 */
#define DESC_BASE        GPCP_DESC_BASE
#define PSS_REF_BASE     0x80040000ul          /* 3 x 128 words (one per N_ID2) */
#define RX_BASE          0x80050000ul          /* 254 packed words     */
#define OUT_BASE         0x80060000ul          /* rotating result blocks */
#define OUT_BLOCKS       64
#define TD_RAM           0x80400000ul          /* samples (loaded)     */

/* SSS hypothesis search: all 336 reference blocks are generated up front (a
 * block written right in front of its DMA can still be stale in RAM), and each
 * candidate gets its own result block (the engine writes RAM without
 * invalidating the CPU D$).  Both live above the 512 KiB linker RAM, so they
 * never collide with code/stack. */
#define NHYP             336
#define REF_STRIDE       128
#define SSS_REF_BASE     0x80200000ul          /* 336 x 128 words = 172 KiB */
#define SSS_OUT_BASE     0x80240000ul          /* 336 x 32 words           */
#define TRUE_NID1        200
#define TRUE_NID2        1
#define CP_LEN           288
#define DC_RE            24
#define I_SSB            3
#define N_HF             0
#define SYM_WORDS        (2 * (CP_LEN + FFT_N))
#define NFFT             4096

/* 38.211 7.4.3.1 carrier mapping: SSB RE r sits at FFT bin NFFT/2 - DC_RE + r
 * (k_ssb = 0), so the DC of the carrier lands on RE 24 and stays nulled.
 * Same expression as tools/models/ssb_plan_p1.py's bin_of(). */
static uint32_t re_bin(uint32_t r)
{
    return (uint32_t)(NFFT / 2 - DC_RE) + r;
}

/* PBCH: DMRS channel estimate -> QPSK demod + descrambling -> polar SC */
#define NRE              240
#define NPILOT           60
#define QPSK             2000
#define KINFO            32
#define NPOL             512
#define SDEPTH           9
#define NODE_CYCLES   POLAR_NODE_PROG_CYCLES
#define LLR_SHIFT        2
#define H_SCALE          16          /* keep a fractional part in H_hat */

#define Y_BASE           0x80030000ul          /* 4 x 240 complex = 1920 words */
#define LC_IN_BASE       0x80032000ul          /* a/b/beta staging (3 x 16)    */
#define GOLD_BASE        0x80300000ul          /* Gold sequences (above linker RAM) */
#define LB_BASE          0x80320000ul          /* LLR / bit buffers            */
#define NODE_OUT_BASE    0x80330000ul          /* one fresh block per node run */
#define NODE_OUT_BLOCKS  2048
#define NODE_OUT_STRIDE  32                    /* words per block              */
#define GOLD_LEN         (RM_LEN + 1600 + 31)

static uint32_t (*desc)[8] = (uint32_t (*)[8])_ring_base;
static int32_t *pref = (int32_t *)PSS_REF_BASE;
static int32_t *rxp = (int32_t *)RX_BASE;
static int32_t *oblk = (int32_t *)OUT_BASE;
static int32_t *sref = (int32_t *)SSS_REF_BASE;
static int32_t *sout = (int32_t *)SSS_OUT_BASE;
static int32_t *yw = (int32_t *)Y_BASE;
static int32_t *ain = (int32_t *)LC_IN_BASE;
static int32_t *bin = (int32_t *)(LC_IN_BASE + 64);
static int32_t *bet = (int32_t *)(LC_IN_BASE + 128);
static uint32_t *gold_x1 = (uint32_t *)GOLD_BASE;
static uint32_t *gold_x2 = (uint32_t *)(GOLD_BASE + 4 * GOLD_LEN);
static int32_t (*lbuf)[NPOL] = (int32_t (*)[NPOL])LB_BASE;
static uint8_t (*bbuf)[NPOL] =
    (uint8_t (*)[NPOL])(LB_BASE + 4 * (SDEPTH + 1) * NPOL);
static int32_t *noblk = (int32_t *)NODE_OUT_BASE;
static uint32_t nblk;          /* node runs never reuse a block (CPU D$ stays cold) */

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, n_desc, out_idx, done, dmas, busy;
static uint32_t dbg_wait, dbg_corr_spin, dbg_array_spin, dbg_nblk;   /* errs breakdown */
static int32_t *last_fb;        /* result block of the most recent corr_run */
static uint32_t latch[4];       /* descriptor the engine latched for the refs copy */

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; dbg_wait++; break; }
    }
}

#include "ssb_fft.inc"          /* 4096-point FFT engine (verified in run-fft) */
#ifdef PROTO_CPU_ONLY
#include "fft_cpu.inc"
#endif

/* --------------------------------------------------------------- microcode */

static uint8_t mseq[2 * NREP];

static void build_mseq(void)
{
    for (int i = 0; i < 7; i++) mseq[i] = (uint8_t)(i == 6);
    for (int i = 0; i < 2 * NREP - 7; i++) mseq[i + 7] = (uint8_t)(mseq[i + 4] ^ mseq[i]);
}

static int pss_sign(int nid2, int n) { return mseq[(n + 43 * nid2) % NREP] ? -1 : 1; }

/* SSS 38.211 7.4.2.3: m0 = 15*floor(N_ID1/112) + 5*N_ID2, m1 = N_ID1 mod 112,
 * d(n) = [1-2x0((n+m0)%127)] * [1-2x1((n+m1)%127)] */
static uint8_t x0seq[2 * NREP], x1seq[2 * NREP];

static void build_sss_seq(void)
{
    for (int i = 0; i < 7; i++) {
        x0seq[i] = (uint8_t)(i == 6);
        x1seq[i] = (uint8_t)(i == 6);
    }
    for (int i = 0; i < 2 * NREP - 7; i++) {
        x0seq[i + 7] = (uint8_t)(x0seq[i + 4] ^ x0seq[i]);
        x1seq[i + 7] = (uint8_t)(x1seq[i + 1] ^ x1seq[i]);
    }
}

static int sss_sym(int nid1, int nid2, int n)
{
    int m0 = 15 * (nid1 / 112) + 5 * nid2;
    int m1 = nid1 % 112;
    int a = x0seq[(n + m0) % NREP] ? -1 : 1;
    int b = x1seq[(n + m1) % NREP] ? -1 : 1;
    return a * b;
}

static void build_corr_uc(void)
{
    for (uint32_t i = 0; i < CORR_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, pss_corr_prog[i]);
    gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(PSS_CORR_SPAD_LEAF_R7(GPCP_LAYOUT_NPES - 1u)), 0);   /* ref[127] 幻影 tap */
}

/* ---------------------------------------------------------------- ring glue */
static void dsc(uint32_t ram, uint32_t spad, uint32_t len, uint32_t page,
                uint32_t out, uint32_t stride, uint32_t row)
{
    /* 32B 描述符 = 64B 描述符的前 4 个字；字段用生成的 setter 组包，
     * 位定义只在 rtl/cgra/cgra_desc64.yml，这里不再手写掩码/移位。 */
    uint32_t b[4] = {0, 0, 0, 0};
    gpcp_d64_set_src(b, ram);
    gpcp_d64_set_spad(b, spad);
    gpcp_d64_set_page(b, page);
    gpcp_d64_set_dir(b, out);
    gpcp_d64_set_in_len(b, len);
    gpcp_d64_set_stride(b, stride);
    gpcp_d64_set_row(b, row);
    uint32_t *d = desc[n_desc];
    d[0] = b[0]; d[1] = b[1]; d[2] = b[2]; d[3] = b[3];
    d[4] = 0; d[5] = 0; d[6] = 0; d[7] = 0;
    n_desc++;
    dmas++;
}

/* One correlation on the array: stream the 127 reference words (2D walk, 16
 * words per bank), optionally reload the rx block into spad 0, run the 52-cycle
 * MAC microcode and stream the 16 per-PE partials out.  rx stays in the
 * scratchpad between candidates, so only the first one reloads it. */
static void corr_one(uint32_t ref_ram, int32_t *fb, int with_rx, int cap)
{
#ifdef PROTO_CPU_ONLY
    /* CPU 对照路径：与 pss_corr 微码同口径（sext16 乘、32-bit 回卷、16 PE×8 tap） */
    const int32_t *rxw = (const int32_t *)RX_BASE;
    const int32_t *rf = (const int32_t *)ref_ram;
    (void)with_rx; (void)cap;
    for (uint32_t p = 0; p < NPES; p++) {
        int32_t re = 0, im = 0;
        for (uint32_t k = 0; k < 8; k++) {
            uint32_t t = 8u * p + k;
            int32_t rr = (t < NREP) ? rf[t] : 0;
            int32_t vr = rxw[2u * t], vi = rxw[2u * t + 1u];
            vr = (int16_t)vr; vi = (int16_t)vi; rr = (int16_t)rr;
            re = (int32_t)((uint32_t)re + (uint32_t)(vr * rr));
            im = (int32_t)((uint32_t)im + (uint32_t)(vi * rr));
        }
        fb[2u * p] = re;
        fb[2u * p + 1u] = im;
    }
    return;
#endif
    n_desc = 0;
    uint32_t n_in;
    if (with_rx) dsc(RX_BASE, 0, 2 * NREP, 0, 0, 0, 0);    /* rx: linear 254 words */
    dsc(ref_ram, (GPCP_LAYOUT_REGION_REF * GPCP_LAYOUT_WORDS), NREP, 0, 0, GPCP_LAYOUT_WORDS, 8);              /* refs: 8 words/bank */
    n_in = n_desc;

    done = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_BASE, DESC_BASE);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_SIZE, RING_DEPTH);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_HEAD, 0);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_TAIL, n_in);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_CTRL, GPCP_CGRA_RING_EN);
    done += n_in;
    wait_desc(done);
    if (cap) {                     /* what the engine decoded for the refs copy */
        latch[0] = mmio_r(CGRA_BASE + 0x0030u);
        latch[1] = mmio_r(CGRA_BASE + 0x0034u);
        latch[2] = mmio_r(CGRA_BASE + 0x0038u);
        latch[3] = mmio_r(CGRA_BASE + 0x003Cu);
    }
    gpcp_cgra_array_start(CGRA_BASE, 0, CORR_CYCLES);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    {
        uint32_t spins = 0;
        while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY) {
            if (++spins > 4000000u) { errs++; dbg_corr_spin++; break; }
        }
    }
    runs++;
    busy += CORR_CYCLES;
    dsc((uint32_t)(uintptr_t)fb, (GPCP_LAYOUT_REGION_LLR * GPCP_LAYOUT_WORDS), GPCP_LAYOUT_WORDS, 0, 1, 0, 0); /* results: 2 words/bank */
    gpcp_cgra_ring_push(CGRA_BASE, n_in + 1u);
    done += 1;
    wait_desc(done);
    mmio_w(CGRA_BASE + GPCP_CGRA_RING_CTRL, 0);
}

/* |re| + |im| of one result block (the CPU sum, as in sw/soc/bench/sss_bm.c) */
static uint64_t metric_of(int32_t *fb)
{
    int64_t sr = 0, si = 0;
    for (int p = 0; p < NPES; p++) { sr += fb[2 * p]; si += fb[2 * p + 1]; }
    return (uint64_t)((sr < 0 ? -sr : sr) + (si < 0 ? -si : si));
}

/* ---------------------------------------------------------- PBCH sequences */
/* Gold sequence of 38.211 5.2.1 (x1 with tap 3, x2 with taps 3/2/1), skipped
 * by Nc = 1600 like tools/models/ssb_plan.py's gold(). */
static void gold_init(uint32_t c_init)
{
    /* the PBCH data scrambling needs E = RM_LEN bits (720), longer than the
     * 512-bit polar block: fill the whole GOLD_LEN buffer */
    uint32_t n = RM_LEN + 1600;
    for (uint32_t i = 0; i < n + 31; i++) { gold_x1[i] = 0; gold_x2[i] = 0; }
    gold_x1[0] = 1;
    for (uint32_t i = 0; i < 31; i++) gold_x2[i] = (c_init >> i) & 1u;
    for (uint32_t i = 0; i < n; i++) {
        gold_x1[i + 31] = gold_x1[i + 3] ^ gold_x1[i];
        gold_x2[i + 31] = gold_x2[i + 3] ^ gold_x2[i + 2] ^ gold_x2[i + 1] ^ gold_x2[i];
    }
}

static uint32_t gold_bit(uint32_t n) { return gold_x1[1600 + n] ^ gold_x2[1600 + n]; }

static uint32_t bitrev(uint32_t v, uint32_t bits)
{
    uint32_t r = 0;
    for (uint32_t b = 0; b < bits; b++) r |= ((v >> b) & 1u) << (bits - 1 - b);
    return r;
}

static int is_frozen(uint32_t i)
{
    return bitrev(i % NPOL, SDEPTH) < (uint32_t)(NPOL - KINFO);
}

/* ------------------------------------------------------------ polar min-sum */
static void build_node_uc(void)
{
    for (uint32_t i = 0; i < NODE_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, polar_node_prog[i]);
}

static void array_run(uint32_t cycles)
{
    gpcp_cgra_array_start(CGRA_BASE, 0, cycles);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY) {
        if (++spins > 4000000u) { errs++; dbg_array_spin++; break; }
    }
    runs++;
    busy += cycles;
}

/* one f/g node over up to 16 LLR pairs: a, b and beta are staged in the
 * scratchpad, the 16 per-PE results are streamed back (16 words each) */
static void node_run(uint32_t d, uint32_t o, uint32_t half, const uint8_t *bp, uint32_t cnt)
{
#ifdef PROTO_CPU_ONLY
    for (uint32_t chunk = 0; chunk < cnt; chunk += NPES) {
        uint32_t n = cnt - chunk < NPES ? cnt - chunk : NPES;
        for (uint32_t k = 0; k < n; k++) {
            int32_t a = lbuf[d][o + chunk + k];
            int32_t b = lbuf[d][o + half + chunk + k];
            int32_t bt = bp ? bp[chunk + k] : 0;
            int32_t sa = a >> 31, sb = b >> 31;
            int32_t aa = (a ^ sa) - sa, ab = (b ^ sb) - sb;
            int32_t m = aa < ab ? aa : ab;
            int32_t s = sa ^ sb;
            lbuf[d + 1][o + chunk + k] = (m ^ s) - s;
            lbuf[d + 1][o + half + chunk + k] =
                a + b - ((a & (-bt)) << 1);
        }
    }
    return;
#endif
    for (uint32_t chunk = 0; chunk < cnt; chunk += NPES) {
        uint32_t n = cnt - chunk < NPES ? cnt - chunk : NPES;
        for (uint32_t k = 0; k < n; k++) {
            ain[k] = lbuf[d][o + chunk + k];
            bin[k] = lbuf[d][o + half + chunk + k];
            bet[k] = bp ? bp[chunk + k] : 0;
        }
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        (void)ain[n - 1];

        int32_t *fb = noblk + (nblk % NODE_OUT_BLOCKS) * NODE_OUT_STRIDE;
        int32_t *gb = fb + 16;
        nblk++;
        if (nblk > NODE_OUT_BLOCKS) { errs++; dbg_nblk++; nblk = 0; }

        n_desc = 0;
        dsc((uint32_t)(uintptr_t)ain, 0, n, 0, 0, 0, 0);
        dsc((uint32_t)(uintptr_t)bin, 32, n, 0, 0, 0, 0);
        dsc((uint32_t)(uintptr_t)bet, 64, n, 0, 0, 0, 0);
        uint32_t n_in = n_desc;

        done = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE);
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_BASE, DESC_BASE);
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_SIZE, RING_DEPTH);
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_HEAD, 0);
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_TAIL, n_in);
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_CTRL, GPCP_CGRA_RING_EN);
        done += n_in;
        wait_desc(done);
        array_run(NODE_CYCLES);
        dsc((uint32_t)(uintptr_t)fb, (GPCP_LAYOUT_REGION_OUT * GPCP_LAYOUT_WORDS), n, 0, 1, 0, 0);
        dsc((uint32_t)(uintptr_t)gb, ((GPCP_LAYOUT_REGION_OUT + 1) * GPCP_LAYOUT_WORDS), n, 0, 1, 0, 0);
        gpcp_cgra_ring_push(CGRA_BASE, n_in + 2u);
        done += 2;
        wait_desc(done);
        mmio_w(CGRA_BASE + GPCP_CGRA_RING_CTRL, 0);
        for (uint32_t k = 0; k < n; k++) {
            lbuf[d + 1][o + chunk + k] = fb[k];
            lbuf[d + 1][o + half + chunk + k] = gb[k];
        }
    }
}

static void sc(uint32_t d, uint32_t o, uint32_t size)
{
    if (size == 1) {
        bbuf[d][o] = (uint8_t)(is_frozen(o) ? 0 : (lbuf[d][o] < 0));
        return;
    }
    uint32_t half = size >> 1;
    node_run(d, o, half, 0, half);
    sc(d + 1, o, half);
    node_run(d, o, half, &bbuf[d + 1][o], half);
    sc(d + 1, o + half, half);
    for (uint32_t i = 0; i < half; i++) {
        bbuf[d][o + i] = bbuf[d + 1][o + i] ^ bbuf[d + 1][o + half + i];
        bbuf[d][o + half + i] = bbuf[d + 1][o + half + i];
    }
}

/* -------------------------------------------------------- PBCH RE mapping */
static int64_t div_round(int64_t v, int64_t d)
{
    return (v + (v < 0 ? -(d / 2) : (d / 2))) / d;
}

/* data RE scan: every RE of the PBCH symbols except the DMRS grid (r%4==0)
 * and the carrier DC null (r==DC_RE).  Real NR PBCH fills the whole symbol, so
 * the PSS/SSS positions (56..182) carry PBCH data here (unlike the demo). */
static void data_re_list(uint32_t s, int *out, int *cnt)
{
    (void)s;
    int n = 0;
    for (int r = 0; r < NRE; r++) {
        if ((r % 4) == 0) continue;                       /* DMRS grid */
        if (r == DC_RE) continue;                        /* carrier DC null */
        out[n++] = r;
    }
    *cnt = n;
}

static uint32_t nid_cell(int nid1, int nid2)
{
    return (uint32_t)(3 * nid1 + nid2);
}

static uint32_t pbch_dmrs_cinit(int nid1, int nid2, int i_ssb, int n_hf)
{
    uint32_t ncell = nid_cell(nid1, nid2);
    int ibar = i_ssb + 4 * n_hf;
    return ((1u << 11) * (uint32_t)(ibar + 1) * ((ncell / 4) + 1)
            + ((1u << 6) * (uint32_t)(ibar + 1))
            + (ncell % 4));
}

/* --------------------------------------------------------- 阶段状态（结果） */
static uint32_t ss_nid2, ss_nid1, ss_dmrs_ci, ss_sbest10, ss_strue10;
static int32_t ss_hre[NSYM], ss_him[NSYM];
static uint32_t ss_mib[KINFO];

/* 激励是四符号时域样本（没有 magic 头）。 */
static int ss_setup(const uint32_t *par)
{
    (void)par;
    errs = runs = n_desc = out_idx = done = dmas = busy = 0;
    /* 环窗口必须留在 0x80030000 起的固定缓冲区（Y_BASE 等）之前。 */
    if (DESC_BASE + RING_DEPTH * 32u > Y_BASE) errs++;
    dbg_wait = dbg_corr_spin = dbg_array_spin = dbg_nblk = 0;
    ss_nid2 = ss_nid1 = ss_dmrs_ci = 0;
    ss_sbest10 = ss_strue10 = 0;
    nblk = 0;
    return 0;
}

/* ------------------------------------------- 阶段 0：接收（四符号 TD 常驻） */
static int ss_received(void) { return 0; }

/* ---------------- 阶段 1：OFDM 解调 [array]（四个符号各一次 4096 点 FFT） */
static int ss_ofdm_demodulate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    for (int s = 0; s < NSYM; s++)
#ifdef PROTO_CPU_ONLY
        fft4096_cpu(td + (size_t)s * SYM_WORDS + 2 * CP_LEN);
#else
        fft4096(td + (size_t)s * SYM_WORDS + 2 * CP_LEN);
#endif
    build_corr_uc();          /* the FFT image owned the imem until here */
    return 0;
}

/* ---------------- 阶段 2：PSS 相关搜索 [array]（3 个 N_ID2 假设） */
static int ss_correlate_pss(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    int32_t *spec0 = td + 2 * CP_LEN;
    int64_t best = -1;
    uint32_t nid2 = 0;

    build_mseq();
    build_sss_seq();

    for (int t = 0; t < NREP; t++) {
        uint32_t idx = rev4(re_bin((uint32_t)(NPSS_RE0 + t)));
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 0] = spec0[2 * idx] << 4;
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 1] = spec0[2 * idx + 1] << 4;
    }
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)rxp[2 * NREP - 1];       /* drain the store buffer before the DMA */

    for (int c = 0; c < 3; c++)
        for (int t = 0; t < NREP; t++)
            pref[c * REF_STRIDE + t] = pss_sign(c, t) * 32767;
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)pref[2 * REF_STRIDE + NREP - 1];

    for (int c = 0; c < 3; c++) {
        int32_t *fb = oblk + out_idx * 32;
        out_idx = (out_idx + 1) % OUT_BLOCKS;
        last_fb = fb;
        corr_one(PSS_REF_BASE + 4u * (uint32_t)c * REF_STRIDE, fb, 1, c == 1);
        int64_t re = 0, im = 0;
        for (int p = 0; p < NPES; p++) { re += fb[2 * p]; im += fb[2 * p + 1]; }
        int64_t mag = (re < 0 ? -re : re) + (im < 0 ? -im : im);
        if (mag > best) { best = mag; nid2 = (uint32_t)c; }
    }
    if (nid2 != TRUE_NID2)
        errs++;
    ss_nid2 = nid2;
    return 0;
}

/* ---------------- 阶段 3：SSS 相关搜索 [array]（336 个 N_ID1 假设） */
static int ss_correlate_sss(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    int32_t *spec2 = td + 2 * (size_t)SYM_WORDS + 2 * CP_LEN;
    uint64_t sbest = 0, strue = 0;
    uint32_t nid1 = 0;

    for (int t = 0; t < NREP; t++) {
        uint32_t idx = rev4(re_bin((uint32_t)(NPSS_RE0 + t)));
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 0] = spec2[2 * idx] << 4;
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 1] = spec2[2 * idx + 1] << 4;
    }
    for (int h = 0; h < NHYP; h++)
        for (int t = 0; t < NREP; t++)
            sref[h * REF_STRIDE + t] = sss_sym(h, (int)ss_nid2, t) * 32767;
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)rxp[2 * NREP - 1];
    (void)sref[(NHYP - 1) * REF_STRIDE + NREP - 1];   /* drain both stores */

    for (int h = 0; h < NHYP; h++) {
        int32_t *fb = sout + h * 32;
        corr_one(SSS_REF_BASE + 4u * (uint32_t)h * REF_STRIDE, fb, h == 0, 0);
        uint64_t mag = metric_of(fb);
        if (h == (int)TRUE_NID1) strue = mag;
        if (mag > sbest) { sbest = mag; nid1 = (uint32_t)h; }
    }
    if (nid1 != TRUE_NID1)
        errs++;
    ss_nid1 = nid1;
    ss_sbest10 = (uint32_t)(sbest >> 10);
    ss_strue10 = (uint32_t)(strue >> 10);
    return 0;
}

/* ------------ 阶段 4：PBCH DMRS 种子（38.211 7.4.1.3，i_SSB/n_hf） */
static int ss_dmrs(void)
{
    ss_dmrs_ci = pbch_dmrs_cinit((int)ss_nid1, (int)ss_nid2, I_SSB, N_HF);
    gold_init(ss_dmrs_ci);
    return 0;
}

/* ---- 阶段 5：信道估计（网格抽取 + PBCH 符号 1/3 的 DMRS LS） */
static int ss_estimate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    int64_t den = (int64_t)NPILOT * 2 * QPSK * QPSK / H_SCALE;
    int pbch_sym[2] = {1, 3};
    int n = 0;

    for (int s = 0; s < NSYM; s++) {
        int32_t *sp = td + (size_t)s * SYM_WORDS + 2 * CP_LEN;
        for (int r = 0; r < NRE; r++) {
            if (r == DC_RE) continue;                 /* carrier DC null */
            uint32_t idx = rev4(re_bin((uint32_t)r));
            yw[(s * NRE + r) * 2 + 0] = sp[2 * idx] << 4;
            yw[(s * NRE + r) * 2 + 1] = sp[2 * idx + 1] << 4;
        }
    }
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)yw[2 * NRE - 1];

    for (int si = 0; si < 2; si++) {
        int s = pbch_sym[si];
        int64_t accr = 0, acci = 0;
        for (int r = 0; r < NRE; r += 4) {
            if (r == DC_RE) continue;   /* skip the nulled DC RE */
            int32_t dr = (int32_t)(1 - 2 * (int)gold_bit(2u * (uint32_t)n)) * QPSK;
            int32_t di = (int32_t)(1 - 2 * (int)gold_bit(2u * (uint32_t)n + 1u)) * QPSK;
            int32_t yr = yw[(s * NRE + r) * 2 + 0];
            int32_t yi = yw[(s * NRE + r) * 2 + 1];
            accr += (int64_t)yr * dr + (int64_t)yi * di;      /* y*d* */
            acci += (int64_t)yi * dr - (int64_t)yr * di;
            n++;
        }
        ss_hre[s] = (int32_t)div_round(accr, den);
        ss_him[s] = (int32_t)div_round(acci, den);
    }
    return 0;
}

/* ---- 阶段 6：QPSK 软解调 + PBCH 解扰 + 逆速率匹配（720 -> 512） */
static int32_t e720[RM_LEN];

static int ss_equalise(void)
{
    int pbch_sym[2] = {1, 3};
    int n = 0;

    gold_init(nid_cell((int)ss_nid1, (int)ss_nid2));   /* 38.211 7.3.2 */
    for (int i = 0; i < NPOL; i++) lbuf[0][i] = 0;
    for (int si = 0; si < 2; si++) {
        int s = pbch_sym[si];
        int res[NRE], cnt = 0;
        data_re_list((uint32_t)s, res, &cnt);
        for (int j = 0; j < cnt; j++) {
            int32_t yr = yw[(s * NRE + res[j]) * 2 + 0];
            int32_t yi = yw[(s * NRE + res[j]) * 2 + 1];
            int64_t eqr = (int64_t)yr * ss_hre[s] + (int64_t)yi * ss_him[s];
            int64_t eqi = (int64_t)yi * ss_hre[s] - (int64_t)yr * ss_him[s];
            int32_t v0 = (int32_t)(eqr >> LLR_SHIFT);
            int32_t v1 = (int32_t)(eqi >> LLR_SHIFT);
            if (v0 > 2000000) v0 = 2000000;
            if (v0 < -2000000) v0 = -2000000;
            if (v1 > 2000000) v1 = 2000000;
            if (v1 < -2000000) v1 = -2000000;
            e720[2 * n]     = gold_bit((uint32_t)(2 * n))     ? -v0 : v0;
            e720[2 * n + 1] = gold_bit((uint32_t)(2 * n + 1)) ? -v1 : v1;
            n++;
        }
    }
    return 0;
}

static int ss_demap(void)
{
    static int32_t zrm[RM_LEN], pre[POLAR_N];
    for (int m = 0; m < RM_LEN; m++) zrm[m] = e720[tri_perm[m]];
    for (int i = 0; i < POLAR_N; i++) pre[i] = 0;
    for (int k = 0; k < RM_LEN; k++) pre[k % POLAR_N] += zrm[k];
    for (int i = 0; i < POLAR_N; i++) lbuf[0][i] = pre[sb_perm[i]];
    return 0;
}

/* 兼容入口：老的单片调用路径（bm 程序 / 逐阶段调试）仍可用。 */
static int ss_equalise_demap(void)
{
    ss_equalise();
    ss_demap();
    return 0;
}

/* ---- 阶段 7：Polar SC 译码 [array]（N=512，K=32）+ MIB ---- */
static int ss_decode(void)
{
    uint32_t k = 0;
    build_node_uc();
    sc(0, 0, NPOL);
    for (uint32_t i = 0; i < NPOL && k < KINFO; i++)
        if (!is_frozen(i))
            ss_mib[k++] = bbuf[SDEPTH][i];
    return 0;
}
