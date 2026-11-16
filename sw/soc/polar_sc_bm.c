/* -----------------------------------------------------------------------------
 * Polar SC decoder on the CGRA (SoC level)
 *
 * N = 64, K = 32 information bits.  The CPU walks the SC tree; every internal
 * node hands its (a, b, beta) triples to the `polar_node` microcode
 * (min-sum f and g, 22 cycles, 16 pairs per run) through the descriptor ring.
 *
 *   f(a,b)   = sign(a)*sign(b)*min(|a|,|b|)
 *   g(a,b,b) = b + (1-2*beta)*a
 *
 * LLRs live in lbuf[depth][node offset], the partial sums in bbuf[depth].
 * Frozen set: index i is frozen when bitrev6(i) < N - K (the classic
 * polarization ordering); frozen bits are 0.
 *
 * Results at 0x8001_0000 (words):
 *   [0] N  [1] K  [2] array runs  [3] cycles
 *   [4] decode mismatches  [5] SC vs transmitted differences
 *   [6..69] decoded bits u^   [70..133] transmitted bits u   [134] 0x600D
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "polar_node_prog.h"      /* 编译器生成的 SC 节点微码 */

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#define NPES             16
#define NODE_CYCLES      POLAR_NODE_PROG_CYCLES
#define RING_DEPTH       16
#define NREC             64              /* N */
#define KINFO            32
#define DEPTH            6               /* log2 N */
#define AMP              3000

#include "../include/gpcp_ring.h"   /* 描述符环基址跟着 .bss 走（不再硬编码） */
#define DESC_BASE     GPCP_DESC_BASE
#define IN_BASE          0x80030000ul          /* a[16] b[16] beta[16] */
#define OUT_BASE         0x80100000ul          /* rotating fresh blocks */
#define OUT_BLOCKS       160                   /* 32 words each */

static uint32_t (*desc)[8] = (uint32_t (*)[8])_ring_base;
static int32_t *ain  = (int32_t *)IN_BASE;
static int32_t *bin  = (int32_t *)(IN_BASE + 64);
static int32_t *bet  = (int32_t *)(IN_BASE + 128);
static int32_t *oblk = (int32_t *)OUT_BASE;
static int out_idx;

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, mism, runs, n_desc, cyc_extra;

static int32_t lbuf[DEPTH + 1][NREC];
static uint8_t bbuf[DEPTH + 1][NREC];

/* --------------------------------------------------------------- sequences */
static uint32_t lcg = 0x5EED0042u;
static int32_t noise(void)
{
    lcg = lcg * 1103515245u + 12345u;
    return (int32_t)((lcg >> 16) & 0x3FFu) - 512;
}

static uint32_t bitrev6(uint32_t v)
{
    uint32_t r = 0;
    for (uint32_t b = 0; b < 6; b++)
        r |= ((v >> b) & 1u) << (5 - b);
    return r;
}

static int is_frozen(uint32_t i) { return bitrev6(i % NREC) < (uint32_t)(NREC - KINFO); }

static void polar_encode(const uint8_t *u, uint8_t *x)
{
    for (int i = 0; i < NREC; i++)
        x[i] = u[i];
    for (int step = 1; step < NREC; step *= 2)
        for (int i = 0; i < NREC; i += 2 * step)
            for (int k = 0; k < step; k++)
                x[i + k] ^= x[i + k + step];
}

/* --------------------------------------------------------------- microcode */
/* 微码来自 cgra/kernels/polar_node.yaml（L1/L2 编译器生成），spad 布局与手写版
 * 完全一致（叶子 bank 0/1/2、汇 16/17，word=实例号），所以流水线其余部分不动。 */
static void build_microcode(void)
{
    for (uint32_t i = 0; i < NODE_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u,
                        polar_node_prog[i]);
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
        if (++spins > 2000000u) { (void)got; errs++; break; }
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

static void array_run(void)
{
    gpcp_cgra_array_start(CGRA_BASE, 0, NODE_CYCLES);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY) {
        if (++spins > 2000000u) { errs++; break; }
    }
    runs++;
}

static uint32_t done;

/* Run one node slice: cnt pairs of (a, b, beta) -> f / g written into
 * lbuf[d+1] (first half f, second half g). */
static void node_run(uint32_t d, uint32_t o, uint32_t half, const uint8_t *bp,
                     uint32_t cnt)
{
    for (uint32_t chunk = 0; chunk < cnt; chunk += NPES) {
        uint32_t n = cnt - chunk < NPES ? cnt - chunk : NPES;
        for (uint32_t k = 0; k < n; k++) {
            ain[k] = lbuf[d][o + chunk + k];
            bin[k] = lbuf[d][o + half + chunk + k];
            bet[k] = bp ? bp[chunk + k] : 0;
        }
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        (void)ain[n - 1];                        /* drain the store buffer */

        int32_t *fb = oblk + out_idx * 32;
        int32_t *gb = fb + 16;
        out_idx = (out_idx + 1) % OUT_BLOCKS;    /* fresh block every run */

        n_desc = 0;
        dsc((uint32_t)(uintptr_t)ain, 0, n, 0, 0, 0, 0);
        dsc((uint32_t)(uintptr_t)bin, (GPCP_LAYOUT_REGION_CONST * GPCP_LAYOUT_WORDS), n, 0, 0, 0, 0);
        dsc((uint32_t)(uintptr_t)bet, (GPCP_LAYOUT_REGION_DATA * GPCP_LAYOUT_WORDS), n, 0, 0, 0, 0);
        dsc((uint32_t)(uintptr_t)fb, (GPCP_LAYOUT_REGION_OUT * GPCP_LAYOUT_WORDS), n, 0, 1, 0, 0);
        dsc((uint32_t)(uintptr_t)gb, ((GPCP_LAYOUT_REGION_OUT + 1) * GPCP_LAYOUT_WORDS), n, 0, 1, 0, 0);

        ring_on(0, 3);
        done += 3;
        wait_desc(done);
        array_run();
        gpcp_cgra_ring_push(CGRA_BASE, 5);
        done += 2;
        wait_desc(done);
        mmio_w(CGRA_BASE + GPCP_CGRA_RING_CTRL, 0);

        for (uint32_t k = 0; k < n; k++) {
            lbuf[d + 1][o + chunk + k] = fb[k];
            lbuf[d + 1][o + half + chunk + k] = gb[k];
        }
    }
}

/* -------------------------------------------------------------- SC recursion */
static void sc(uint32_t d, uint32_t o, uint32_t size)
{
    if (size == 1) {
        bbuf[d][o] = (uint8_t)(is_frozen(o) ? 0 : (lbuf[d][o] < 0));
        return;
    }
    uint32_t half = size >> 1;

    node_run(d, o, half, 0, half);            /* alpha_left = f(...) */
    sc(d + 1, o, half);
    node_run(d, o, half, &bbuf[d + 1][o], half);   /* alpha_right = g(..., beta_left) */
    sc(d + 1, o + half, half);

    for (uint32_t i = 0; i < half; i++) {
        bbuf[d][o + i] = bbuf[d + 1][o + i] ^ bbuf[d + 1][o + half + i];
        bbuf[d][o + half + i] = bbuf[d + 1][o + half + i];
    }
}

/* ------------------------------------------------------------------ epilogue */
static void finish(void)
{
    RESULT_BASE[134] = 0x600D;
    *(volatile uint32_t *)EXIT_ADDR = 0;
    while (1) { }
}

int main(void)
{
    static uint8_t u[NREC], x[NREC], uhat[NREC];

    build_microcode();

    /* information bits over an LCG, frozen bits forced to 0 */
    for (int i = 0; i < NREC; i++) {
        lcg = lcg * 1103515245u + 12345u;
        u[i] = is_frozen((uint32_t)i) ? 0u : (uint8_t)((lcg >> 16) & 1u);
        RESULT_BASE[70 + i] = u[i];
    }
    polar_encode(u, x);
    for (int i = 0; i < NREC; i++)
        lbuf[0][i] = (x[i] ? -AMP : AMP) + noise();

    uint32_t t0 = mtime();
    done = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE);
    sc(0, 0, NREC);
    cyc_extra = mtime() - t0;

    /* SC output: the partial sums of the leaves (bbuf[DEPTH]), not the root
     * (bbuf[0] is the reconstructed codeword) */
    mism = 0;
    for (int i = 0; i < NREC; i++) {
        uhat[i] = bbuf[DEPTH][i];
        RESULT_BASE[6 + i] = uhat[i];
        if (uhat[i] != u[i])
            mism++;
    }

    RESULT_BASE[0] = NREC;
    RESULT_BASE[1] = KINFO;
    RESULT_BASE[2] = runs;
    RESULT_BASE[3] = cyc_extra;
    RESULT_BASE[4] = errs;
    RESULT_BASE[5] = mism;
    finish();
    return (int)mism;
}
