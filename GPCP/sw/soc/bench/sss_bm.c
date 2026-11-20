/* -----------------------------------------------------------------------------
 * NR SSS hypothesis search on the CGRA (SoC level)
 *
 * PSS gives N_ID(2); SSS then has to be found among N_ID(1) = 0..335 (m0 takes
 * the three groups 15*floor(N_ID1/112) + 5*N_ID2 and m1 = N_ID1 mod 112).
 * The rx block is loaded into scratchpad once and stays there; each candidate
 * streams only its 127 reference symbols (8 words per bank = one block per PE)
 * and runs the compiler-generated sss_corr microcode (66 cycles) whose flat
 * addresses are 16p+2k (x), 256+32p+k (ref) and 768+769+2p (partials).
 * The CPU keeps the best |re| + |im| and its N_ID(1).
 *
 * SSS 38.211 7.4.2.3 (mirrored by tools/models/nr_seq.py / check_sss.py):
 *   d(n) = [1-2x0((n+m0) mod 127)] * [1-2x1((n+m1) mod 127)]
 *   x0(i+7) = x0(i+4) + x0(i)   x1(i+7) = x1(i+1) + x1(i)   init [0,...,0,1]
 *
 * Results at 0x8001_0000 (words):
 *   [0] detected N_ID1   [1] true N_ID1   [2] true N_ID2
 *   [3] best metric (|re|+|im| >> 10)     [4] metric of the true candidate
 *   [5] errors   [6] cycles   [7] array runs   [8] 0x600D
 *   [9..40]  partials of the detected candidate
 *   [41..72] partials of the true candidate
 * ----------------------------------------------------------------------------- */
#include "../../include/gpcp_cgra.h"
#include "../../include/soc_io.h"
#include "sss_corr_prog.h"        /* 编译器生成的 SSS 相关微码 */

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#define NPES             16
#define CORR_CYCLES      SSS_CORR_PROG_CYCLES
#define RING_DEPTH       16
#define NREP             127
#define TAPS_PER_PE      8
#define NHYP             336
#define TRUE_NID1        200
#define TRUE_NID2        1

#include "../../include/gpcp_ring.h"   /* 描述符环基址跟着 .bss 走（不再硬编码） */
#include "../../include/gpcp_desc64.h"  /* 字段契约生成物：setter 是唯一的位定义 */
#define DESC_BASE     GPCP_DESC_BASE
#define RX_BASE          0x80030000ul          /* 254 words -> 0x8003_03F8 */
#define REF_BASE         0x80030400ul          /* one 128-word block (8-byte aligned) */
#define OUT_BASE         0x80030800ul          /* 32 words */
/* all 336 reference blocks are generated once, before the search: writing a
 * block right in front of its DMA is not safe (the CPU store buffer is still
 * draining when the engine reads RAM, and we then correlated stale data) */
#define REF_ALL          0x80100000ul          /* 336 x 128 words = 172 KB */
#define REF_STRIDE       128
/* one 32-word result block per candidate: the engine writes RAM, which does
 * not invalidate the CPU D$, so a reused buffer would be read from cache */
#define OUT_ALL          0x80180000ul
#define OUT_STRIDE       32

static uint32_t (*desc)[8] = (uint32_t (*)[8])_ring_base;
static int32_t *rxw  = (int32_t *)RX_BASE;

static int32_t *outw = (int32_t *)OUT_BASE;
static int32_t *refall = (int32_t *)REF_ALL;
static int32_t *outall = (int32_t *)OUT_ALL;

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, n_desc;
static uint8_t  x0seq[2 * NREP], x1seq[2 * NREP];

/* --------------------------------------------------------------- sequences */
static void build_mseq(uint8_t *x, int tap)
{
    for (int i = 0; i < 7; i++)
        x[i] = (uint8_t)(i == 6);              /* init [0,...,0,1] */
    for (int i = 0; i < 2 * NREP - 7; i++)
        x[i + 7] = (uint8_t)(x[i + tap] ^ x[i]);
}

static int sss_sym(int nid1, int nid2, int n)
{
    int m0 = 15 * (nid1 / 112) + 5 * nid2;
    int m1 = nid1 % 112;
    int a = x0seq[(n + m0) % NREP] ? -1 : 1;
    int b = x1seq[(n + m1) % NREP] ? -1 : 1;
    return a * b;
}

/* deterministic noise, mirrored in tools/checks/check_sss.py */
static uint32_t lcg = 0x0BADF00Du;
static int32_t noise(void)
{
    lcg = lcg * 1103515245u + 12345u;
    return (int32_t)((lcg >> 16) & 0x3FFu) - 512;
}

static void build_rx(int nid1, int nid2)
{
    for (int t = 0; t < NREP; t++) {
        int s = sss_sym(nid1, nid2, t);
        rxw[2 * t + 0] = s * 6000 + noise();
        rxw[2 * t + 1] = s * 3000 + noise();
    }
}

static void build_ref_table(int nid2)
{
    for (int nid1 = 0; nid1 < NHYP; nid1++)
        for (int t = 0; t < NREP; t++)
            refall[nid1 * REF_STRIDE + t] = sss_sym(nid1, nid2, t) * 32767;
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)refall[(NHYP - 1) * REF_STRIDE + NREP - 1];   /* drain the store buffer */
}

/* --------------------------------------------------------------- microcode */
/* 微码来自 cgra/kernels/sss_corr.yaml。编译器不用 MAC/acc，实部改 mul+add 两条链，
 * 所以拍数比手写 52 拍多（66）。spad 形状也按线性地址重排过：x 窗 16p+2k、
 * ref 每 PE 一块 256+32p+k、汇压缩成 768/769+2p（只占 1 个 bank）。 */
static void build_microcode(void)
{
    for (uint32_t i = 0; i < CORR_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u,
                        sss_corr_prog[i]);
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

static void array_run(uint32_t page, uint32_t cycles)
{
    gpcp_cgra_array_start(CGRA_BASE, page, cycles);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY) {
        if (++spins > 2000000u) { errs++; break; }
    }
    runs++;
}

/* sum the 16 complex partials of one result block */
static int64_t metric_of(uint32_t nid1)
{
    int32_t *o = outall + nid1 * OUT_STRIDE;
    int64_t re = 0, im = 0;
    for (int p = 0; p < NPES; p++) {
        re += o[2 * p + 0];
        im += o[2 * p + 1];
    }
    return (re < 0 ? -re : re) + (im < 0 ? -im : im);
}

static void dump_partials(uint32_t at, uint32_t nid1)
{
    int32_t *o = outall + nid1 * OUT_STRIDE;
    for (int k = 0; k < 32; k++)
        RESULT_BASE[at + k] = (uint32_t)o[k];
}

/* ------------------------------------------------------------------ epilogue */
static void finish(void)
{
    RESULT_BASE[8] = 0x600D;
    *(volatile uint32_t *)EXIT_ADDR = 0;
    while (1) { }
}

int main(void)
{
    build_mseq(x0seq, 4);
    build_mseq(x1seq, 1);
    build_rx(TRUE_NID1, TRUE_NID2);
    build_ref_table(TRUE_NID2);
    build_microcode();

    uint32_t done = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE);
    uint32_t t0 = mtime();
    uint32_t best = 0, best_metric = 0, true_metric = 0;

    for (uint32_t nid1 = 0; nid1 < NHYP; nid1++) {
        n_desc = 0;
        dsc(RX_BASE, 0, 2 * NREP, 0, 0, 0, 0);                      /* rx every candidate */
        dsc(REF_ALL + 4u * nid1 * REF_STRIDE, (GPCP_LAYOUT_REGION_REF * GPCP_LAYOUT_WORDS), NREP, 0, 0, GPCP_LAYOUT_WORDS, 8);
        dsc(OUT_ALL + 4u * nid1 * OUT_STRIDE, (GPCP_LAYOUT_REGION_LLR * GPCP_LAYOUT_WORDS), GPCP_LAYOUT_WORDS, 0, 1, 0, 0);
        uint32_t nin = 2u;

        ring_on(0, nin);
        done += nin;
        wait_desc(done);
        array_run(0, CORR_CYCLES);
        gpcp_cgra_ring_push(CGRA_BASE, nin + 1);
        done += 1;
        wait_desc(done);
        mmio_w(CGRA_BASE + GPCP_CGRA_RING_CTRL, 0);

        uint32_t mag = (uint32_t)(metric_of(nid1) >> 10);
        if (mag > best_metric) {
            best_metric = mag;
            best = nid1;
        }
        if (nid1 == TRUE_NID1) {
            true_metric = mag;
            dump_partials(41, nid1);
        }
    }

    /* re-run the detected candidate into a fresh block so its partials are
     * read from RAM (block `best` may already be cached) */
    n_desc = 0;
    dsc(RX_BASE, 0, 2 * NREP, 0, 0, 0, 0);
    dsc(REF_ALL + 4u * best * REF_STRIDE, (GPCP_LAYOUT_REGION_REF * GPCP_LAYOUT_WORDS), NREP, 0, 0, GPCP_LAYOUT_WORDS, 8);
    dsc(OUT_ALL + 4u * NHYP * OUT_STRIDE, (GPCP_LAYOUT_REGION_LLR * GPCP_LAYOUT_WORDS), GPCP_LAYOUT_WORDS, 0, 1, 0, 0);
    ring_on(0, 2);
    done += 2;
    wait_desc(done);
    array_run(0, CORR_CYCLES);
    gpcp_cgra_ring_push(CGRA_BASE, 3);
    done += 1;
    wait_desc(done);
    mmio_w(CGRA_BASE + GPCP_CGRA_RING_CTRL, 0);
    dump_partials(9, NHYP);

    if (best != TRUE_NID1)
        errs++;

    RESULT_BASE[0] = best;
    RESULT_BASE[1] = TRUE_NID1;
    RESULT_BASE[2] = TRUE_NID2;
    RESULT_BASE[3] = best_metric;
    RESULT_BASE[4] = true_metric;
    RESULT_BASE[5] = errs;
    RESULT_BASE[6] = (mtime() - t0);
    RESULT_BASE[7] = runs;
    finish();
    return (int)errs;
}
