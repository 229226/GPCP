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
 * Sequences follow 38.211 (see tools/models/nr_seq.py, which holds the golden model):
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
#include "../../include/gpcp_cgra.h"
#include "../../include/soc_io.h"
#include "pss_corr_prog.h"      /* 编译器生成的 PSS 复数相关 */
#include "pss_corr_layout.h"

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#define NPES             16
#define CORR_CYCLES   PSS_CORR_PROG_CYCLES
#define RING_DEPTH       16
#define NREP             127
#define TAPS_PER_PE      8
#define NCAND            3
#define REF_STRIDE       128        /* words per candidate: keeps 8-byte AXI alignment */
#define TRUE_NID2        1

/* buffers outside .bss so nothing lands on the result area */
#include "../../include/gpcp_ring.h"   /* 描述符环基址跟着 .bss 走（不再硬编码） */
#include "../../include/gpcp_desc64.h"  /* 字段契约生成物：setter 是唯一的位定义 */
#define DESC_BASE     GPCP_DESC_BASE
#define RX_BASE          0x80030000ul          /* 254 words -> 0x8003_03F8 */
#define REF_BASE         0x80030400ul          /* 3 x 128 words -> 0x8003_0A00 */
#define OUT_BASE         0x80030A00ul          /* 3 x 32 words -> 0x8003_0B80 */

static uint32_t (*desc)[8] = (uint32_t (*)[8])_ring_base;
static int32_t *rxw  = (int32_t *)RX_BASE;
static int32_t *refw = (int32_t *)REF_BASE;
static int32_t *outw = (int32_t *)OUT_BASE;


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

/* deterministic noise, mirrored in tools/checks/check_sync.py */
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

static void build_microcode(void)
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
    gpcp_d64_set_src(b, (uint32_t)ram);
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
        dsc(REF_BASE + 4u * (uint32_t)(c * REF_STRIDE), (GPCP_LAYOUT_REGION_REF * GPCP_LAYOUT_WORDS), NREP, 0, 0, GPCP_LAYOUT_WORDS, 8);
        /* results are scattered: 2 words per bank (bank 16..31), stride 32 */
        dsc(OUT_BASE + 4u * (uint32_t)(c * 32), (GPCP_LAYOUT_REGION_LLR * GPCP_LAYOUT_WORDS), GPCP_LAYOUT_WORDS, 0, 1, 0, 0);

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
