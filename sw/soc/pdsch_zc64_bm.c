/* -----------------------------------------------------------------------------
 * One layered min-sum iteration of a 38.212 LDPC code block at Zc = 64, with
 * the check-node work paged 4 x 16 through the 16-PE CGRA array.
 *
 * Built twice from this one source:
 *   pdsch_zc64_bm   -DBG2 (default): BG2, 42 x 52, Kb = 10, 10 edges per node
 *   pdsch_bg1_bm    -DUSE_BG1:       BG1, 46 x 68, Kb = 22, 19 edges (padded 20)
 *
 * Why paging: one PE holds one lifting value, so a Zc = 64 edge message needs
 * four pages of 16 values.  The CN microcode is the same half image shape the
 * Zc = 20 gate uses (PE p owns lifting value p, one edge per bank, min1/min2/sign
 * into three result banks); what changes is that every (check node, page) pair is
 * its own DMA + array run, and the messages are gathered/scattered through a
 * staging buffer.
 *
 * Layered scheduling: as soon as a layer's CN outputs come back the CPU applies
 *   total[v] += c2v_new - c2v_old
 * for that edge, so the next layer already sees the updated total.
 *
 *   TD (0x8040_0000): magic, bg, zc, rows, cols, iters, nllr, ninfo,
 *                     chan LLR (cols*zc int32), info bits (ninfo words)
 *   results 0x8001_0000 : 16 summary words, then
 *             cn_res[rows][4][3][16] (min1, min2, sign per check node / page)
 *             total[cols][64]         (variable totals after the last iteration)
 *   messages c2v[rows][4][edges][16] at 0x8003_0000, total at 0x8007_0000
 *
 * The CN outputs are read back through the spad host window (uncached MMIO)
 * instead of a DMA-out + D-cache flush: the value-by-value check then cannot be
 * poisoned by a stale D-cache line.
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"

#ifndef USE_BG1
#define USE_BG1 0
#endif

#if USE_BG1
#include "../include/ldpc_bg1.h"
#include "pdsch_cn20_prog.h"
#define LDPC_BG   1
#define LROWS     BG1_ROWS
#define LCOLS     BG1_COLS
#define l_deg     bg1_deg
#define l_col     bg1_col
#define l_sh      bg1_sh
#else
/* Zc=64 lives in lifting set i_LS = 0, so this build needs the set-0 table --
 * sw/include/ldpc_bg2.h holds i_LS = 2 for the Zc=20 benches (BG2_A5). */
#include "../include/ldpc_bg2_ils0.h"
#include "pdsch_cn_prog.h"
#define LDPC_BG   2
#define LROWS     BG2_ROWS
#define LCOLS     BG2_COLS
#define l_deg     bg2_deg
#define l_col     bg2_col
#define l_sh      bg2_sh
#endif

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)
#define TD_BASE          0x80400000ul

#define ZC               64
#define PES              16
#define PAGES            (ZC / PES)
#define SENTINEL         32767
#define CYC_CN           (CN_PROG_CYCLES / 2)   /* first half: PEs 0..15 */
#define TD_MAGIC         0x5A433634u

#define CNRES_OFF        16
#define CNRES_BASE       (0x80010000ul + CNRES_OFF * 4)
#define C2V_BASE         0x80030000ul
#define TOTAL_BASE       0x80070000ul
#define STAGE_BASE       0x80080000ul

static int32_t (*cn_res)[PAGES][3][PES] =
    (int32_t (*)[PAGES][3][PES])CNRES_BASE;
static int32_t (*c2v)[PAGES][CN_EDGES][PES] =
    (int32_t (*)[PAGES][CN_EDGES][PES])C2V_BASE;
static int32_t (*total)[ZC] = (int32_t (*)[ZC])TOTAL_BASE;
static int32_t *stage = (int32_t *)STAGE_BASE;   /* CN_EDGES * PES gathered inputs */

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, dmas;

static void load_prog(void)
{
    if (gpcp_cgra_imem_load(CGRA_BASE, cn_prog, CN_PROG_CYCLES * PES) != 0)
        errs++;
}

int main(void)
{
    uint32_t *out = RESULT_BASE;
    const uint32_t *td = (const uint32_t *)TD_BASE;
    const int32_t *chan;
    const uint32_t *info;
    uint32_t cyc, it, r, j, k, i, m;
    uint32_t syn_ones = 0, info_bad = 0;
    uint32_t trows, tcols, nllr, ninfo, iters;

    if (td[0] != TD_MAGIC || td[1] != LDPC_BG || td[2] != ZC) {
        out[0] = td[0];
        out[7] = 0xDEADu;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 1;
    }
    trows = td[3];
    tcols = td[4];
    iters = td[5];
    nllr = td[6];
    ninfo = td[7];
    if (trows != LROWS || tcols != LCOLS || nllr != LCOLS * ZC) {
        out[0] = trows * 100 + tcols;
        out[7] = 0xBEEFu;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 1;
    }
    chan = (const int32_t *)(TD_BASE + 8 * 4);
    info = (const uint32_t *)(TD_BASE + 8 * 4 + nllr * 4);

    for (uint32_t v = 0; v < LCOLS; v++)
        for (i = 0; i < ZC; i++)
            total[v][i] = chan[v * ZC + i];
    for (r = 0; r < LROWS; r++)
        for (j = 0; j < PAGES; j++)
            for (k = 0; k < CN_EDGES; k++)
                for (i = 0; i < PES; i++)
                    c2v[r][j][k][i] = 0;

    load_prog();

    /* the sentinel constant must exist in both pages (page-relative bank) */
    for (i = 0; i < PES; i++) {
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_ADDR(CN_SENTINEL_BANK, i), SENTINEL);
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_ADDR(CN_SENTINEL_BANK + GPCP_LAYOUT_PAGE_BANKS, i), SENTINEL);
    }

    uint32_t t0 = mtime();
    for (it = 0; it < iters; it++) {
        for (r = 0; r < LROWS; r++) {
            uint32_t deg = l_deg[r];
            for (j = 0; j < PAGES; j++) {
                uint32_t page = (r * PAGES + j) & 1u;

                /* v2c = total - c2v, gathered with the edge's lifting shift */
                for (k = 0; k < CN_EDGES; k++) {
                    uint32_t col = l_col[r][k], sh = l_sh[r][k];
                    for (i = 0; i < PES; i++) {
                        if (k >= deg) {
                            stage[k * PES + i] = SENTINEL;
                            continue;
                        }
                        uint32_t idx = (j * PES + i + sh) & (ZC - 1u);
                        stage[k * PES + i] = total[col][idx] - c2v[r][j][k][i];
                    }
                }
                /* the copy engine reads RAM directly: drain the store buffer
                 * (the D-cache is write-through, the store buffer is not) */
                __asm__ __volatile__("fence iorw, iorw" ::: "memory");
                (void)stage[CN_EDGES * PES - 1];

                if (rt_dma_begin(CGRA_BASE, (uintptr_t)stage, 0, CN_EDGES * PES,
                                 page, 0, 32, PES) != 0)
                    errs++;
                dmas++;
                if (rt_dma_wait(CGRA_BASE) != 0)
                    errs++;
                if (rt_array_run(CGRA_BASE, page, CYC_CN) != 0)
                    errs++;
                runs++;

                /* read min1 / min2 / sign straight out of the spad host window */
                for (m = 0; m < 3; m++)
                    for (i = 0; i < PES; i++)
                        cn_res[r][j][m][i] = (int32_t)gpcp_cgra_read(
                            CGRA_BASE, GPCP_SPAD_ADDR(CN_MIN1_BANK + m + (page ? GPCP_LAYOUT_PAGE_BANKS : 0), i));

                /* immediate VN update for this layer (= layered scheduling) */
                for (k = 0; k < deg; k++) {
                    uint32_t col = l_col[r][k], sh = l_sh[r][k];
                    for (i = 0; i < PES; i++) {
                        int32_t old = c2v[r][j][k][i];
                        int32_t x = stage[k * PES + i];   /* the CN input of this edge */
                        int32_t ax = x < 0 ? -x : x;
                        /* min-sum with only min1/min2: an edge that *is* the
                         * minimum takes min2, everything else takes min1 */
                        int32_t mag = (ax == cn_res[r][j][0][i]) ? cn_res[r][j][1][i]
                                                                 : cn_res[r][j][0][i];
                        int32_t val = (mag * 3) >> 2;          /* normalised 0.75 */
                        int32_t sg = cn_res[r][j][2][i] ^ (x < 0 ? -1 : 0);
                        int32_t nw = sg ? -val : val;
                        uint32_t idx = (j * PES + i + sh) & (ZC - 1u);
                        total[col][idx] += nw - old;
                        c2v[r][j][k][i] = nw;
                    }
                }
            }
        }
    }
    cyc = mtime() - t0;

    /* syndrome: s[i] = XOR over the row's edges of bit[col][(i+sh) % Zc] */
    for (i = 0; i < ZC; i++) {
        uint8_t s = 0;
        for (r = 0; r < LROWS; r++)
            for (k = 0; k < l_deg[r]; k++) {
                uint32_t sh = l_sh[r][k];
                s ^= (uint8_t)(total[l_col[r][k]][(i + sh) & (ZC - 1u)] < 0);
            }
        syn_ones += s;
    }
    for (i = 0; i < ninfo; i++)
        info_bad += (((total[i / ZC][i % ZC] < 0) ? 1u : 0u) != (info[i] & 1u));

    /* total goes into the dump right after cn_res (the checker compares both) */
    int32_t *dump_tot = (int32_t *)(CNRES_BASE + LROWS * PAGES * 3 * PES * 4);
    for (uint32_t v = 0; v < LCOLS; v++)
        for (i = 0; i < ZC; i++)
            dump_tot[v * ZC + i] = total[v][i];

    out[0] = 0xCAFEBABEu;
    out[1] = LDPC_BG;
    out[2] = ZC;
    out[3] = LROWS;
    out[4] = runs;
    out[5] = dmas;
    out[6] = cyc;
    out[7] = errs;
    out[8] = syn_ones;
    out[9] = info_bad;
    out[10] = iters;
    out[11] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
