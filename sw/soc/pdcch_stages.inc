/* -----------------------------------------------------------------------------
 * PDCCH 收发自环的「阶段实现」——被两个程序共用（同一算法只留一份）：
 *
 *   sw/soc/pdcch_loop_bm.c   人写参照基准（run-pdcch-loop），按老顺序直接调用；
 *   sw/soc/proto_runner.c    协议运行器（docs/20 §6），按 plan.h 的阶段表调用。
 *
 * 这样"等价门"比的是同一份代码，而不是把 C 复制一份糊过去。
 *
 * 阶段划分与 docs/20 §2 的动词一一对应（顺序也一致）：
 *   transport_block -> encode -> modulate -> dmrs -> map_to_grid
 *   -> ofdm_modulate -> channel -> estimate -> equalise_demap -> decode
 *
 * 标 [array] 的阶段会真的启动阵列；其余是标量阶段。本文件不含 main()，
 * 也不解析激励——调用者先填 ps_* 参数（ps_setup），再按阶段顺序调用。
 *
 * 使用本文件的 .c 必须先 include：gpcp_cgra.h / soc_io.h / gpcp_cgra_rt.h /
 * 各 *_prog.h（微码）/ pdcch_luts.h（E=432 逆速率匹配），再 include 本文件。
 * -------------------------------------------------------------------------- */

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
/* 结果区主动上移到 0x8007_0000：本程序同时含 FFT 两张 ~50 KB 的生成镜像和
 * polar SC 节点微码，镜像总长 67 KB，.bss 尾部到 0x8001_0740，放在 0x8001_0000
 * 会被 .bss 压住（与 run-pusch / run-pdsch-full 同一处理，AGENTS 4.13）。 */
#define RESULT_BASE   ((uint32_t *)0x80070000ul)
#define DESC_BASE     0x80020000ul
#define RING_DEPTH    1024

#define PARAM_BASE    0x80600000ul        /* nr_pdcch_tx.dump_td */
#define FREQ_BASE     0x80300000ul        /* IFFT 工作缓冲：沿用 run-ifft 的 work 地址 */
#define TD_RAM        0x80400000ul        /* receiver's TD window */
#define WS_BASE       0x80700000ul        /* TX/RX bit vectors (keep .bss small) */

#define NFFT          4096
#define CP_LEN        288
#define TD_SCALE      256
#define BIN0          (NFFT / 2 + 1)
#define NRE           216
#define E_BITS        432
#define SYM_WORDS     (2 * (CP_LEN + NFFT))

#define A_DCI         32
#define CRC_LEN       24
#define K_INFO        (A_DCI + CRC_LEN)
#define NPOL          512
#define NW            (NPOL / 32)
#define DEPTH         9
#define N_FROZEN      (NPOL - K_INFO)
#define QAM           2000
#define NC            1600u
#define NPES          16
#define MAX_SYM       (E_BITS / 2)
#define TRI_T         29
#define MAGIC         0x1DCC0001ul

/* receiver-side (same layout run-pdcch uses) */
#define GOLD_BASE     0x80780000ul
#define LC_IN_BASE    0x80310000ul
#define LB_BASE       0x80320000ul
#define NODE_OUT_BASE 0x80340000ul
#define NODE_OUT_BLOCKS 2048
#define NODE_OUT_STRIDE 32
#define GOLD_NC       1600
#define GOLD_LEN      (E_BITS + GOLD_NC + 31)
#define NCAND         5
#define NODE_CYCLES   POLAR_NODE_PROG_CYCLES

#define MASK_WORD     (GPCP_LAYOUT_REGION_CONST * GPCP_LAYOUT_WORDS)
#define WMASK_WORD    (GPCP_LAYOUT_REGION_DATA * GPCP_LAYOUT_WORDS)

/* ---- workspace: everything that used to be .bss, moved out of the image ----
 * The FFT images are ~50 KB of .rodata, and a receiver-shaped program already
 * ends at 0x8000_ff3c, i.e. 196 bytes below the result area.  Anything larger
 * than a handful of scalars has to live in high RAM (AGENTS 4.13). */
#define WS(o)   ((uint8_t *)(WS_BASE + (o)))
static uint8_t *dci_bits = WS(0x0000);     /* A_DCI */
static uint8_t *info     = WS(0x0100);     /* K_INFO */
static uint8_t *u_bits   = WS(0x0200);     /* NPOL */
static uint8_t *x_bits   = WS(0x0400);     /* NPOL */
static uint8_t *y_bits   = WS(0x0600);     /* NPOL */
static uint8_t *z_bits   = WS(0x0800);     /* E_BITS */
static uint8_t *e_bits   = WS(0x0A00);     /* E_BITS */
static uint8_t *e_sc     = WS(0x0C00);     /* E_BITS */
static uint8_t *tri_m    = WS(0x1000);     /* TRI_T * TRI_T */

static int32_t *llr_e = (int32_t *)WS(0x2000);       /* E_BITS */
static int32_t *pre   = (int32_t *)WS(0x2800);       /* NPOL */
static int32_t *sym   = (int32_t *)WS(0x3000);       /* 2*MAX_SYM (I,Q) */

static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;
static uint32_t *gold_x1 = (uint32_t *)GOLD_BASE;
static uint32_t *gold_x2 = (uint32_t *)(GOLD_BASE + 4 * GOLD_LEN);
static int32_t *ain = (int32_t *)LC_IN_BASE;
static int32_t *bin_ = (int32_t *)(LC_IN_BASE + 64);
static int32_t *bet = (int32_t *)(LC_IN_BASE + 128);
static int32_t (*lbuf)[NPOL] = (int32_t (*)[NPOL])LB_BASE;
static uint8_t (*bbuf)[NPOL] =
    (uint8_t (*)[NPOL])(LB_BASE + 4 * (DEPTH + 1) * NPOL);
static int32_t *noblk = (int32_t *)NODE_OUT_BASE;

static const uint8_t SUBBLK_PERM[32] = {
    0, 1, 2, 4, 3, 5, 6, 7, 8, 9, 10, 12, 11, 13, 14, 15,
    16, 17, 18, 20, 19, 21, 22, 23, 24, 25, 26, 28, 27, 29, 30, 31
};
static const uint32_t candidates[NCAND] = {
    0x1111u, 0x5678u, 0x1234u, 0x9ABCu, 0xDEF0u
};

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }
static uint32_t errs, runs, n_desc, done, nblk;
/* 阵列忙拍数与 DMA 条目数是协议运行器的口径；手写参照不开（保持周期逐值不变）。 */
static uint32_t busy;
static uint32_t dma_n;

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

#include "ssb_fft.inc"          /* fft4096(), rev4(), generated stage images */
#ifdef PROTO_CPU_ONLY
#include "fft_cpu.inc"
#endif

/* ---------------------------------------------------------------- TX helpers */
static uint32_t pack32(const uint8_t *b)
{
    uint32_t w = 0, i;
    for (i = 0; i < 32u; i++)
        if (b[i])
            w |= 1u << i;
    return w;
}

static uint32_t bitrev9(uint32_t v)
{
    uint32_t r = 0, b;
    for (b = 0; b < DEPTH; b++)
        r |= ((v >> b) & 1u) << (DEPTH - 1 - b);
    return r;
}

static uint32_t is_frozen(uint32_t i) { return bitrev9(i) < N_FROZEN; }

/* 38.212 7.2 gCRC24C, MSB-first. */
static uint32_t crc24c_bits(const uint8_t *bits, uint32_t n, uint8_t *out)
{
    uint32_t reg = 0, i, j;
    for (i = 0; i < n; i++) {
        reg ^= (uint32_t)(bits[i] & 1u) << 23;
        for (j = 0; j < 24u; j++)
            reg = (reg & 0x800000u) ? (((reg << 1) ^ 0xB2B117u) & 0xFFFFFFu)
                                    : ((reg << 1) & 0xFFFFFFu);
    }
    if (out)
        for (i = 0; i < 24u; i++)
            out[i] = (uint8_t)((reg >> (23u - i)) & 1u);
    return reg;
}

static void tx_load_run(const unsigned int *prog, uint32_t cycles)
{
    if (gpcp_cgra_imem_load(CGRA_BASE, prog, cycles * NPES) != 0)
        errs++;
    if (rt_array_run(CGRA_BASE, 0, cycles) != 0)
        errs++;
    runs++;
#ifdef PS_TRACK_STATS
    busy += cycles;
#endif
}

static uint32_t polar_mask(uint32_t s)
{
    uint32_t m = 0, p;
    for (p = 0; p < 32u; p++)
        if ((p & s) == 0)
            m |= 1u << p;
    return m;
}

/* A1's nine stages, in place on spad words 0..15 (unchanged from pdcch_tx). */
static void polar_transform(void)
{
    uint32_t i, w;
#ifdef PROTO_CPU_ONLY
    /* CPU 对照路径：与 9 级阵列镜像同一多项式（stage 1..512 的标准极化变换） */
    for (uint32_t step = 1; step < NPOL; step <<= 1)
        for (uint32_t base = 0; base < NPOL; base += 2u * step)
            for (uint32_t k = 0; k < step; k++)
                u_bits[base + k] ^= u_bits[base + k + step];
    for (uint32_t i = 0; i < NPOL; i++)
        x_bits[i] = u_bits[i];
    return;
#endif

    for (i = 0; i < NW; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(i), pack32(u_bits + 32 * i));

    for (i = 0; i < 5; i++) {
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(MASK_WORD),
                        polar_mask(1u << i));
        switch (i) {
        case 0: tx_load_run(polar_inword_s0_prog, POLAR_INWORD_S0_PROG_CYCLES); break;
        case 1: tx_load_run(polar_inword_s1_prog, POLAR_INWORD_S1_PROG_CYCLES); break;
        case 2: tx_load_run(polar_inword_s2_prog, POLAR_INWORD_S2_PROG_CYCLES); break;
        case 3: tx_load_run(polar_inword_s3_prog, POLAR_INWORD_S3_PROG_CYCLES); break;
        default: tx_load_run(polar_inword_s4_prog, POLAR_INWORD_S4_PROG_CYCLES); break;
        }
    }
    for (i = 0; i < 4; i++) {
        uint32_t d = 1u << i;
        for (w = 0; w < NW; w++)
            gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(WMASK_WORD + w),
                            (w & d) ? 0u : 0xFFFFFFFFu);
        switch (i) {
        case 0: tx_load_run(polar_word_d1_prog, POLAR_WORD_D1_PROG_CYCLES); break;
        case 1: tx_load_run(polar_word_d2_prog, POLAR_WORD_D2_PROG_CYCLES); break;
        case 2: tx_load_run(polar_word_d4_prog, POLAR_WORD_D4_PROG_CYCLES); break;
        default: tx_load_run(polar_word_d8_prog, POLAR_WORD_D8_PROG_CYCLES); break;
        }
    }

    for (i = 0; i < NW; i++) {
        uint32_t v = gpcp_cgra_read(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(i));
        uint32_t b;
        for (b = 0; b < 32u; b++)
            x_bits[32 * i + b] = (uint8_t)((v >> b) & 1u);
    }
}

/* 38.212 5.4.1.1 sub-block interleaving (32 columns). */
static void subblock_interleave(uint32_t n, const uint8_t *d, uint8_t *out)
{
    uint32_t rows = n / 32u, p, r, k = 0;
    for (p = 0; p < 32u; p++)
        for (r = 0; r < rows; r++)
            out[k++] = d[r * 32u + SUBBLK_PERM[p]];
}

/* 38.212 5.4.1.3 triangular interleaver (lower triangle, truncated to E). */
static void triangular_interleave(uint32_t n, const uint8_t *in, uint8_t *out)
{
    uint32_t i, j, k = 0;
    for (i = 0; i < TRI_T && k < n; i++)
        for (j = 0; j <= i && k < n; j++)
            tri_m[i * TRI_T + j] = in[k++];
    k = 0;
    for (j = 0; j < TRI_T; j++)
        for (i = j; i < TRI_T; i++)
            if (i * (i + 1u) / 2u + j < n)
                out[k++] = tri_m[i * TRI_T + j];
}

static int32_t qpsk(int32_t b) { return (1 - 2 * b) * QAM; }

/* CPU 写完一大片 RAM 后要让引擎去读：fence + 读回最后一个字，排空 WT 写缓冲
 * （AGENTS 4.3 的老坑；这里用一个 volatile 读，避免被优化掉）。 */
static void drain_stores(const int32_t *p, uint32_t last)
{
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)*(volatile const int32_t *)&p[last];
}

/* ------------------------------------------------------------- RX helpers */
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
        if (++spins > 4000000u) { errs++; break; }
    }
    runs++;
#ifdef PS_TRACK_STATS
    busy += cycles;
#endif
}

static void dsc(uint32_t ram, uint32_t spad, uint32_t len, uint32_t page,
                uint32_t out, uint32_t stride, uint32_t row)
{
    uint32_t *d = desc[n_desc];
    d[0] = ram;
    d[1] = (spad & 0xFFFFu) | (page ? GPCP_DMA_DESC_PAGE1 : 0u) |
           (out ? GPCP_DMA_DESC_OUT : 0u);
    d[2] = len & 0xFFFFu;
    d[3] = (stride & 0xFFFFu) | ((row & 0xFFFFu) << 16);
    d[4] = 0; d[5] = 0; d[6] = 0; d[7] = 0;
    n_desc++;
#ifdef PS_TRACK_STATS
    dma_n++;
#endif
}

static void node_run(uint32_t d, uint32_t o, uint32_t half,
                     const uint8_t *bp, uint32_t cnt)
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
            bin_[k] = lbuf[d][o + half + chunk + k];
            bet[k] = bp ? bp[chunk + k] : 0;
        }
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        (void)ain[n - 1];

        int32_t *fb = noblk + (nblk % NODE_OUT_BLOCKS) * NODE_OUT_STRIDE;
        int32_t *gb = fb + 16;
        nblk++;
        if (nblk > NODE_OUT_BLOCKS) { errs++; nblk = 0; }

        n_desc = 0;
        dsc((uint32_t)(uintptr_t)ain, 0, n, 0, 0, 0, 0);
        dsc((uint32_t)(uintptr_t)bin_, 32, n, 0, 0, 0, 0);
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
        dsc((uint32_t)(uintptr_t)fb, (GPCP_LAYOUT_REGION_OUT * GPCP_LAYOUT_WORDS),
            n, 0, 1, 0, 0);
        dsc((uint32_t)(uintptr_t)gb, ((GPCP_LAYOUT_REGION_OUT + 1) * GPCP_LAYOUT_WORDS),
            n, 0, 1, 0, 0);
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

static uint32_t bitrev(uint32_t v, uint32_t bits)
{
    uint32_t r = 0;
    for (uint32_t b = 0; b < bits; b++)
        r |= ((v >> b) & 1u) << (bits - 1 - b);
    return r;
}

static int is_frozen_rx(uint32_t i)
{
    return bitrev(i % NPOL, DEPTH) < (uint32_t)(NPOL - K_INFO);
}

static void sc(uint32_t d, uint32_t o, uint32_t size)
{
    if (size == 1) {
        bbuf[d][o] = (uint8_t)(is_frozen_rx(o) ? 0 : (lbuf[d][o] < 0));
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

/* 38.211 5.2.1 Gold sequence, kept in RAM so it costs no .bss. */
static void gold_init(uint32_t c_init)
{
    uint32_t n = E_BITS + GOLD_NC;
    for (uint32_t i = 0; i < n + 31u; i++) { gold_x1[i] = 0; gold_x2[i] = 0; }
    gold_x1[0] = 1;
    for (uint32_t i = 0; i < 31u; i++) gold_x2[i] = (c_init >> i) & 1u;
    for (uint32_t i = 0; i < n; i++) {
        gold_x1[i + 31] = gold_x1[i + 3] ^ gold_x1[i];
        gold_x2[i + 31] = gold_x2[i + 3] ^ gold_x2[i + 2] ^ gold_x2[i + 1] ^ gold_x2[i];
    }
}

static uint32_t gold_bit(uint32_t n) { return gold_x1[GOLD_NC + n] ^ gold_x2[GOLD_NC + n]; }

/* --------------------------------------------------------- 阶段状态与激励 */
static uint32_t ps_dci_word;              /* 激励里的 DCI（MSB-first 打包） */
static uint32_t ps_rnti;
static uint32_t ps_n_id = 200;
static uint32_t ps_n_reg = NRE;           /* 216 个 REG = 432 bit / 2 */
static uint32_t ps_L = 6;                 /* 一个 CCE 的 REG 数 */
static uint32_t ps_n_shift = 0;

static uint32_t ps_dci_rx_word;           /* 译码得到的 DCI */
static uint32_t ps_rnti_match;            /* 盲检命中的 RNTI（0 = 没命中） */
static uint32_t ps_crc_ok;
static uint32_t ps_llr_bad;               /* 与发射比特符号不符的 LLR 数 */
static uint32_t ps_llr_amin, ps_llr_amax;
static uint32_t ps_max_sym;
static uint8_t rx_info[K_INFO];
static uint8_t rx_crc[CRC_LEN];

/* 读激励（tools/nr_pdcch_tx.py dump-td 的格式）并复位统计量。
 * 返回 0 成功；非 0 表示激励不合法。 */
static int ps_setup(const uint32_t *par)
{
    if (par[0] != MAGIC || par[1] == 0 || par[4] != MAX_SYM)
        return 1;
    ps_n_id = par[2];
    ps_dci_word = par[3];
    ps_n_reg = par[4];
    ps_L = par[5];
    ps_n_shift = par[6];
    ps_rnti = par[8];
    errs = runs = n_desc = done = nblk = 0;
    busy = dma_n = 0;
    ps_dci_rx_word = ps_rnti_match = ps_crc_ok = 0;
    ps_llr_bad = ps_llr_amin = ps_llr_amax = ps_max_sym = 0;
    return 0;
}

/* ------------------------------------------------------------ 阶段 0：传输块 */
/* DCI 32 bit + CRC24C + RNTI 掩码，展开成 K_INFO 个信息位。 */
static int ps_transport_block(void)
{
    uint32_t i;
    for (i = 0; i < A_DCI; i++)
        dci_bits[i] = (uint8_t)((ps_dci_word >> (31u - i)) & 1u);
    {
        uint8_t crc[CRC_LEN];
        crc24c_bits(dci_bits, A_DCI, crc);
        for (i = 0; i < A_DCI; i++)
            info[i] = dci_bits[i];
        for (i = 0; i < CRC_LEN; i++)
            info[A_DCI + i] = (uint8_t)(crc[i] ^ ((ps_rnti >> i) & 1u));
    }
    return 0;
}

/* ------------------------------------------------- 阶段 1：polar 编码 [array] */
/* 冻结位插入 -> 9 级变换（阵列）-> 子块交织 -> 圆缓冲 E=432 -> 三角交织。 */
static int ps_encode(void)
{
    uint32_t i, k;
    for (i = 0; i < NPOL; i++)
        u_bits[i] = 0;
    {
        uint32_t n_info = 0;
        for (i = 0; i < NPOL; i++)
            if (!is_frozen(i) && n_info < K_INFO)
                u_bits[i] = info[n_info++];
        if (n_info != K_INFO)
            errs++;
    }
    polar_transform();
    subblock_interleave(NPOL, x_bits, y_bits);
    for (k = 0; k < E_BITS; k++)
        z_bits[k] = y_bits[k % NPOL];
    triangular_interleave(E_BITS, z_bits, e_bits);
    return 0;
}

/* --------------------------------------- 阶段 2：加扰 + QPSK + REG/CCE 交织 */
static int ps_modulate(void)
{
    uint32_t k, j;
    gold_init(ps_n_id);
    for (k = 0; k < E_BITS; k++)
        e_sc[k] = (uint8_t)(e_bits[k] ^ gold_bit(k));
    {
        uint32_t nb = ps_n_reg / ps_L, half = nb / 2u, c, r, fj;
        for (k = 0; k < MAX_SYM; k++) {
            sym[2 * k] = qpsk(e_sc[2 * k]);
            sym[2 * k + 1] = qpsk(e_sc[2 * k + 1]);
        }
        for (k = 0; k < nb; k++) {
            c = k / 2u;
            r = k % 2u;
            fj = (r * half + c + ps_n_shift) % nb;
            for (j = 0; j < ps_L; j++) {
                sym[2 * (fj * ps_L + j)] = qpsk(e_sc[2 * (k * ps_L + j)]);
                sym[2 * (fj * ps_L + j) + 1] = qpsk(e_sc[2 * (k * ps_L + j) + 1]);
            }
        }
    }
    {
        uint32_t smax = 0;
        for (k = 0; k < 2u * MAX_SYM; k++) {
            int32_t v = sym[k];
            uint32_t a = (uint32_t)(v < 0 ? -v : v);
            if (a > smax) smax = a;
        }
        ps_max_sym = smax;
    }
    return 0;
}

/* ------------------------------------ 阶段 3：解调参考信号（自环里不含 DMRS） */
/* PDCCH 自环用的是一条理想信道，没有导频；只保留阶段位置，便于与外部的
 * 动词序列一一对应。c_init（= n_ID）已由 ps_setup 从激励读入。 */
static int ps_dmrs(void) { return 0; }

/* ----------------------------------------- 阶段 4：资源映射（频域格点） */
/* DC 左移半个 FFT，216 个 QPSK RE 放在正频；共轭由虚部取负完成。 */
static int ps_map_to_grid(void)
{
    uint32_t i;
    int32_t *freq = (int32_t *)FREQ_BASE;
    for (i = 0; i < 2u * NFFT; i++)
        freq[i] = 0;
    for (i = 0; i < NRE; i++) {
        freq[2 * (BIN0 + i)] = sym[2 * i];
        freq[2 * (BIN0 + i) + 1] = -sym[2 * i + 1];
    }
    drain_stores(freq, 2u * (BIN0 + NRE) - 1u);
    return 0;
}

/* ------------------------------------ 阶段 5：OFDM 调制 [array]（IFFT + CP） */
static int ps_ofdm_modulate(void)
{
    uint32_t k;
    int32_t *freq = (int32_t *)FREQ_BASE;
    int32_t *td = (int32_t *)TD_RAM;
#ifdef PROTO_CPU_ONLY
    fft4096_cpu(freq);
#else
    fft4096(freq);
#endif
    rt_dcache_flush();                          /* the engine wrote it in place */
    for (k = 0; k < CP_LEN; k++) {
        uint32_t t = NFFT - CP_LEN + k, r = rev4(t);
        td[2 * k] = freq[2 * r] * TD_SCALE;
        td[2 * k + 1] = -freq[2 * r + 1] * TD_SCALE;
    }
    for (k = 0; k < NFFT; k++) {
        uint32_t r = rev4(k);
        td[2 * (CP_LEN + k)] = freq[2 * r] * TD_SCALE;
        td[2 * (CP_LEN + k) + 1] = -freq[2 * r + 1] * TD_SCALE;
    }
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    drain_stores(td, 2u * (CP_LEN + NFFT) - 1u);   /* engine must see the TD */
    return 0;
}

/* ------------------------------------------------ 阶段 6：信道（自环 = 理想） */
static int ps_channel(void) { return 0; }

/* ----------------------- 阶段 7：信道估计 [array]（接收 FFT + 216 RE 抽取） */
/* 场景里没有单独的 ofdm_demodulate 动词（docs/20 §2 有，但本场景未用），
 * 解调归到估计阶段的第一步：时域 -> 频域 -> 取 216 个 RE。<<4 是接收链
 * 既有的定点口径（与 run-pdcch 一致）。 */
/* 节点粒度：接收 FFT 是独立变换节点，不再藏在 estimate 里。 */
static int ps_transform(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    int32_t *spec = td + 2 * CP_LEN;
#ifdef PROTO_CPU_ONLY
    fft4096_cpu(spec);
#else
    fft4096(spec);
#endif
    return 0;
}

static int ps_estimate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    int32_t *spec = td + 2 * CP_LEN;
    uint32_t r, idx;

    rt_dcache_flush();                          /* CPU wrote this window, engine
                                                 * transformed it in place */
    for (r = 0; r < NRE; r++) {
        idx = rev4(BIN0 + r);
        llr_e[2 * r] = spec[2 * idx] << 4;
        llr_e[2 * r + 1] = spec[2 * idx + 1] << 4;
    }
    return 0;
}

/* --------------------------- 阶段 8：均衡 + 软解调（逆 REG + 解扰 + 符号锚点） */
static int ps_equalise(void)
{
    uint32_t k, j;
    /* 逆 REG/CCE 映射：发射侧按 38.211 7.3.2.3 把 bundle j 搬到 f(j)，
     * 接收侧必须搬回来才拿得到 E 序列（run-pdcch 的激励本来没交织，
     * 所以那条链里没有这一步）。 */
    {
        int32_t *tmp = (int32_t *)WS(0x4000);
        uint32_t nb = ps_n_reg / ps_L, half = nb / 2u, c, rr, fj;
        for (k = 0; k < nb; k++) {
            c = k / 2u;
            rr = k % 2u;
            fj = (rr * half + c + ps_n_shift) % nb;
            for (j = 0; j < ps_L; j++) {
                tmp[2 * (k * ps_L + j)] = llr_e[2 * (fj * ps_L + j)];
                tmp[2 * (k * ps_L + j) + 1] = llr_e[2 * (fj * ps_L + j) + 1];
            }
        }
        for (k = 0; k < E_BITS; k++)
            llr_e[k] = tmp[k];
    }
    return 0;
}

static int ps_demap(void)
{
    uint32_t k;
    ps_llr_bad = 0;
    ps_llr_amin = 0xFFFFFFFFu;
    ps_llr_amax = 0;
    for (k = 0; k < E_BITS; k++) {          /* sign anchor vs TX bits */
        uint32_t neg = (uint32_t)(llr_e[k] < 0);
        if (neg != (uint32_t)e_sc[k])
            ps_llr_bad++;
        {
            uint32_t a = (uint32_t)(llr_e[k] < 0 ? -llr_e[k] : llr_e[k]);
            if (a < ps_llr_amin) ps_llr_amin = a;
            if (a > ps_llr_amax) ps_llr_amax = a;
        }
    }

    gold_init(ps_n_id);                        /* same c_init as the TX */
    for (k = 0; k < E_BITS; k++)
        if (gold_bit(k))
            llr_e[k] = -llr_e[k];
    return 0;
}

/* 兼容入口：老的单片调用路径（bm 程序 / 逐阶段调试）仍可用。 */
static int ps_equalise_demap(void)
{
    ps_equalise();
    ps_demap();
    return 0;
}

/* ------------------- 阶段 9：polar 译码 [array]（逆速率匹配 + SC 树 + 盲检） */
static int ps_decode(void)
{
    uint32_t i, m;
    int j;
    uint32_t ninfo = 0, matched = 0, crc_ok = 0;

    for (i = 0; i < NPOL; i++)
        pre[i] = 0;
    for (i = 0; i < RM_LEN; i++)
        pre[i] += llr_e[tri_perm[i]];
    for (i = 0; i < NPOL; i++)
        lbuf[0][i] = pre[sb_perm[i]];

    build_node_uc();
    sc(0, 0, NPOL);

    for (i = 0; i < NPOL && ninfo < K_INFO; i++)
        if (!is_frozen_rx(i))
            rx_info[ninfo++] = bbuf[DEPTH][i];
    if (ninfo != K_INFO)
        errs++;
    crc24c_bits(rx_info, A_DCI, rx_crc);
    for (m = 0; m < NCAND; m++) {
        uint8_t got[CRC_LEN];
        int ok = 1;
        for (j = 0; j < 24; j++)
            got[j] = rx_info[A_DCI + j];
        for (j = 0; j < 16; j++)
            got[j] ^= (uint8_t)((candidates[m] >> j) & 1u);
        for (j = 0; j < 24; j++)
            if (got[j] != rx_crc[j]) { ok = 0; break; }
        if (ok && !matched) {
            matched = candidates[m];
            crc_ok = 1;
        }
    }
    ps_rnti_match = matched;
    ps_crc_ok = crc_ok;
    ps_dci_rx_word = 0;
    for (i = 0; i < A_DCI; i++)
        ps_dci_rx_word = (ps_dci_rx_word << 1) | rx_info[i];
    return 0;
}
