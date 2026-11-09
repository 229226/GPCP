/* -----------------------------------------------------------------------------
 * G2 / B2: downlink modulation chain on CVA6 (38.212 5.4.2 + 38.211 7.3).
 *
 * TB -> CRC24A -> LDPC encode -> rate matching -> scrambling -> bit
 * interleaving -> QAM mapping -> layer mapping (single layer, single port).
 *
 * Everything here is scalar bit work, exactly like B1: T0 measured ~1456 cycles
 * of DMA/orchestration per array run, so pushing this onto the CGRA would cost
 * more than it saves.  No kernel, no IMEM, no DMA in this benchmark.
 *
 * The pieces that already exist are shared, not copied:
 *   sw/soc/ldpc_enc.inc   acc_info / ldpc_encode / ldpc_syndrome   (B1)
 *   sw/soc/gold_seq.inc   38.211 5.2.1 Gold sequence               (A2)
 * so a bit-order fix cannot land in one bench and miss another.
 *
 * Rate matching follows the same parameter set the hardware receive chain
 * already decodes (tools/pdsch_plan.py / tools/pdsch_full_plan.py):
 *   KB=6, ZC=20 -> K=120 information bits (80 TB + 24 CRC24A + 16 filler)
 *   NCODED=1040, PUNCT=40 (2*Zc), buffer 1000 bits, filler at buffer 64..79
 *   USABLE=984, E=720, K0 = rv * 246
 *
 *   TD (0x8040_0000):
 *     [0] magic 0x1D10D001  [1] nvar  [2] a_tb  [3] tb_words
 *     [4..] TB bits packed LSB-first
 *     [8 + 4*v] variant v: {qm, rv, n_rnti, reserved}
 *   results 0x8001_0000:
 *     [0] marker [1] nvar [2] errors [3] cycles [4] array runs
 *     [5] a_tb [6] K [7] 0x600D
 *     blocks at 8 + v*BLK, each BLK words:
 *       cw[33] usable[31] e[23] e_sc[23] f[23] syms[720] meta[8]
 *     meta = {qm, rv, k0, nsym, crc_ok, syndrome, filler_n, 0}
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/ldpc_enc_tables.h"
#include "ldpc_enc.inc"
#include "gold_seq.inc"

#define CGRA_BASE    0x50000000ul
#define EXIT_ADDR    0x40010000ul
#define RESULT_BASE  ((uint32_t *)0x80010000ul)
#define TD_BASE      0x80400000ul
#define TD_MAGIC     0x1D10D001ul

#define A_TB         80
#define CRC_LEN      24
#define CRC_POLY     0x864CFBu
#define KB           6
#define ZC           20
#define K_BITS       (KB * ZC)              /* 120 */
#define NCODED       (52 * ZC)              /* 1040 */
#define PUNCT        (2 * ZC)               /* 40 */
#define NBUF         (NCODED - PUNCT)       /* 1000 */
#define FILLER0      (A_TB + CRC_LEN - PUNCT)   /* 64, buffer coordinates */
#define FILLER_N     (K_BITS - A_TB - CRC_LEN)  /* 16 */
#define USABLE       (NBUF - FILLER_N)      /* 984 */
#define E_BITS       720
#define RV_STEP      (USABLE / 4)           /* 246 */
#define MAX_VAR      4
#define MAX_SYM      (E_BITS / 2)           /* 360 QPSK symbols */
#define MAXD         10

#define CW_W         ((NCODED + 31) / 32)   /* 33 */
#define US_W         ((USABLE + 31) / 32)   /* 31 */
#define E_W          ((E_BITS + 31) / 32)   /* 23 */
#define SYM_W        (2 * MAX_SYM)          /* 720 */
#define BLK          (CW_W + US_W + E_W + E_W + E_W + SYM_W + 8)
#define META_HDR     8

static const uint32_t QAM_AMP[7] = {0, 0, 2000, 0, 894, 0, 436};
static const uint32_t QAM_NB[7] = {0, 0, 1, 0, 2, 0, 3};   /* bits per branch */

static uint8_t tb_bits[A_TB];
static uint8_t info[K_BITS];
static uint8_t cw[NCODED];
static uint8_t usable[USABLE];
static uint8_t e_bits[E_BITS];
static uint8_t e_sc[E_BITS];
static uint8_t f_bits[E_BITS];
static uint8_t scr[E_BITS];

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static void unpack_bits(const uint32_t *in, uint32_t n, uint8_t *bits)
{
    uint32_t i;
    for (i = 0; i < n; i++)
        bits[i] = (uint8_t)((in[i >> 5] >> (i & 31u)) & 1u);
}

static void pack_bits(const uint8_t *bits, uint32_t n, uint32_t *out)
{
    uint32_t i, w = (n + 31u) / 32u;
    for (i = 0; i < w; i++)
        out[i] = 0;
    for (i = 0; i < n; i++)
        if (bits[i])
            out[i >> 5] |= 1u << (i & 31u);
}

/* 38.212 7.1 gCRC24A(D), MSB-first output. */
static uint32_t crc24a(const uint8_t *bits, uint32_t n)
{
    uint32_t reg = 0, i, j;
    for (i = 0; i < n; i++) {
        reg ^= (uint32_t)(bits[i] & 1u) << 23;
        for (j = 0; j < 24u; j++)
            reg = (reg & 0x800000u) ? (((reg << 1) ^ CRC_POLY) & 0xFFFFFFu)
                                    : ((reg << 1) & 0xFFFFFFu);
    }
    return reg;
}

/* 38.212 5.4.2.1 bit selection: puncture 2*Zc, skip the filler, then read
 * E bits from the circular buffer at the redundancy-version offset k0. */
static uint32_t rate_match(const uint8_t *cw_bits, uint32_t rv,
                           uint8_t *out, uint8_t *buf_out, uint32_t *k0_out)
{
    uint32_t i, j, k, u = 0;
    uint32_t k0 = rv * RV_STEP;

    for (i = 0; i < NBUF; i++) {
        uint32_t b = cw_bits[PUNCT + i];
        buf_out[i] = (uint8_t)b;
        if (i >= FILLER0 && i < FILLER0 + FILLER_N)
            continue;                       /* NULL: not transmitted */
        usable[u++] = (uint8_t)b;
        if (u > USABLE)
            return 1;
    }
    if (u != USABLE)
        return 2;
    for (j = 0; j < E_BITS; j++) {
        k = (k0 + j) % USABLE;
        out[j] = usable[k];
    }
    *k0_out = k0;
    return 0;
}

/* 38.212 5.4.2.2 bit interleaving: write rows of Qm, read columns. */
static void interleave(const uint8_t *in, uint32_t qm, uint8_t *out)
{
    uint32_t rows = E_BITS / qm, i, j;
    for (i = 0; i < rows; i++)
        for (j = 0; j < qm; j++)
            out[qm * i + j] = in[j * rows + i];
}

/* 38.211 7.3.1.2 square-QAM Gray level for one I/Q branch.
 * b0 is the sign (0 -> +), the rest is a Gray-coded magnitude. */
static int32_t branch_level(const uint8_t *b, uint32_t n)
{
    int32_t sign = b[0] ? -1 : 1;
    int32_t mag;
    if (n == 1) {
        mag = 1;
    } else if (n == 2) {
        mag = b[1] ? 1 : 3;                 /* b1=0 -> 3, b1=1 -> 1 */
    } else {
        if (!b[1] && !b[2])      mag = 1;   /* b1b2 = 00 -> 1 */
        else if (!b[1] && b[2])  mag = 3;   /*         01 -> 3 */
        else if (b[1] && b[2])   mag = 5;   /*         11 -> 5 */
        else                     mag = 7;   /*         10 -> 7 */
    }
    return sign * mag;
}

int main(void)
{
    uint32_t *res = RESULT_BASE;
    const uint32_t *td = (const uint32_t *)TD_BASE;
    uint32_t tb_words, nvar, v, i, rc = 0, errors = 0, t0;
    uint32_t crc, syn_all = 0;

    if (td[0] != TD_MAGIC) {
        res[0] = 0x0BAD0D10u;
        res[META_HDR - 1] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        mmio_w(EXIT_ADDR, 0x600Du);
        for (;;) ;
    }

    nvar = td[1];
    tb_words = td[3];
    if (nvar == 0 || nvar > MAX_VAR || tb_words != (A_TB + 31u) / 32u) {
        res[0] = 0x0BAD0D11u;
        res[META_HDR - 1] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        mmio_w(EXIT_ADDR, 0x600Du);
        for (;;) ;
    }

    t0 = mtime();

    /* ---- share the transport block, build the LDPC information vector ---- */
    unpack_bits(td + 4, A_TB, tb_bits);
    for (i = 0; i < A_TB; i++)
        info[i] = tb_bits[i];
    crc = crc24a(tb_bits, A_TB);
    for (i = 0; i < CRC_LEN; i++)
        info[A_TB + i] = (uint8_t)((crc >> (23u - i)) & 1u);
    for (i = A_TB + CRC_LEN; i < K_BITS; i++)
        info[i] = 0;                        /* filler: known zero, not sent */

    rc = ldpc_encode(&LDPC_BG2_ILS2, info, KB, ZC, cw);
    syn_all = (rc == 0) ? ldpc_syndrome(&LDPC_BG2_ILS2, cw, ZC) : 0xFFFFFFFFu;
    if (rc != 0 || syn_all != 0)
        errors++;

    /* ---- per-variant modulation ---- */
    for (v = 0; v < nvar; v++) {
        const uint32_t *vp = td + 8 + 4 * v;
        uint32_t qm = vp[0], rv = vp[1], n_rnti = vp[2];
        uint32_t k0 = 0, nsym, n, k, j, m;
        uint32_t *blk = res + META_HDR + v * BLK;
        uint32_t *p_cw = blk;
        uint32_t *p_us = blk + CW_W;
        uint32_t *p_e = blk + CW_W + US_W;
        uint32_t *p_esc = blk + CW_W + US_W + E_W;
        uint32_t *p_f = blk + CW_W + US_W + 2 * E_W;
        uint32_t *p_sym = blk + CW_W + US_W + 3 * E_W;
        uint32_t *p_meta = p_sym + SYM_W;

        if ((qm != 2 && qm != 4 && qm != 6) || rv > 3 || n_rnti > 0xFFFFu) {
            p_meta[5] = 0xBAD00000u | v;
            errors++;
            continue;
        }

        /* 1. rate matching (38.212 5.4.2.1) */
        if (rate_match(cw, rv, e_bits, usable, &k0) != 0) {
            p_meta[5] = 0xBAD00001u | v;
            errors++;
            continue;
        }

        /* 2. scrambling (38.211 7.3.1.1, q=0, n_f=0 -> c_init = n_RNTI<<15) */
        gold_seq(n_rnti << 15, E_BITS, scr);
        for (j = 0; j < E_BITS; j++)
            e_sc[j] = (uint8_t)(e_bits[j] ^ scr[j]);

        /* 3. bit interleaving with Qm columns (38.212 5.4.2.2) */
        interleave(e_sc, qm, f_bits);

        /* 4. QAM mapping (38.211 7.3.1.2) + layer mapping / precoding.
         *    Single layer, single antenna port: x = d and y = x, written out
         *    explicitly so the 1-layer assumption is visible rather than
         *    implied by skipping the step. */
        n = QAM_NB[qm];
        nsym = E_BITS / qm;
        for (k = 0; k < nsym; k++) {
            const uint8_t *grp = f_bits + k * qm;
            int32_t il = branch_level(grp, n);
            int32_t ql = branch_level(grp + n, n);
            int32_t y_re = il * (int32_t)QAM_AMP[qm];   /* layer 0 -> port 0 */
            int32_t y_im = ql * (int32_t)QAM_AMP[qm];
            p_sym[2 * k] = (uint32_t)y_re;
            p_sym[2 * k + 1] = (uint32_t)y_im;
        }
        for (k = 2 * nsym; k < SYM_W; k++)
            p_sym[k] = 0xDEADBEEFu;         /* unused tail is marked, not zero */

        /* 5. dump every intermediate stage so the checker can gate each one */
        pack_bits(cw, NCODED, p_cw);
        pack_bits(usable, USABLE, p_us);
        pack_bits(e_bits, E_BITS, p_e);
        pack_bits(e_sc, E_BITS, p_esc);
        pack_bits(f_bits, E_BITS, p_f);

        p_meta[0] = qm;
        p_meta[1] = rv;
        p_meta[2] = k0;
        p_meta[3] = nsym;
        p_meta[4] = (crc24a(tb_bits, A_TB) == crc) ? 1u : 0u;
        p_meta[5] = syn_all;
        p_meta[6] = FILLER_N;
        p_meta[7] = 0;
        (void)m;
    }

    res[0] = TD_MAGIC;
    res[1] = nvar;
    res[2] = errors;
    res[3] = mtime() - t0;
    res[4] = 0;                             /* array runs: this bench uses none */
    res[5] = A_TB;
    res[6] = K_BITS;
    res[7] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    mmio_w(EXIT_ADDR, 0x600Du);
    for (;;) ;
}
