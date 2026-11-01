/* -----------------------------------------------------------------------------
 * NR PSS detection on the CGRA (SoC level)
 *
 * For each candidate N_ID(2) = 0, 1, 2 the descriptor ring moves
 *      rx  (127 complex points, 254 words, linear)
 *      ref (127 BPSK symbols, 2D walk: 16 words per bank -> banks 8..15)
 * into scratchpad page 0, the array runs `bpsk_corr` (52 cycles) and the ring
 * moves the 32 result words (16 PE * (re, im)) back to RAM.  The CPU sums the
 * 16 complex partials in 64-bit and keeps |re| + |im| as the metric; the
 * largest metric is the detected N_ID(2).
 *
 * Sequences follow 38.211 (see tools/nr_seq.py, which holds the golden model):
 *   PSS: m-sequence x(i+7) = x(i+4) + x(i), init [0,0,0,0,0,0,1],
 *        d(n) = 1 - 2*x((n + 43*N_ID2) mod 127)
 *
 * Results at 0x8001_0000 (words):
 *   [0..2]  metric per candidate (|re|+|im| >> 10)
 *   [3]     detected N_ID(2)        [4]  true N_ID(2)
 *   [5..100] raw partials (3 candidates x 32 words)
 *   [101]   errors/timeouts         [102] cycles
 *   [103]   array runs              [104] 0x600D
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#define NPES             16
#define CORR_CYCLES      52
#define RING_DEPTH       16
#define NREP             127
#define TAPS_PER_PE      8
#define NCAND            3
#define REF_STRIDE       128        /* words per candidate: keeps 8-byte AXI alignment */
#define TRUE_NID2        1

/* buffers outside .bss so nothing lands on the result area */
#define DESC_BASE        0x80020000ul          /* 16 descriptors x 32 B */
#define RX_BASE          0x80030000ul          /* 254 words -> 0x8003_03F8 */
#define REF_BASE         0x80030400ul          /* 3 x 128 words -> 0x8003_0A00 */
#define OUT_BASE         0x80030A00ul          /* 3 x 32 words -> 0x8003_0B80 */

static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;
static int32_t *rxw  = (int32_t *)RX_BASE;
static int32_t *refw = (int32_t *)REF_BASE;
static int32_t *outw = (int32_t *)OUT_BASE;

static inline void mmio_w(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline uint32_t mmio_r(uintptr_t a) { return *(volatile uint32_t *)a; }

static uint32_t prog[CORR_CYCLES * NPES];
static uint32_t cycles;
static uint32_t errs;
static uint32_t runs;
static uint32_t n_desc;
static uint8_t  xseq[2 * NREP];

/* --------------------------------------------------------------- sequences */
static void build_mseq(void)
{
    for (int i = 0; i < 7; i++)
        xseq[i] = (uint8_t)(i == 6);           /* init [0,0,0,0,0,0,1] */
    for (int i = 0; i < 2 * NREP - 7; i++)
        xseq[i + 7] = (uint8_t)(xseq[i + 4] ^ xseq[i]);
}

static int pss_sign(int nid2, int n)
{
    return xseq[(n + 43 * nid2) % NREP] ? -1 : 1;
}

/* deterministic noise, mirrored in tools/check_sync.py */
static uint32_t lcg = 0x12345678u;
static int32_t noise(void)
{
    lcg = lcg * 1103515245u + 12345u;
    return (int32_t)((lcg >> 16) & 0x3FFu) - 512;
}

static void build_rx(int nid2)
{
    for (int t = 0; t < NREP; t++) {
        int s = pss_sign(nid2, t);
        rxw[2 * t + 0] = s * 6000 + noise();
        rxw[2 * t + 1] = s * 3000 + noise();
    }
}

/* --------------------------------------------------------------- microcode */
static uint32_t sidx(uint32_t bank, uint32_t word) { return bank * 32u + word; }

static void build_microcode(void)
{
    for (uint32_t i = 0; i < CORR_CYCLES * NPES; i++)
        prog[i] = gpcp_cgra_inst(GPCP_OP_NOP, 0, 0, 0, 0);

    for (uint32_t pe = 0; pe < NPES; pe++) {
#define EMIT(cyc, op, a, b, d, imm) \
        prog[(cyc) * NPES + pe] = gpcp_cgra_inst(op, a, b, d, imm)
        EMIT(0, GPCP_OP_LACC, GPCP_SRC_ZERO, 0, 0, 0);
        EMIT(1, GPCP_OP_MOV, GPCP_SRC_ZERO, 0, 2, 0);
        for (uint32_t k = 0; k < TAPS_PER_PE; k++) {
            uint32_t t = pe * TAPS_PER_PE + k;
            if (t >= NREP)
                continue;
            uint32_t b = 2 + k * 6;
            EMIT(b + 0, GPCP_OP_LD, 0, 0, 4, sidx(t >> 4, 2 * (t & 15)));
            EMIT(b + 1, GPCP_OP_LD, 0, 0, 5, sidx(t >> 4, 2 * (t & 15) + 1));
            EMIT(b + 2, GPCP_OP_LD, 0, 0, 6, sidx(8 + (t >> 4), t & 15));
            EMIT(b + 3, GPCP_OP_MAC, 4, 6, 0, 0);          /* acc += re*ref */
            EMIT(b + 4, GPCP_OP_MUL, 5, 6, 7, 0);
            EMIT(b + 5, GPCP_OP_ADD, 2, 7, 2, 0);          /* im accumulator */
        }
        EMIT(50, GPCP_OP_ST, GPCP_SRC_ACC, 0, 0, sidx(16 + pe, 0));
        EMIT(51, GPCP_OP_ST, 2, 0, 0, sidx(16 + pe, 1));
#undef EMIT
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

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 2000000u) { errs++; break; }
    }
}

static void ring_on(uint32_t head, uint32_t tail)
{
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_BASE, DESC_BASE);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_SIZE, RING_DEPTH);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_HEAD, head);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_TAIL, tail);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_CTRL, GPCP_CGRA_RING_EN);
}

static void ring_off(void)
{
    mmio_w(CGRA_BASE + GPCP_CGRA_RING_CTRL, 0);
}

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

/* ------------------------------------------------------------------ epilogue */
static void finish(void)
{
    RESULT_BASE[102] = cycles;
    RESULT_BASE[103] = runs;
    RESULT_BASE[104] = 0x600D;
    *(volatile uint32_t *)EXIT_ADDR = 0;
    while (1) { }
}

int main(void)
{
    build_mseq();
    for (int c = 0; c < NCAND; c++)
        for (int t = 0; t < NREP; t++)
            refw[c * REF_STRIDE + t] = pss_sign(c, t) * 32767;

    build_rx(TRUE_NID2);
    build_microcode();

    uint32_t done = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE);
    uint32_t t0 = mtime();

    for (int c = 0; c < NCAND; c++) {
        n_desc = 0;
        dsc(RX_BASE, 0, 2 * NREP, 0, 0, 0, 0);                       /* rx: linear */
        dsc(REF_BASE + 4u * (uint32_t)(c * REF_STRIDE), 8 * 32, NREP, 0, 0, 32, 16);
        /* results are scattered: 2 words per bank (bank 16..31), stride 32 */
        dsc(OUT_BASE + 4u * (uint32_t)(c * 32), 16 * 32, 32, 0, 1, 32, 2);

        ring_on(0, 2);
        done += 2;
        wait_desc(done);                        /* inputs in scratchpad */

        gpcp_cgra_array_start(CGRA_BASE, 0, CORR_CYCLES);
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        uint32_t spins = 0;
        while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY) {
            if (++spins > 2000000u) { errs++; break; }
        }
        runs++;

        gpcp_cgra_ring_push(CGRA_BASE, 3);
        done += 1;
        wait_desc(done);                        /* partials in RAM */
        ring_off();

        int64_t re = 0, im = 0;
        for (int p = 0; p < NPES; p++) {
            re += outw[c * 32 + 2 * p + 0];
            im += outw[c * 32 + 2 * p + 1];
        }
        int64_t mag = (re < 0 ? -re : re) + (im < 0 ? -im : im);
        RESULT_BASE[c] = (uint32_t)(mag >> 10);        /* scale before truncating */
        for (int k = 0; k < 32; k++)
            RESULT_BASE[5 + c * 32 + k] = (uint32_t)outw[c * 32 + k];
    }

    uint32_t best = 0;
    for (int c = 1; c < NCAND; c++)
        if (RESULT_BASE[c] > RESULT_BASE[best])
            best = (uint32_t)c;
    if (best != TRUE_NID2)
        errs++;

    cycles = mtime() - t0;
    RESULT_BASE[3] = best;
    RESULT_BASE[4] = TRUE_NID2;
    RESULT_BASE[101] = errs;
    finish();
    return (int)errs;
}
