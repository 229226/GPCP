/* -----------------------------------------------------------------------------
 * 38.212 LDPC encoding core (BG1/BG2, any Zc).
 *
 * Extracted from sw/soc/bench/ldpc_enc_bm.c (B1) so that the downlink modulation
 * chain (B2) shares one implementation instead of copying it: a bit-order or
 * shift fix must not be able to land in one bench and miss the other.
 *
 * Algorithm (mirrors tools/ldpc_nr.py::encode, cross-checked against
 * ldpc5g.py for BG2 Zc=20):
 *   1. systematic part = the information bits;
 *   2. the 4-row double-diagonal core is solved in closed form (A_V / B_V / RB
 *      from the table), giving parity blocks p0..p3;
 *   3. each remaining row accumulates its information columns plus the already
 *      known parity columns, then shifts to produce its own parity block.
 *
 * The caller includes sw/include/ldpc_enc_tables.h (for ldpc_bg_t and the
 * graph tables) before this file, and may override MAX_ZC.
 * ----------------------------------------------------------------------------- */
#ifndef LDPC_ENC_INC
#define LDPC_ENC_INC

#ifndef MAX_ZC
#define MAX_ZC 64
#endif

static uint8_t le_acc[MAX_ZC];

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

        acc_info(g, c, zc, r, kb, le_acc);
        for (k = 0; k < g->deg[r]; k++) {
            col = g->col[r * g->maxd + k];
            if (col == unknown || col < kb)
                continue;
            if (col > unknown)
                return 2;                   /* later parity block needed */
            sh = g->sh[r * g->maxd + k] % zc;
            for (i = 0; i < zc; i++)
                le_acc[i] ^= c[col * zc + (i + sh) % zc];
        }
        sh = (uint32_t)shv % zc;
        for (i = 0; i < zc; i++)
            c[unknown * zc + i] = le_acc[(i + zc - sh) % zc];
    }
    return 0;
}

static uint32_t ldpc_syndrome(const ldpc_bg_t *g, const uint8_t *c, uint32_t zc)
{
    uint32_t r, k, i, col, sh, bad = 0;
    for (r = 0; r < g->rows; r++) {
        for (i = 0; i < zc; i++)
            le_acc[i] = 0;
        for (k = 0; k < g->deg[r]; k++) {
            col = g->col[r * g->maxd + k];
            sh = g->sh[r * g->maxd + k] % zc;
            for (i = 0; i < zc; i++)
                le_acc[i] ^= c[col * zc + (i + sh) % zc];
        }
        for (i = 0; i < zc; i++)
            if (le_acc[i])
                bad++;
    }
    return bad;
}

#endif /* LDPC_ENC_INC */
