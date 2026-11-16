/* GPCP SoC - multi-stage radix-4 / radix-16 FFT (256, 1024, 4096 points)
 *
 * The CGRA page holds 1024 scratchpad words (32 banks x 32) and one array run
 * is capped at 512 cycles, so a 4096-point complex FFT is tiled: the array
 * works on 64 radix-4 butterflies (256 points) while the copy engine fills /
 * drains the other page through the descriptor ring.
 *
 * Two microcode images cover every size (tools/fft_plan.py is the reference):
 *
 *   "stage" image (448 cycles)  one radix-4 DIF stage, 64 butterflies
 *                                data word 128*j + 2*bi + h    (banks 0..15)
 *                                twiddle 512 + (m-1)*128 + bi*2 + h (16..27)
 *   "block" image (336 cycles)  the last three stages fused (radix-16 tail,
 *                                i.e. radix-4 x radix-4 then radix-4) on one
 *                                64-point block, twiddles are stage constants
 *
 * Every tile is moved by four contiguous 128-word copies (one per sub-block j)
 * plus one 384-word twiddle block, so the descriptors never need a 2-D walk.
 *
 * Fixed-point format: Q15 complex, two words per point (re then im), every
 * stage scaled by 1/4 so the result is X/N.  The DIF output sits in base-4
 * digit-reversed order (position i holds X[rev4(i)]).
 *
 * Data flow (RAM stays in natural index order, every stage is in place):
 *   1. for each size, the full stages (L >= 256) tile by tile
 *   2. the fused 64-point tail, one block per array run
 * The twiddle ROM image (tools/fft_plan.py lut) is loaded at LUT_RAM.
 *
 * Result area (RESULT_BASE, 32-bit words):
 *   [0..511]       FFT-256  (256 points x re/im)
 *   [512..2559]    FFT-1024 (1024 points)
 *   [2560..10751]  FFT-4096 (4096 points)
 *   [10752] marker 0xCAFEBABE   [10753..10755] cycles per size
 *   [10756] array runs          [10757] descriptors completed
 *   [10758] errors/timeouts     [10759] epilogue 0x600D
 */

#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "fft_stage_prog.h"   /* 编译器 image：16 轮 × 4 m 组 */
#include "fft_block_prog.h"   /* 编译器 image：radix-16 融合尾 */

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)
#define LUT_RAM          0x80100000ul

#define NPES             16
#define TILE_BFLIES      64
#define DATA_WORDS       512
#define TW_BASE          512
#define TW_WORDS         384
#define TAIL_WORDS       120
#define TAIL_BLOCK       64
#define TAIL_BLOCK_WORDS 128


/* twiddle ROM layout (words) - must match tools/fft_plan.py */
#define LUT_OFS_256      0ul
#define LUT_OFS_1024     384ul
#define LUT_OFS_4096     3456ul
#define LUT_TAIL         21888ul

#define RING_DEPTH       1024
#define MAX_TILES        160

/* --------------------------------------------------------------- scratch */
/* Buffers live at fixed addresses outside .bss: the program image must stay
 * clear of the result area at 0x8001_0000 (the linker would otherwise put a
 * 100 KB .bss right on top of it). */
#include "../include/gpcp_ring.h"   /* 描述符环基址跟着 .bss 走（不再硬编码） */
#include "../include/gpcp_desc64.h"  /* 字段契约生成物：setter 是唯一的位定义 */
#define DESC_BASE     GPCP_DESC_BASE
#define DATA_256    0x80030000ul          /* 512 words  (2 KB) -> 0x80030800 */
#define DATA_1024   0x80031000ul          /* 2048 words (8 KB) -> 0x80033000 */
#define DATA_4096   0x80034000ul          /* 8192 words (32 KB) -> 0x8003C000 */

static uint32_t (*desc)[8] = (uint32_t (*)[8])_ring_base;
static int32_t *d256  = (int32_t *)DATA_256;
static int32_t *d1024 = (int32_t *)DATA_1024;
static int32_t *d4096 = (int32_t *)DATA_4096;


static uint32_t t_dstart[MAX_TILES], t_in_end[MAX_TILES], t_out_end[MAX_TILES];
static uint32_t t_in[MAX_TILES], t_out[MAX_TILES];
static uint32_t t_page[MAX_TILES], t_cyc[MAX_TILES];
static uint32_t n_tiles;
static uint32_t n_desc;

static uint32_t errs;
static uint32_t runs;
static uint32_t cyc_256, cyc_1024, cyc_4096;

static uint32_t rng;

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

/* deterministic test vector, mirrored by tools/check_fft.py */
static int32_t rnd(int32_t mod)
{
    rng = rng * 1664525u + 1013904223u;
    return (int32_t)((rng >> 16) % (uint32_t)(2 * mod + 1)) - mod;
}

/* ------------------------------------------------------------- microcode */
/* 两张镜像全部由 cgra/kernels/fft_stage.yaml / fft_block.yaml 编译：
 * 每个 (轮, m) 是一个 radix-4 蝶形 DFG，rule_map.schedule 编译后由 image
 * 编译器按 at=轮*30、pe_map=[m,m+4,m+8,m+12] 拼回 480 / 360 拍镜像。 */
static void load_prog(uint32_t which)
{
    const uint32_t *p = which ? fft_block_prog : fft_stage_prog;
    uint32_t n = which ? FFT_BLOCK_PROG_CYCLES : FFT_STAGE_PROG_CYCLES;
    for (uint32_t i = 0; i < n * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, p[i]);
}

/* --------------------------------------------------------------- ring setup */
static void dsc(uintptr_t ram, uint32_t spad, uint32_t len, uint32_t page,
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

static void tile_begin(uint32_t page, uint32_t cyc)
{
    t_dstart[n_tiles] = n_desc;
    t_page[n_tiles] = page;
    t_cyc[n_tiles] = cyc;
    t_in[n_tiles] = 0;
    t_out[n_tiles] = 0;
}

static void tile_end(uint32_t in, uint32_t out)
{
    t_in[n_tiles] = in;
    t_out[n_tiles] = out;
    t_out_end[n_tiles] = n_desc;     /* out group of this tile ends here */
    n_tiles++;
}

/* phase 1 descriptors: full stages of one size, ring order [in, tw, out] */
static void desc_full_stages(uint32_t n, int32_t *data, uint32_t lut_ofs,
                             uint32_t *page_toggle)
{
    uint32_t tiles = n / 256;
    uint32_t stages = 0;
    while ((n >> (2 * stages)) >= 256u)
        stages++;

    for (uint32_t s = 0; s < stages; s++) {
        uint32_t L = n >> (2 * s), M = L / 4;
        for (uint32_t q = 0; q < tiles; q++) {
            uint32_t page = (*page_toggle)++;
            uint32_t base = q * TILE_BFLIES;
            uint32_t blk = base / M, t0 = base % M;

            tile_begin(page & 1u, FFT_STAGE_PROG_CYCLES);
            for (uint32_t j = 0; j < 4; j++) {
                uint32_t g0 = blk * L + j * M + t0;
                dsc((uintptr_t)&data[2 * g0], j * 128, 128, page & 1u, 0, 0, 0);
            }
            dsc(LUT_RAM + 4u * (lut_ofs + (s * tiles + q) * TW_WORDS),
                TW_BASE, TW_WORDS, page & 1u, 0, 0, 0);
            t_in_end[n_tiles] = n_desc;              /* in group ends here */
            for (uint32_t j = 0; j < 4; j++) {
                uint32_t g0 = blk * L + j * M + t0;
                dsc((uintptr_t)&data[2 * g0], j * 128, 128, page & 1u, 1, 0, 0);
            }
            tile_end(5, 4);
        }
    }
}

/* phase 2 descriptors: fused 64-point tail, one block per run (linear copies) */
static void desc_tail(uint32_t n, int32_t *data, uint32_t *page_toggle)
{
    for (uint32_t b = 0; b < n / TAIL_BLOCK; b++) {
        uint32_t page = (*page_toggle)++;
        tile_begin(page & 1u, FFT_BLOCK_PROG_CYCLES);
        dsc((uintptr_t)&data[b * TAIL_BLOCK * 2], 0, TAIL_BLOCK_WORDS, page & 1u, 0, 0, 0);
        t_in_end[n_tiles] = n_desc;
        dsc((uintptr_t)&data[b * TAIL_BLOCK * 2], 0, TAIL_BLOCK_WORDS, page & 1u, 1, 0, 0);
        tile_end(1, 1);
    }
}

/* ------------------------------------------------------------- scheduling */
static void wait_desc(uint32_t want)
{
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE) < want) {
        if (++spins > 400000u) { errs++; break; }
    }
}

/* Start the array and wait for it.  The fence keeps the status poll from
 * overtaking the posted start write (CVA6 keeps stores in a write-through
 * buffer), so "busy" is never read as 0 just because the start is still in
 * flight. */
static void array_run(uint32_t page, uint32_t cycles)
{
    gpcp_cgra_array_start(CGRA_BASE, page, cycles);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY) {
        if (++spins > 2000000u) { errs++; break; }
    }
}

/* Run the tiles [first, last) with the image that is currently loaded.  Each
 * tile gets one contiguous ring window [in group][out group]: the engine fills
 * it, the array runs, then the window is extended to the out group and drained
 * before the next tile starts.  An out copy can therefore never read a page
 * before the array has written it (= the bug that hit the 1024-point run). */
static void exec_tiles(uint32_t first, uint32_t last)
{
    uint32_t done = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE);
    uint32_t i;

    for (i = first; i < last; i++) {
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_BASE, (uint32_t)(uintptr_t)&desc[0][0]);
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_SIZE, RING_DEPTH);
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_HEAD, t_dstart[i]);
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_TAIL, t_in_end[i]);
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_CTRL, GPCP_CGRA_RING_EN);

        done += t_in[i];
        wait_desc(done);                       /* tile inputs are in place */
        array_run(t_page[i], t_cyc[i]);
        runs++;
        gpcp_cgra_ring_push(CGRA_BASE, t_out_end[i]);
        done += t_out[i];
        wait_desc(done);                       /* result drained to RAM */
        mmio_w(CGRA_BASE + GPCP_CGRA_RING_CTRL, 0);
    }
}

/* preload the stage-constant tail twiddles into both scratchpad pages */
static void load_tail_twiddles(void)
{
    for (uint32_t page = 0; page < 2u; page++) {
        for (uint32_t i = 0; i < TAIL_WORDS; i++) {
            uint32_t idx = TW_BASE + i;
            uint32_t bank = (idx >> 5) & 0x1Fu, word = idx & 0x1Fu;
            mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(page * GPCP_LAYOUT_PAGE_BANKS + bank, word),
                   mmio_r(LUT_RAM + 4u * (LUT_TAIL + i)));
        }
    }
}

/* ----------------------------------------------------------------- payload */
static void fill_input(int32_t *d, uint32_t n, uint32_t seed)
{
    rng = seed;
    for (uint32_t i = 0; i < n; i++) {
        d[2 * i]     = rnd(3000);
        d[2 * i + 1] = rnd(3000);
    }
}

/* -------------------------------------------------------------------- main */
int main(void)
{
    uint32_t *res = RESULT_BASE;
    uint32_t t_full[3], t_tail[3];
    uint32_t g[4], i, t0;

    fill_input(d256, 256, 0x1357u);
    fill_input(d1024, 1024, 0x2468u);
    fill_input(d4096, 4096, 0x369Cu);

    /* ---- phase 1: every full stage (L >= 256) of all three sizes ---- */
    load_prog(0);
    n_tiles = 0; n_desc = 0;
    {
        uint32_t pg = 0;
        g[0] = n_tiles; desc_full_stages(256,  d256,  LUT_OFS_256,  &pg);
        g[1] = n_tiles; desc_full_stages(1024, d1024, LUT_OFS_1024, &pg);
        g[2] = n_tiles; desc_full_stages(4096, d4096, LUT_OFS_4096, &pg);
        g[3] = n_tiles;
    }
    for (i = 0; i < 3u; i++) {
        t0 = mtime();
#ifdef CHK1024
        if (i == 1u) {
            exec_tiles(g[i], g[i] + 4u);          /* 1024: stage 0 tiles */
            {
                uint32_t *dbg = (uint32_t *)0x80010000ul;
                const uint32_t *e1 = (const uint32_t *)0x80200000ul;
                uint32_t k, bad = 0, first = 0xFFFFFFFFu;
                uint32_t blk[16];
                for (k = 0; k < 16u; k++) blk[k] = 0;
                for (k = 0; k < 1024u; k++) {
                    if ((uint32_t)d1024[2 * k] != e1[2 * k] ||
                        (uint32_t)d1024[2 * k + 1] != e1[2 * k + 1]) {
                        bad++;
                        blk[k >> 6]++;
                        if (first == 0xFFFFFFFFu) first = k;
                    }
                }
                dbg[15000] = bad;
                dbg[15001] = first;
                dbg[15002] = (first == 0xFFFFFFFFu) ? 0u : (first & 63u);
                for (k = 0; k < 16u; k++) dbg[15003 + k] = blk[k];
            }
            exec_tiles(g[i] + 4u, g[i + 1]);      /* 1024: stage 1 tiles */
        } else
#endif
        exec_tiles(g[i], g[i + 1]);
        t_full[i] = mtime() - t0;
#ifdef CHK1024
        if (i == 1u) {
            /* compare the 1024 stage result against ROM snapshots computed
             * offline: after one stage (s1) and after two stages (s2) */
            uint32_t *dbg = (uint32_t *)0x80010000ul;
            const uint32_t *e1 = (const uint32_t *)0x80200000ul;
            const uint32_t *e2 = (const uint32_t *)0x80201000ul;
            uint32_t k, bad1 = 0, bad2 = 0, f1 = 0xFFFFFFFFu, f2 = 0xFFFFFFFFu;
            uint32_t got_r = 0, got_i = 0, exp_r = 0, exp_i = 0;
            uint32_t blk[16];
            for (k = 0; k < 16u; k++) blk[k] = 0;
            for (k = 0; k < 1024u; k++) {
                if ((uint32_t)d1024[2 * k] != e1[2 * k] ||
                    (uint32_t)d1024[2 * k + 1] != e1[2 * k + 1]) {
                    bad1++;
                    if (f1 == 0xFFFFFFFFu) f1 = k;
                }
                if ((uint32_t)d1024[2 * k] != e2[2 * k] ||
                    (uint32_t)d1024[2 * k + 1] != e2[2 * k + 1]) {
                    bad2++;
                    blk[k >> 6]++;
                    if (f2 == 0xFFFFFFFFu) {
                        f2 = k;
                        got_r = (uint32_t)d1024[2 * k];
                        got_i = (uint32_t)d1024[2 * k + 1];
                        exp_r = e2[2 * k];
                        exp_i = e2[2 * k + 1];
                    }
                }
            }
            dbg[16000] = bad1;
            dbg[16001] = f1;
            dbg[16002] = (f1 == 0xFFFFFFFFu) ? 0u : (f1 & 63u);
            dbg[16003] = bad2;
            dbg[16004] = f2;
            dbg[16005] = (f2 == 0xFFFFFFFFu) ? 0u : (f2 & 63u);
            dbg[16006] = got_r;
            dbg[16007] = got_i;
            dbg[16008] = exp_r;
            dbg[16009] = exp_i;
            for (k = 0; k < 16u; k++) dbg[16010 + k] = blk[k];
        }
#endif
    }

    /* ---- phase 2: fused radix-16 tails ---- */
    load_prog(1);
    load_tail_twiddles();
    n_tiles = 0; n_desc = 0;
    {
        uint32_t pg = 0;
        g[0] = n_tiles; desc_tail(256,  d256,  &pg);
        g[1] = n_tiles; desc_tail(1024, d1024, &pg);
        g[2] = n_tiles; desc_tail(4096, d4096, &pg);
        g[3] = n_tiles;
    }
    for (i = 0; i < 3u; i++) {
        t0 = mtime();
        exec_tiles(g[i], g[i + 1]);
        t_tail[i] = mtime() - t0;
    }

    cyc_256  = t_full[0] + t_tail[0];
    cyc_1024 = t_full[1] + t_tail[1];
    cyc_4096 = t_full[2] + t_tail[2];

    /* ---- export (raw g-order: position i holds X[rev4(i)]) ---- */
    for (i = 0; i < 512u; i++)  res[i] = (uint32_t)d256[i];
    for (i = 0; i < 2048u; i++) res[512 + i] = (uint32_t)d1024[i];
    for (i = 0; i < 8192u; i++) res[2560 + i] = (uint32_t)d4096[i];

    res[10752] = 0xCAFEBABEu;
    res[10753] = cyc_256;
    res[10754] = cyc_1024;
    res[10755] = cyc_4096;
    res[10756] = runs;
    res[10757] = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE);
    res[10758] = errs;
    res[10759] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
