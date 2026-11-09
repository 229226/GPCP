/* -----------------------------------------------------------------------------
 * G2 / B4: PDCCH self-loopback in ONE SoC program (no host round trip).
 *
 * Purpose: close the first half of docs/19 G2's acceptance criterion -- "the
 * DCI we transmit is blind-detected by our own receiver" -- on the same chip,
 * with no file written in between and no golden symbols loaded from the host.
 *
 *   params (0x8060_0000, nr_pdcch_tx.dump_td: magic, nvar, n_id, dci, n_reg,
 *           L, n_shift, -, rnti..)
 *     -> B3 transmit chain (polar transform on the array, 9 kernels)
 *     -> frequency grid: 216 QPSK REs at bins BIN0..BIN0+215, conjugate + int16
 *     -> 4096-point IFFT (fft4096, conjugate trick) + TD_SCALE + CP
 *     -> TD block at 0x8040_0000 (the layout run-pdcch expects)
 *     -> A4 receive chain: FFT -> 216 RE -> 432 LLR -> descramble
 *        -> inverse rate matching -> polar SC on the array -> RNTI blind detect
 *
 * The only thing the two halves share is the TD layout -- exactly the interface
 * that run-pdcch-tx / run-pdcch already validate separately.  One extra step on
 * the receive side: B3's transmitter applies the 38.211 7.3.2.3 REG/CCE
 * interleave, so the loopback receiver undoes it before descrambling (run-pdcch's
 * stimulus was never interleaved, which is why that chain has no such step).
 *
 * D$ coherence (AGENTS 4.2): the CPU writes the frequency grid and the TD, then
 * the engine transforms both in place, so *both* buffers need rt_dcache_flush()
 * before the CPU reads them back.  (run-pdcch does not flush because its TD was
 * written by the testbench, not by the CPU.)
 *
 * Two engine gotchas this benchmark found the hard way:
 *   1. 写完一大片 RAM 交给引擎读，要先 fence + 读回最后一个字（drain_stores）；
 *   2. 之前若用 gpcp_cgra_imem_load（DMA 写 IMEM），RTL 的 dma_tgt 曾是粘滞的，
 *      会把之后所有 DMA 静默改道去写 IMEM —— 已在 cgra_top.sv 的 D_DESC_SET 清零。
 *
 * Result area (0x8007_0000, 32-bit words; .bss 到 0x8001_0740，故主动上移):
 *   [0] 0xCAFEB4B1  [1] errors  [2] cycles  [3] array runs
 *   [4] dci_tx      [5] dci_rx  [6] rnti_tx [7] rnti_matched  [8] crc_ok
 *   [9] llr_bad (sign mismatches vs the transmitted bits)
 *   [10] llr_min |abs|  [11] llr_max |abs|  [12] n_id  [13] 0x600D
 *   [16..47]  transmitted DCI bits      [48..79]  decoded DCI bits
 *   [80..103] CRC24C bits (unmasked)    [108] max|symbol|
 *   [112..543] e_sc bits (432, the scrambled codeword the TX actually sent)
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"      /* rt_array_run / rt_dcache_flush */

#include "polar_inword_s0_prog.h"
#include "polar_inword_s1_prog.h"
#include "polar_inword_s2_prog.h"
#include "polar_inword_s3_prog.h"
#include "polar_inword_s4_prog.h"
#include "polar_word_d1_prog.h"
#include "polar_word_d2_prog.h"
#include "polar_word_d4_prog.h"
#include "polar_word_d8_prog.h"
#include "polar_node_prog.h"              /* polar SC node (receiver) */
#include "pdcch_luts.h"                   /* E=432 inverse rate matching */

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

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

#include "ssb_fft.inc"          /* fft4096(), rev4(), generated stage images */

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
}

static void node_run(uint32_t d, uint32_t o, uint32_t half,
                     const uint8_t *bp, uint32_t cnt)
{
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

int main(void)
{
    uint32_t *out = RESULT_BASE;
    const uint32_t *par = (const uint32_t *)PARAM_BASE;
    int32_t *freq = (int32_t *)FREQ_BASE;
    int32_t *td = (int32_t *)TD_RAM;
    uint32_t n_id, dci_word, n_reg, L, n_shift, rnti, t0, i, k;

    if (par[0] != MAGIC || par[1] == 0 || par[4] != MAX_SYM) {
        out[0] = 0x0BADC4B1u;
        out[13] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        mmio_w(EXIT_ADDR, 0x600Du);
        return 0;
    }
    n_id = par[2];
    dci_word = par[3];
    n_reg = par[4];
    L = par[5];
    n_shift = par[6];
    rnti = par[8];

    t0 = mtime();

    /* ================= 1. transmit chain (B3, array polar) ================= */
    for (i = 0; i < A_DCI; i++)
        dci_bits[i] = (uint8_t)((dci_word >> (31u - i)) & 1u);
    {
        uint8_t crc[CRC_LEN];
        crc24c_bits(dci_bits, A_DCI, crc);
        for (i = 0; i < A_DCI; i++)
            info[i] = dci_bits[i];
        for (i = 0; i < CRC_LEN; i++)
            info[A_DCI + i] = (uint8_t)(crc[i] ^ ((rnti >> i) & 1u));
    }
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
    gold_init(n_id);
    for (k = 0; k < E_BITS; k++)
        e_sc[k] = (uint8_t)(e_bits[k] ^ gold_bit(k));

    /* QPSK + REG/CCE interleaving, exactly as pdcch_tx_bm.c */
    {
        uint32_t nb = n_reg / L, half = nb / 2u, j, c, r, fj;
        for (k = 0; k < MAX_SYM; k++) {
            sym[2 * k] = qpsk(e_sc[2 * k]);
            sym[2 * k + 1] = qpsk(e_sc[2 * k + 1]);
        }
        for (k = 0; k < nb; k++) {
            c = k / 2u;
            r = k % 2u;
            fj = (r * half + c + n_shift) % nb;
            for (j = 0; j < L; j++) {
                sym[2 * (fj * L + j)] = qpsk(e_sc[2 * (k * L + j)]);
                sym[2 * (fj * L + j) + 1] = qpsk(e_sc[2 * (k * L + j) + 1]);
            }
        }
    }

    /* ================= 2. frequency grid + IFFT + CP ====================== */
    for (i = 0; i < 2u * NFFT; i++)
        freq[i] = 0;
    for (i = 0; i < NRE; i++) {                 /* conjugate: im negated */
        freq[2 * (BIN0 + i)] = sym[2 * i];
        freq[2 * (BIN0 + i) + 1] = -sym[2 * i + 1];
    }
    {
        uint32_t smax = 0;
        for (i = 0; i < 2u * MAX_SYM; i++) {
            int32_t v = sym[i];
            uint32_t a = (uint32_t)(v < 0 ? -v : v);
            if (a > smax) smax = a;
        }
        out[108] = smax;                        /* anchor: symbols are +-QAM */
    }
    drain_stores(freq, 2u * (BIN0 + NRE) - 1u);
    fft4096(freq);
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

    /* ================= 3. receive chain (A4, array polar SC) =============== */
    {
        int32_t *spec = td + 2 * CP_LEN;
        uint32_t r, idx, llr_bad = 0, amin = 0xFFFFFFFFu, amax = 0;

        fft4096(spec);
        rt_dcache_flush();                      /* CPU wrote this window, engine
                                                 * transformed it in place */
        for (r = 0; r < NRE; r++) {
            idx = rev4(BIN0 + r);
            llr_e[2 * r] = spec[2 * idx] << 4;
            llr_e[2 * r + 1] = spec[2 * idx + 1] << 4;
        }
        /* 逆 REG/CCE 映射：发射侧按 38.211 7.3.2.3 把 bundle j 搬到 f(j)，
         * 接收侧必须搬回来才拿得到 E 序列（run-pdcch 的激励本来没交织，
         * 所以那条链里没有这一步）。 */
        {
            int32_t *tmp = (int32_t *)WS(0x4000);
            uint32_t nb = n_reg / L, half = nb / 2u, j, c, rr, fj;
            for (k = 0; k < nb; k++) {
                c = k / 2u;
                rr = k % 2u;
                fj = (rr * half + c + n_shift) % nb;
                for (j = 0; j < L; j++) {
                    tmp[2 * (k * L + j)] = llr_e[2 * (fj * L + j)];
                    tmp[2 * (k * L + j) + 1] = llr_e[2 * (fj * L + j) + 1];
                }
            }
            for (k = 0; k < E_BITS; k++)
                llr_e[k] = tmp[k];
        }
        for (k = 0; k < E_BITS; k++) {          /* sign anchor vs TX bits */
            uint32_t neg = (uint32_t)(llr_e[k] < 0);
            if (neg != (uint32_t)e_sc[k])
                llr_bad++;
            {
                uint32_t a = (uint32_t)(llr_e[k] < 0 ? -llr_e[k] : llr_e[k]);
                if (a < amin) amin = a;
                if (a > amax) amax = a;
            }
        }

        gold_init(n_id);                        /* same c_init as the TX */
        for (k = 0; k < E_BITS; k++)
            if (gold_bit(k))
                llr_e[k] = -llr_e[k];

        for (i = 0; i < NPOL; i++)
            pre[i] = 0;
        for (i = 0; i < RM_LEN; i++)
            pre[i] += llr_e[tri_perm[i]];
        for (i = 0; i < NPOL; i++)
            lbuf[0][i] = pre[sb_perm[i]];

        build_node_uc();
        sc(0, 0, NPOL);

        {
            uint8_t rinfo[K_INFO];
            uint8_t expect[24];
            int j, m;
            uint32_t ninfo = 0, matched = 0, crc_ok = 0;
            for (i = 0; i < NPOL && ninfo < K_INFO; i++)
                if (!is_frozen_rx(i))
                    rinfo[ninfo++] = bbuf[DEPTH][i];
            if (ninfo != K_INFO)
                errs++;
            crc24c_bits(rinfo, A_DCI, expect);
            for (m = 0; m < NCAND; m++) {
                uint8_t got[24];
                int ok = 1;
                for (j = 0; j < 24; j++)
                    got[j] = rinfo[A_DCI + j];
                for (j = 0; j < 16; j++)
                    got[j] ^= (uint8_t)((candidates[m] >> j) & 1u);
                for (j = 0; j < 24; j++)
                    if (got[j] != expect[j]) { ok = 0; break; }
                if (ok && !matched) {
                    matched = candidates[m];
                    crc_ok = 1;
                }
            }
            for (i = 0; i < A_DCI; i++)
                out[48 + i] = rinfo[i];
            for (i = 0; i < 24; i++)
                out[80 + i] = expect[i];
            out[7] = matched;
            out[8] = crc_ok;
            out[5] = 0;
            for (i = 0; i < A_DCI; i++)
                out[5] = (out[5] << 1) | rinfo[i];
        }
        out[9] = llr_bad;
        out[10] = amin;
        out[11] = amax;
    }

    for (i = 0; i < A_DCI; i++)
        out[16 + i] = dci_bits[i];
    for (i = 0; i < E_BITS; i++)
        out[112 + i] = e_sc[i];

    out[0] = 0xCAFEB4B1u;
    out[1] = errs;
    out[2] = mtime() - t0;
    out[3] = runs;
    out[4] = dci_word;
    out[6] = rnti;
    out[12] = n_id;
    out[13] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    mmio_w(EXIT_ADDR, 0x600Du);
    return 0;
}
