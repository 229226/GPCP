/* -----------------------------------------------------------------------------
 * SSB at the sample level: 4096-point FFT of a time-domain SSB symbol, then
 * PSS detection on the resulting spectrum (SoC level).
 *
 *   * the four SSB symbols are generated offline by tools/ssb_plan.py and
 *     loaded at 0x8040_0000 (4096 complex samples each, int32 re/im)
 *   * the 4096-point radix-4 FFT is the verified engine from
 *     sw/soc/ssb_fft.inc (same microcode/twiddle ROM as run-fft); the spectrum
 *     comes out digit-reversed, so X[k] = data[rev4(k)]
 *   * the 127 PSS resource elements are packed into the bpsk_corr layout and
 *     correlated against the three N_ID(2) candidates; the SSS symbol (3) then
 *     runs the same kernel over all 336 N_ID(1) hypotheses
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
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"

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

static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;
static int32_t *pref = (int32_t *)PSS_REF_BASE;
static int32_t *rxp = (int32_t *)RX_BASE;
static int32_t *oblk = (int32_t *)OUT_BASE;
static int32_t *sref = (int32_t *)SSS_REF_BASE;
static int32_t *sout = (int32_t *)SSS_OUT_BASE;

static inline void mmio_w(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline uint32_t mmio_r(uintptr_t a) { return *(volatile uint32_t *)a; }
static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t prog[CORR_CYCLES * NPES];
static uint32_t errs, runs, n_desc, out_idx, done;
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
        if (++spins > 4000000u) { (void)got; errs++; break; }
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
            if (++spins > 4000000u) { errs++; break; }
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

/* ------------------------------------------------------------------ epilogue */
static void finish(void)
{
    RESULT_BASE[11] = 0x600D;
    *(volatile uint32_t *)EXIT_ADDR = 0;
    while (1) { }
}

int main(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    uint32_t t0;

    build_mseq();
    build_sss_seq();
    /* ---- FFT of the PSS symbol (1) and the SSS symbol (3) ---- */
    t0 = mtime();
    fft4096(td + 1 * 2 * FFT_N);
    fft4096(td + 3 * 2 * FFT_N);
    build_corr_uc();          /* the FFT image owned the imem until here */


    for (int t = 0; t < NREP; t++) {
        uint32_t idx = rev4((uint32_t)(NPSS_RE0 + t));
        /* the fixed-point FFT is normalised by 4096 and pre-scaled by TD_SCALE */
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 0] = td[1 * 2 * FFT_N + 2 * idx] << 4;
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 1] = td[1 * 2 * FFT_N + 2 * idx + 1] << 4;
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
        RESULT_BASE[2 * c + 0] = (uint32_t)(re >> 16);
        RESULT_BASE[2 * c + 1] = (uint32_t)(im >> 16);
        /* probe of the true candidate: what the array actually consumed.
         *   [12 .. 138]   127 reference words as the kernel addresses them
         *   [140 .. 171]  16 x (re,im) per-PE accumulators (RAM copy)
         *   [172 .. 425]  254 rx words (the packed spectrum) read back
         *   [426 .. 429]  engine-latched descriptor (src, len, stride, row)
         *   [430 .. 941]  512 scratchpad words, banks 8..23 (the row layout)  */
        if (c == 1) {
            uint32_t o = 12;
            for (uint32_t t = 0; t < NREP; t++)
                RESULT_BASE[o + t] = spad_rd((8u + (t >> 4)) * 32u + (t & 15u));
            for (uint32_t i = 0; i < 32u; i++)
                RESULT_BASE[o + 128u + i] = (uint32_t)last_fb[i];
            for (uint32_t i = 0; i < 2u * NREP; i++)
                RESULT_BASE[o + 160u + i] = spad_rd(i);
            for (uint32_t i = 0; i < 4u; i++)
                RESULT_BASE[o + 160u + 2u * NREP + i] = latch[i];
            for (uint32_t i = 0; i < 512u; i++)
                RESULT_BASE[o + 160u + 2u * NREP + 4u + i] = spad_rd(8u * 32u + i);
        }
        int64_t mag = (re < 0 ? -re : re) + (im < 0 ? -im : im);
        if (mag > best) { best = mag; nid2 = (uint32_t)c; }
    }

    if (nid2 != 1u)
        errs++;
    RESULT_BASE[6] = nid2;
    RESULT_BASE[7] = 1;

    /* ---- SSS: 336 hypotheses on the spectrum of symbol 3 ---------------- */
    for (int t = 0; t < NREP; t++) {
        uint32_t idx = rev4((uint32_t)(NPSS_RE0 + t));
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 0] = td[3 * 2 * FFT_N + 2 * idx] << 4;
        rxp[(t >> 4) * 32 + 2 * (t & 15) + 1] = td[3 * 2 * FFT_N + 2 * idx + 1] << 4;
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

    RESULT_BASE[8] = errs;
    RESULT_BASE[9] = mtime() - t0;
    RESULT_BASE[10] = runs;
    finish();
    return (int)errs;
}
