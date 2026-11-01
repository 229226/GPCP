/* -----------------------------------------------------------------------------
 * SSB at the sample level: 4096-point FFTs of the four time-domain symbols, then
 * the whole receive chain (SoC level).
 *
 *   * the four SSB symbols are generated offline by tools/ssb_plan.py and
 *     loaded at 0x8040_0000 (4096 complex samples each, int32 re/im)
 *   * the 4096-point radix-4 FFT is the verified engine from
 *     sw/soc/ssb_fft.inc (same microcode/twiddle ROM as run-fft); the spectrum
 *     comes out digit-reversed, so X[k] = data[rev4(k)]
 *   * PSS (3 candidates) and SSS (336 hypotheses) on the symbol 1 / 3 spectra
 *   * DMRS channel estimate per symbol (60 pilots, Gold sequence, c_init =
 *     3*N_ID1 + N_ID2), QPSK demod + PBCH descrambling -> 512 LLRs,
 *     polar SC decode (min-sum node microcode, 22 cycles) -> 32 MIB bits
 *
 * Results at 0x8001_0000 (words):
 *   [0..5]  PSS correlation (re,im) of candidate 0, 1, 2 (>> 16)
 *   [6] detected N_ID2   [7] true N_ID2
 *   [8] errors   [9] cycles   [10] array runs   [11] 0x600D
 *   [12..138]  reference words of candidate 1 read back from the scratchpad
 *   [140..171] 16 x (re,im) per-PE accumulators of candidate 1
 *   [172..425] 254 rx words (packed spectrum) of candidate 1
 *   [426..429] engine-latched descriptor of the reference copy (src,len,stride,row)
 *   [430..941] scratchpad banks 8..23 (the 2D row layout the copy produced)
 *   [942] detected N_ID1   [943] true N_ID1
 *   [944] best SSS metric (>>10)   [945] metric of the true hypothesis
 *   [946..953] channel estimate (re,im) per symbol, H_SCALE = 16
 *   [954..985] decoded MIB payload (32 bits)
 *   [986..990] error breakdown: wait timeout / corr spin / node spin /
 *              block-pool overrun, node blocks used
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "ssb_fid_luts.h"

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#define NPES             16
#define CORR_CYCLES      52
#define RING_DEPTH       1024
#define NREP             127
#define NPSS_RE0         56
#define NSYM             4

#define DESC_BASE        0x80020000ul
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

/* PBCH: DMRS channel estimate -> QPSK demod + descrambling -> polar SC */
#define NRE              240
#define NPILOT           60
#define QPSK             2000
#define KINFO            32
#define NPOL             512
#define SDEPTH           9
#define NODE_CYCLES      22
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

static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;
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

static inline void mmio_w(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline uint32_t mmio_r(uintptr_t a) { return *(volatile uint32_t *)a; }
static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t prog[CORR_CYCLES * NPES];
static uint32_t errs, runs, n_desc, out_idx, done;
static uint32_t dbg_wait, dbg_corr_spin, dbg_array_spin, dbg_nblk;   /* errs breakdown */
static int32_t *last_fb;        /* result block of the most recent corr_run */
static uint32_t latch[4];       /* descriptor the engine latched for the refs copy */

/* host readback of one scratchpad word (register window 0xA000 + idx*4) */
static uint32_t spad_rd(uint32_t idx)
{
    return mmio_r(CGRA_BASE + 0xA000u + idx * 4u);
}

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; dbg_wait++; break; }
    }
}

#include "ssb_fft.inc"          /* 4096-point FFT engine (verified in run-fft) */

/* --------------------------------------------------------------- microcode */
static uint32_t sidx(uint32_t bank, uint32_t word) { return bank * 32u + word; }

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
        prog[i] = gpcp_cgra_inst(GPCP_OP_NOP, 0, 0, 0, 0);
    for (uint32_t pe = 0; pe < NPES; pe++) {
#define E(cyc, op, a, b, d, imm) prog[(cyc) * NPES + pe] = gpcp_cgra_inst(op, a, b, d, imm)
        E(0, GPCP_OP_LACC, GPCP_SRC_ZERO, 0, 0, 0);
        E(1, GPCP_OP_MOV, GPCP_SRC_ZERO, 0, 2, 0);
        for (uint32_t k = 0; k < 8; k++) {
            uint32_t t = pe * 8 + k;
            if (t >= NREP) continue;
            uint32_t base = 2 + k * 6;
            E(base + 0, GPCP_OP_LD, 0, 0, 4, sidx(t >> 4, 2 * (t & 15)));
            E(base + 1, GPCP_OP_LD, 0, 0, 5, sidx(t >> 4, 2 * (t & 15) + 1));
            E(base + 2, GPCP_OP_LD, 0, 0, 6, sidx(8 + (t >> 4), t & 15));
            E(base + 3, GPCP_OP_MAC, 4, 6, 0, 0);
            E(base + 4, GPCP_OP_MUL, 5, 6, 7, 0);
            E(base + 5, GPCP_OP_ADD, 2, 7, 2, 0);
        }
        E(50, GPCP_OP_ST, GPCP_SRC_ACC, 0, 0, sidx(16 + pe, 0));
        E(51, GPCP_OP_ST, 2, 0, 0, sidx(16 + pe, 1));
#undef E
    }
    for (uint32_t i = 0; i < CORR_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, prog[i]);
}

/* ---------------------------------------------------------------- ring glue */
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

/* One correlation on the array: stream the 127 reference words (2D walk, 16
 * words per bank), optionally reload the rx block into spad 0, run the 52-cycle
 * MAC microcode and stream the 16 per-PE partials out.  rx stays in the
 * scratchpad between candidates, so only the first one reloads it. */
static void corr_one(uint32_t ref_ram, int32_t *fb, int with_rx, int cap)
{
    n_desc = 0;
    uint32_t n_in;
    if (with_rx) dsc(RX_BASE, 0, 2 * NREP, 0, 0, 0, 0);    /* rx: linear 254 words */
    dsc(ref_ram, 8 * 32, NREP, 0, 0, 32, 16);             /* refs: 16 words/bank */
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
    dsc((uint32_t)(uintptr_t)fb, 16 * 32, 32, 0, 1, 32, 2); /* results: 2 words/bank */
    gpcp_cgra_ring_push(CGRA_BASE, n_in + 1u);
    done += 1;
    wait_desc(done);
    mmio_w(CGRA_BASE + GPCP_CGRA_RING_CTRL, 0);
}

/* |re| + |im| of one result block (the CPU sum, as in sw/soc/sss_bm.c) */
static uint64_t metric_of(int32_t *fb)
{
    int64_t sr = 0, si = 0;
    for (int p = 0; p < NPES; p++) { sr += fb[2 * p]; si += fb[2 * p + 1]; }
    return (uint64_t)((sr < 0 ? -sr : sr) + (si < 0 ? -si : si));
}

/* ---------------------------------------------------------- PBCH sequences */
/* Gold sequence of 38.211 5.2.1 (x1 with tap 3, x2 with taps 3/2/1), skipped
 * by Nc = 1600 like tools/ssb_plan.py's gold(). */
static void gold_init(uint32_t c_init)
{
    uint32_t n = NPOL + 1600;
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
        prog[i] = gpcp_cgra_inst(GPCP_OP_NOP, 0, 0, 0, 0);
    for (uint32_t pe = 0; pe < NPES; pe++) {
#define E(cyc, op, a, b, d, imm) prog[(cyc) * NPES + pe] = gpcp_cgra_inst(op, a, b, d, imm)
        E(0,  GPCP_OP_LD, 0, 0, 0, sidx(0, pe));
        E(1,  GPCP_OP_LD, 0, 0, 1, sidx(1, pe));
        E(2,  GPCP_OP_LD, 0, 0, 2, sidx(2, pe));
        E(3,  GPCP_OP_SHR, 0, 0, 3, 31);
        E(4,  GPCP_OP_XOR, 0, 3, 4, 0);
        E(5,  GPCP_OP_SUB, 4, 3, 4, 0);
        E(6,  GPCP_OP_SHR, 1, 0, 5, 31);
        E(7,  GPCP_OP_XOR, 1, 5, 6, 0);
        E(8,  GPCP_OP_SUB, 6, 5, 6, 0);
        E(9,  GPCP_OP_MIN, 4, 6, 7, 0);
        E(10, GPCP_OP_XOR, 3, 5, 3, 0);
        E(11, GPCP_OP_XOR, 7, 3, 4, 0);
        E(12, GPCP_OP_SUB, 4, 3, 4, 0);
        E(13, GPCP_OP_SUB, GPCP_SRC_ZERO, 2, 6, 0);
        E(14, GPCP_OP_AND, 0, 6, 7, 0);
        E(15, GPCP_OP_SHL, 7, 0, 7, 1);
        E(16, GPCP_OP_ADD, 1, 0, 6, 0);
        E(17, GPCP_OP_SUB, 6, 7, 6, 0);
        E(18, GPCP_OP_ST, 4, 0, 0, sidx(16, pe));
        E(19, GPCP_OP_ST, 6, 0, 0, sidx(17, pe));
#undef E
    }
    for (uint32_t i = 0; i < NODE_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, prog[i]);
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
}

/* one f/g node over up to 16 LLR pairs: a, b and beta are staged in the
 * scratchpad, the 16 per-PE results are streamed back (16 words each) */
static void node_run(uint32_t d, uint32_t o, uint32_t half, const uint8_t *bp, uint32_t cnt)
{
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
        dsc((uint32_t)(uintptr_t)fb, 16 * 32, n, 0, 1, 0, 0);
        dsc((uint32_t)(uintptr_t)gb, 17 * 32, n, 0, 1, 0, 0);
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

static int pilot_re(int p) { return p * 4; }
static int is_mid(int r) { return r >= NPSS_RE0 && r < NPSS_RE0 + NREP; }

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

/* ------------------------------------------------------------------ epilogue */
static void finish(void)
{
    RESULT_BASE[11] = 0x600D;
    *(volatile uint32_t *)EXIT_ADDR = 0;
    while (1) { }
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

int main(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    int32_t *spec[NSYM];
    for (int s = 0; s < NSYM; s++)
        spec[s] = td + (size_t)s * SYM_WORDS + 2 * CP_LEN;
    uint32_t t0;

    build_mseq();
    build_sss_seq();
    /* ---- FFT of all four SSB symbols ---------------------------------- */
    t0 = mtime();
    for (int s = 0; s < NSYM; s++)
        fft4096(td + s * 2 * FFT_N);
    build_corr_uc();          /* the FFT image owned the imem until here */


    for (int t = 0; t < NREP; t++) {
        uint32_t idx = rev4((uint32_t)(NPSS_RE0 + t));
        /* the fixed-point FFT is normalised by 4096 and pre-scaled by TD_SCALE */
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 0] = spec[0][2 * idx] << 4;
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 1] = spec[0][2 * idx + 1] << 4;
    }

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)rxp[2 * NREP - 1];       /* drain the store buffer before the DMA */
    /* ---- three PSS candidates (all reference blocks generated up front) ---- */
    for (int c = 0; c < 3; c++)
        for (int t = 0; t < NREP; t++)
            pref[c * REF_STRIDE + t] = pss_sign(c, t) * 32767;
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)pref[2 * REF_STRIDE + NREP - 1];

    int64_t best = -1;
    uint32_t nid2 = 0;
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

    /* ---- SSS: 336 hypotheses on the spectrum of symbol 3 ---------------- */
    for (int t = 0; t < NREP; t++) {
        uint32_t idx = rev4((uint32_t)(NPSS_RE0 + t));
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 0] = spec[2][2 * idx] << 4;
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 1] = spec[2][2 * idx + 1] << 4;
    }
    for (int h = 0; h < NHYP; h++)
        for (int t = 0; t < NREP; t++)
            sref[h * REF_STRIDE + t] = sss_sym(h, (int)nid2, t) * 32767;
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)rxp[2 * NREP - 1];
    (void)sref[(NHYP - 1) * REF_STRIDE + NREP - 1];   /* drain both stores */

    uint64_t sbest = 0, strue = 0;
    uint32_t nid1 = 0;
    for (int h = 0; h < NHYP; h++) {
        int32_t *fb = sout + h * 32;
        corr_one(SSS_REF_BASE + 4u * (uint32_t)h * REF_STRIDE, fb, h == 0, 0);
        uint64_t mag = metric_of(fb);
        if (h == (int)TRUE_NID1) strue = mag;
        if (mag > sbest) { sbest = mag; nid1 = (uint32_t)h; }
    }
    if (nid1 != TRUE_NID1)
        errs++;
    RESULT_BASE[942] = nid1;
    RESULT_BASE[943] = TRUE_NID1;
    RESULT_BASE[944] = (uint32_t)(sbest >> 10);
    RESULT_BASE[945] = (uint32_t)(strue >> 10);

    /* ---- grid from the four spectra: 240 REs of every symbol ------------ */
    for (int s = 0; s < NSYM; s++)
        for (int r = 0; r < NRE; r++) {
            if (r == DC_RE) continue;                 /* carrier DC null */
            uint32_t bin = (uint32_t)(NFFT / 2 - DC_RE) + (uint32_t)r;
            uint32_t idx = rev4(bin);
            yw[(s * NRE + r) * 2 + 0] = spec[s][2 * idx] << 4;
            yw[(s * NRE + r) * 2 + 1] = spec[s][2 * idx + 1] << 4;
        }
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)yw[2 * NRE - 1];

    /* ---- DMRS channel estimate, PBCH symbols 1 & 3 (38.211 7.4.1.3) ---- */
    int32_t hre[NSYM], him[NSYM];
    uint32_t dmrs_ci = 0;
    {
        dmrs_ci = pbch_dmrs_cinit((int)nid1, (int)nid2, I_SSB, N_HF);
        gold_init(dmrs_ci);                 /* real PBCH DMRS c_init */
        int n = 0;
        int64_t den = (int64_t)NPILOT * 2 * QPSK * QPSK / H_SCALE;
        int pbch_sym[2] = {1, 3};
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
            hre[s] = (int32_t)div_round(accr, den);
            him[s] = (int32_t)div_round(acci, den);
        }
    }

    /* ---- QPSK demod + PBCH descrambling -> 720 LLRs -------------------- */
    gold_init(nid_cell((int)nid1, (int)nid2));   /* 38.211 7.3.2 c_init = N_ID^cell */
    {
        static int32_t e720[RM_LEN];
        for (int i = 0; i < NPOL; i++) lbuf[0][i] = 0;
        int n = 0;
        int pbch_sym[2] = {1, 3};
        for (int si = 0; si < 2; si++) {
            int s = pbch_sym[si];
            int res[NRE], cnt = 0;
            data_re_list((uint32_t)s, res, &cnt);
            for (int j = 0; j < cnt; j++) {
                int32_t yr = yw[(s * NRE + res[j]) * 2 + 0];
                int32_t yi = yw[(s * NRE + res[j]) * 2 + 1];
                int64_t eqr = (int64_t)yr * hre[s] + (int64_t)yi * him[s];
                int64_t eqi = (int64_t)yi * hre[s] - (int64_t)yr * him[s];
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
        /* inverse PBCH rate matching (38.212 5.4.1): 720 -> 512 LLRs */
        static int32_t zrm[RM_LEN], pre[POLAR_N];
        for (int m = 0; m < RM_LEN; m++) zrm[m] = e720[tri_perm[m]];
        for (int i = 0; i < POLAR_N; i++) pre[i] = 0;
        for (int k = 0; k < RM_LEN; k++) pre[k % POLAR_N] += zrm[k];
        for (int i = 0; i < POLAR_N; i++) lbuf[0][i] = pre[sb_perm[i]];
    }

    /* ---- polar SC decode (N = 512, K = 32) ----------------------------- */
    build_node_uc();
    sc(0, 0, NPOL);
    {
        int k = 0;
        for (uint32_t i = 0; i < NPOL; i++)
            if (!is_frozen(i) && k < KINFO)
                RESULT_BASE[954 + k++] = bbuf[SDEPTH][i];
    }

    RESULT_BASE[0] = 0x600D;
    RESULT_BASE[1] = errs;
    RESULT_BASE[2] = mtime() - t0;
    RESULT_BASE[3] = runs;
    RESULT_BASE[4] = nid2;
    RESULT_BASE[5] = TRUE_NID2;
    RESULT_BASE[6] = nid1;
    RESULT_BASE[7] = TRUE_NID1;
    RESULT_BASE[8] = I_SSB;
    RESULT_BASE[9] = N_HF;
    RESULT_BASE[10] = dmrs_ci;
    RESULT_BASE[11] = (uint32_t)hre[1];
    RESULT_BASE[12] = (uint32_t)him[1];
    RESULT_BASE[13] = (uint32_t)hre[3];
    RESULT_BASE[14] = (uint32_t)him[3];
    {
        int k = 0;
        for (uint32_t i = 0; i < NPOL; i++)
            if (!is_frozen(i) && k < KINFO)
                RESULT_BASE[15 + k++] = bbuf[SDEPTH][i];
    }
    RESULT_BASE[47] = dbg_corr_spin;
    RESULT_BASE[48] = dbg_array_spin;
    RESULT_BASE[49] = dbg_wait;
    RESULT_BASE[50] = nblk;
    finish();
    return (int)errs;
}
