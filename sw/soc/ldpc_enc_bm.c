/* -----------------------------------------------------------------------------
 * G2 / B1: 38.212 LDPC encoder on CVA6 (BG1/BG2 x Zc=20/64).
 *
 * Deliberately no array, no microcode, no DMA: encoding is scalar shift-and-XOR
 * work, and T0 measured ~1456 cycles of DMA/orchestration per array run -- so
 * pushing this onto the CGRA would cost more than it saves.  The base graphs
 * come from the compiler-style generated header tools/gen_ldpc_enc_tables.py.
 *
 * Algorithm (mirrors tools/ldpc_nr.py::encode, itself cross-checked against
 * ldpc5g.py for BG2 Zc=20):
 *   1. systematic part = the information bits;
 *   2. the 4-row double-diagonal core is solved in closed form (A, B, RB taken
 *      from the table), giving parity blocks p0..p3;
 *   3. each remaining row accumulates its information columns plus the already
 *      known parity columns, then shifts to produce its own parity block.
 * A syndrome pass (H*c) is computed on the result as an internal check.
 *
 *   TD (0x8040_0000): magic, bg, ils, zc, kb_used, nbits, -, -,
 *                     then nbits information bits packed LSB-first
 *   results 0x8001_0000:
 *     [0] marker 0x1D5C0DE1 [1] bg [2] ils [3] zc [4] kb_used
 *     [5] rows [6] cols [7] syndrome weight [8] cycles [9] array runs
 *     [10] errors [11] 0x600D
 *     [16..] codeword bits packed LSB-first
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/ldpc_enc_tables.h"

#define CGRA_BASE    0x50000000ul
#define EXIT_ADDR    0x40010000ul
#define RESULT_BASE  ((uint32_t *)0x80010000ul)
#define TD_BASE      0x80400000ul
#define TD_MAGIC     0x1D5C0DE1ul
#define MAX_ROWS     46
#define MAX_COLS     68
#define MAX_ZC       64
#define META         12

static uint8_t cw[MAX_COLS * MAX_ZC];
static uint8_t info[MAX_ROWS * MAX_ZC];
static uint8_t acc[MAX_ZC];

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static void pack_bits(const uint8_t *bits, uint32_t n, uint32_t *out)
{
    uint32_t i, w = (n + 31u) / 32u;
    for (i = 0; i < w; i++)
        out[i] = 0;
    for (i = 0; i < n; i++)
        if (bits[i])
            out[i / 32u] |= 1u << (i % 32u);
}

static void unpack_bits(const uint32_t *in, uint32_t n, uint8_t *bits)
{
    uint32_t i;
    for (i = 0; i < n; i++)
        bits[i] = (uint8_t)((in[i / 32u] >> (i % 32u)) & 1u);
}

/* acc = XOR of c[col] rotated by sh, over every edge with col < upto. */
static void acc_info(const ldpc_bg_t *g, const uint8_t *c, uint32_t zc,
                     uint32_t r, uint32_t upto, uint8_t *out)
{
    uint32_t k, i, col, sh;
    for (i = 0; i < zc; i++)
        out[i] = 0;
    for (k = 0; k < g->deg[r]; k++) {
        col = g->col[r * g->maxd + k];
        if (col >= upto)
            continue;
        sh = g->sh[r * g->maxd + k] % zc;
        for (i = 0; i < zc; i++)
            out[i] ^= c[col * zc + (i + sh) % zc];
    }
}

static uint32_t ldpc_encode(const ldpc_bg_t *g, const uint8_t *info_bits,
                            uint32_t kb_used, uint32_t zc, uint8_t *c)
{
    static uint8_t t0[MAX_ZC], t1[MAX_ZC], t2[MAX_ZC], t3[MAX_ZC];
    static uint8_t u[MAX_ZC], p0[MAX_ZC], p1[MAX_ZC], p2[MAX_ZC], p3[MAX_ZC];
    uint32_t rows = g->rows, cols = g->cols, kb = g->kb;
    uint32_t i, r, k, col, sh, unknown;
    uint32_t a, b;
    int shv;

    for (i = 0; i < cols * zc; i++)
        c[i] = 0;
    for (i = 0; i < kb_used * zc; i++)
        c[i] = info_bits[i];

    a = g->a_v % zc;
    b = g->b_v % zc;
    acc_info(g, c, zc, 0, kb, t0);
    acc_info(g, c, zc, 1, kb, t1);
    acc_info(g, c, zc, 2, kb, t2);
    acc_info(g, c, zc, 3, kb, t3);
    for (i = 0; i < zc; i++)
        u[i] = t0[i] ^ t1[i] ^ t2[i] ^ t3[i];
    for (i = 0; i < zc; i++)
        p0[i] = u[(i + zc - b) % zc];
    for (i = 0; i < zc; i++) {
        p1[i] = t0[i] ^ p0[(i + a) % zc];
        p3[i] = t3[i] ^ p0[(i + a) % zc];
    }
    if (g->rb == 1)
        for (i = 0; i < zc; i++)
            p2[i] = t1[i] ^ p1[i] ^ p0[(i + b) % zc];
    else
        for (i = 0; i < zc; i++)
            p2[i] = t1[i] ^ p1[i];

    for (k = 0; k < zc; k++) {
        c[(kb + 0) * zc + k] = p0[k];
        c[(kb + 1) * zc + k] = p1[k];
        c[(kb + 2) * zc + k] = p2[k];
        c[(kb + 3) * zc + k] = p3[k];
    }

    for (r = 4; r < rows; r++) {
        unknown = kb + r;
        shv = -1;
        for (k = 0; k < g->deg[r]; k++)
            if (g->col[r * g->maxd + k] == unknown)
                shv = g->sh[r * g->maxd + k];
        if (shv < 0)
            return 1;                       /* no diagonal parity block */

        acc_info(g, c, zc, r, kb, acc);
        for (k = 0; k < g->deg[r]; k++) {
            col = g->col[r * g->maxd + k];
            if (col == unknown || col < kb)
                continue;
            if (col > unknown)
                return 2;                   /* later parity block needed */
            sh = g->sh[r * g->maxd + k] % zc;
            for (i = 0; i < zc; i++)
                acc[i] ^= c[col * zc + (i + sh) % zc];
        }
        sh = (uint32_t)shv % zc;
        for (i = 0; i < zc; i++)
            c[unknown * zc + i] = acc[(i + zc - sh) % zc];
    }
    return 0;
}

static uint32_t ldpc_syndrome(const ldpc_bg_t *g, const uint8_t *c, uint32_t zc)
{
    uint32_t r, k, i, col, sh, bad = 0;
    for (r = 0; r < g->rows; r++) {
        for (i = 0; i < zc; i++)
            acc[i] = 0;
        for (k = 0; k < g->deg[r]; k++) {
            col = g->col[r * g->maxd + k];
            sh = g->sh[r * g->maxd + k] % zc;
            for (i = 0; i < zc; i++)
                acc[i] ^= c[col * zc + (i + sh) % zc];
        }
        for (i = 0; i < zc; i++)
            if (acc[i])
                bad++;
    }
    return bad;
}

int main(void)
{
    uint32_t *res = RESULT_BASE;
    const uint32_t *td = (const uint32_t *)TD_BASE;
    const ldpc_bg_t *g = 0;
    uint32_t bg, ils, zc, kb_used, nbits, t0, rc, syn;

    if (td[0] != TD_MAGIC) {
        res[0] = 0x0BAD1D5Cu;
        res[META - 1] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 0;
    }
    bg = td[1];
    ils = td[2];
    zc = td[3];
    kb_used = td[4];
    nbits = td[5];

    if (bg == 1 && ils == 0) g = &LDPC_BG1_ILS0;
    else if (bg == 1 && ils == 2) g = &LDPC_BG1_ILS2;
    else if (bg == 2 && ils == 0) g = &LDPC_BG2_ILS0;
    else if (bg == 2 && ils == 2) g = &LDPC_BG2_ILS2;

    if (!g || zc == 0 || zc > MAX_ZC || kb_used > g->kb ||
        nbits != kb_used * zc) {
        res[0] = 0x0BAD1D5Cu;
        res[1] = bg;
        res[2] = ils;
        res[3] = zc;
        res[4] = kb_used;
        res[META - 1] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 0;
    }

    unpack_bits(td + 8, nbits, info);

    t0 = mtime();
    rc = ldpc_encode(g, info, kb_used, zc, cw);
    res[8] = mtime() - t0;

    syn = (rc == 0) ? ldpc_syndrome(g, cw, zc) : 0xFFFFFFFFu;

    pack_bits(cw, g->cols * zc, res + 16);

    res[0]  = TD_MAGIC;
    res[1]  = bg;
    res[2]  = ils;
    res[3]  = zc;
    res[4]  = kb_used;
    res[5]  = g->rows;
    res[6]  = g->cols;
    res[7]  = syn;
    res[9]  = 0;
    res[10] = rc;
    res[11] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
