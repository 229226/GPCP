/* -----------------------------------------------------------------------------
 * G2 / B3: PDCCH transmit chain (38.212 7.2 + 38.211 7.3.2).
 *
 *   DCI(32) -> CRC24C -> RNTI mask -> frozen insertion -> polar transform
 *          -> rate matching E=432 -> scrambling -> QPSK -> REG/CCE mapping
 *
 * The polar transform is the one stage that belongs on the array, and it is
 * A1's nine compiler-generated kernels, unchanged: five in-word stages
 * (s = 1,2,4,8,16 inside a 32-bit word) and four word stages (d = 1,2,4,8
 * words).  512 bits = 16 words, one word per PE.
 *
 * Everything else stays scalar on CVA6 for the same reason B1/B2 do: the CRC,
 * the frozen-set walk and the rate matcher are bit work with little spatial
 * parallelism, and one array run costs ~1456 cycles of DMA/orchestration (T0).
 *
 *   TD (0x8040_0000):
 *     [0] magic 0x1DCC0001  [1] nvar  [2] n_id  [3] dci (packed, MSB first)
 *     [4] n_reg  [5] L  [6] n_shift  [7] -
 *     [8 + 2*v] variant v: {rnti, -}
 *   results 0x8001_0000:
 *     [0] marker [1] nvar [2] errors [3] cycles [4] array runs
 *     [5] dci [6] n_id [7] n_reg [8] L [9] n_shift [10] 0x600D [11] crc24c
 *     blocks at 16 + v*BLK, each BLK words:
 *       info[2] u[16] x[16] e[14] e_sc[14] sym[432] reg[432] meta[8]
 *     meta = {rnti, info_used, nbundle, nshift, 0, 0, 0, 0}
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"
#include "gold_seq.inc"

#include "polar_inword_s0_prog.h"
#include "polar_inword_s1_prog.h"
#include "polar_inword_s2_prog.h"
#include "polar_inword_s3_prog.h"
#include "polar_inword_s4_prog.h"
#include "polar_word_d1_prog.h"
#include "polar_word_d2_prog.h"
#include "polar_word_d4_prog.h"
#include "polar_word_d8_prog.h"

#define CGRA_BASE    0x50000000ul
#define EXIT_ADDR    0x40010000ul
#define RESULT_BASE  ((uint32_t *)0x80010000ul)
#define TD_BASE      0x80400000ul
#define TD_MAGIC     0x1DCC0001ul

#define A_DCI        32
#define CRC_LEN      24
#define K_INFO       (A_DCI + CRC_LEN)      /* 56 */
#define NPOL         512
#define NW           (NPOL / 32)            /* 16 */
#define DEPTH        9
#define N_FROZEN     (NPOL - K_INFO)        /* 456 */
#define E_BITS       432
#define QAM          2000
#define NC           1600u
#define NPES         16
#define MAX_VAR      2
#define MAX_SYM      (E_BITS / 2)           /* 216 */
#define TRI_T        29                     /* smallest T with T(T+1)/2 >= 432 */

#define MASK_WORD    (GPCP_LAYOUT_REGION_CONST * GPCP_LAYOUT_WORDS)
#define WMASK_WORD   (GPCP_LAYOUT_REGION_DATA * GPCP_LAYOUT_WORDS)

#define INFO_W       ((K_INFO + 31) / 32)   /* 2 */
#define E_W          ((E_BITS + 31) / 32)   /* 14 */
#define SYM_W        (2 * MAX_SYM)          /* 432 */
#define BLK          (INFO_W + NW + NW + E_W + E_W + SYM_W + SYM_W + 8)
#define HDR          16

static const uint8_t SUBBLK_PERM[32] = {
    0, 1, 2, 4, 3, 5, 6, 7, 8, 9, 10, 12, 11, 13, 14, 15,
    16, 17, 18, 20, 19, 21, 22, 23, 24, 25, 26, 28, 27, 29, 30, 31
};

static uint8_t dci_bits[A_DCI];
static uint8_t info[K_INFO];
static uint8_t u_bits[NPOL], x_bits[NPOL];
static uint8_t y_bits[NPOL], z_bits[E_BITS], e_bits[E_BITS];
static uint8_t e_sc[E_BITS], scr[E_BITS];

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }
static uint32_t errs, runs;

static void pack_bits(const uint8_t *b, uint32_t n, uint32_t *out)
{
    uint32_t i, w = (n + 31u) / 32u;
    for (i = 0; i < w; i++)
        out[i] = 0;
    for (i = 0; i < n; i++)
        if (b[i])
            out[i >> 5] |= 1u << (i & 31u);
}

static uint32_t pack32(const uint8_t *b)
{
    uint32_t w = 0, i;
    for (i = 0; i < 32u; i++)
        if (b[i])
            w |= 1u << i;
    return w;
}

static uint32_t bitrev9(uint32_t v)
{
    uint32_t r = 0, b;
    for (b = 0; b < DEPTH; b++)
        r |= ((v >> b) & 1u) << (DEPTH - 1 - b);
    return r;
}

static uint32_t is_frozen(uint32_t i) { return bitrev9(i) < N_FROZEN; }

/* 38.212 7.2 gCRC24C, MSB-first. */
static uint32_t crc24c(const uint8_t *bits, uint32_t n)
{
    uint32_t reg = 0, i, j;
    for (i = 0; i < n; i++) {
        reg ^= (uint32_t)(bits[i] & 1u) << 23;
        for (j = 0; j < 24u; j++)
            reg = (reg & 0x800000u) ? (((reg << 1) ^ 0xB2B117u) & 0xFFFFFFu)
                                    : ((reg << 1) & 0xFFFFFFu);
    }
    return reg;
}

static void load_and_run(const unsigned int *prog, uint32_t cycles)
{
    if (gpcp_cgra_imem_load(CGRA_BASE, prog, cycles * NPES) != 0)
        errs++;
    if (rt_array_run(CGRA_BASE, 0, cycles) != 0)
        errs++;
    runs++;
}

/* mask for the in-word stage of size s: bit p is kept iff (p & s) == 0 */
static uint32_t polar_mask(uint32_t s)
{
    uint32_t m = 0, p;
    for (p = 0; p < 32u; p++)
        if ((p & s) == 0)
            m |= 1u << p;
    return m;
}

/* A1's nine stages, run in place on spad words 0..15. */
static void polar_transform(void)
{
    uint32_t i, w;

    for (i = 0; i < NW; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(i),
                        pack32(u_bits + 32 * i));

    for (i = 0; i < 5; i++) {
        uint32_t s = 1u << i;
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(MASK_WORD),
                        polar_mask(s));
        switch (i) {
        case 0: load_and_run(polar_inword_s0_prog, POLAR_INWORD_S0_PROG_CYCLES); break;
        case 1: load_and_run(polar_inword_s1_prog, POLAR_INWORD_S1_PROG_CYCLES); break;
        case 2: load_and_run(polar_inword_s2_prog, POLAR_INWORD_S2_PROG_CYCLES); break;
        case 3: load_and_run(polar_inword_s3_prog, POLAR_INWORD_S3_PROG_CYCLES); break;
        default: load_and_run(polar_inword_s4_prog, POLAR_INWORD_S4_PROG_CYCLES); break;
        }
    }
    for (i = 0; i < 4; i++) {
        uint32_t d = 1u << i;
        for (w = 0; w < NW; w++)
            gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(WMASK_WORD + w),
                            (w & d) ? 0u : 0xFFFFFFFFu);
        switch (i) {
        case 0: load_and_run(polar_word_d1_prog, POLAR_WORD_D1_PROG_CYCLES); break;
        case 1: load_and_run(polar_word_d2_prog, POLAR_WORD_D2_PROG_CYCLES); break;
        case 2: load_and_run(polar_word_d4_prog, POLAR_WORD_D4_PROG_CYCLES); break;
        default: load_and_run(polar_word_d8_prog, POLAR_WORD_D8_PROG_CYCLES); break;
        }
    }

    for (i = 0; i < NW; i++) {
        uint32_t v = gpcp_cgra_read(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(i));
        uint32_t b;
        for (b = 0; b < 32u; b++)
            x_bits[32 * i + b] = (uint8_t)((v >> b) & 1u);
    }
}

/* 38.212 5.4.1.1 sub-block interleaving: 32 columns, SUBBLK_PERM column order. */
static void subblock_interleave(uint32_t n, const uint8_t *d, uint8_t *out)
{
    uint32_t rows = n / 32u, p, r, k = 0;
    for (p = 0; p < 32u; p++)
        for (r = 0; r < rows; r++)
            out[k++] = d[r * 32u + SUBBLK_PERM[p]];
}

/* 38.212 5.4.1.3 triangular interleaver: fill the lower triangle row-major
 * (truncated to E cells), read it column-major.  T = 29 covers E = 432
 * (29*30/2 = 435 cells); cell (i,j) exists iff i*(i+1)/2 + j < E. */
static void triangular_interleave(uint32_t n, const uint8_t *in, uint8_t *out)
{
    static uint8_t m[TRI_T][TRI_T];
    uint32_t i, j, k = 0;

    for (i = 0; i < TRI_T && k < n; i++)
        for (j = 0; j <= i && k < n; j++)
            m[i][j] = in[k++];
    k = 0;
    for (j = 0; j < TRI_T; j++)
        for (i = j; i < TRI_T; i++)
            if (i * (i + 1u) / 2u + j < n)
                out[k++] = m[i][j];
}

/* QPSK level for one bit: 38.211 7.3.1.2 with a = 2000, bit 0 -> +. */
static int32_t qpsk(int32_t b) { return (1 - 2 * b) * QAM; }

int main(void)
{
    uint32_t *out = RESULT_BASE;
    const uint32_t *td = (const uint32_t *)TD_BASE;
    uint32_t nvar, n_id, dci, n_reg, L, n_shift, i, v, t0;
    uint32_t crc;

    if (td[0] != TD_MAGIC) {
        out[0] = 0x0BADC001u;
        out[10] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        mmio_w(EXIT_ADDR, 0x600Du);
        for (;;) ;
    }

    nvar = td[1];
    n_id = td[2];
    dci = td[3];
    n_reg = td[4];
    L = td[5];
    n_shift = td[6];
    if (nvar == 0 || nvar > MAX_VAR || n_reg != MAX_SYM ||
        L == 0 || (n_reg / L) % 2u != 0) {
        out[0] = 0x0BADC002u;
        out[10] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        mmio_w(EXIT_ADDR, 0x600Du);
        for (;;) ;
    }

    t0 = mtime();

    for (i = 0; i < A_DCI; i++)
        dci_bits[i] = (uint8_t)((dci >> (31u - i)) & 1u);
    crc = crc24c(dci_bits, A_DCI);

    for (v = 0; v < nvar; v++) {
        uint32_t rnti = td[8 + 2 * v];
        uint32_t *blk = out + HDR + v * BLK;
        uint32_t *p_info = blk;
        uint32_t *p_u = blk + INFO_W;
        uint32_t *p_x = p_u + NW;
        uint32_t *p_e = p_x + NW;
        uint32_t *p_esc = p_e + E_W;
        uint32_t *p_sym = p_esc + E_W;
        uint32_t *p_reg = p_sym + SYM_W;
        uint32_t *p_meta = p_reg + SYM_W;
        uint32_t k, nb, half, n_info = 0;

        /* 1. DCI + CRC24C masked by the RNTI (38.212 7.2) */
        for (i = 0; i < A_DCI; i++)
            info[i] = dci_bits[i];
        for (i = 0; i < CRC_LEN; i++)
            info[A_DCI + i] = (uint8_t)(((crc >> (23u - i)) & 1u) ^
                                        ((rnti >> i) & 1u));

        /* 2. frozen insertion: index i frozen iff bitrev9(i) < N-K */
        for (i = 0; i < NPOL; i++)
            u_bits[i] = 0;
        for (i = 0; i < NPOL; i++)
            if (!is_frozen(i) && n_info < K_INFO)
                u_bits[i] = info[n_info++];

        /* 3. polar transform on the array (A1's nine kernels) */
        polar_transform();

        /* 4. rate matching: sub-block interleave -> circular buffer -> triangle */
        subblock_interleave(NPOL, x_bits, y_bits);
        for (k = 0; k < E_BITS; k++)
            z_bits[k] = y_bits[k % NPOL];
        triangular_interleave(E_BITS, z_bits, e_bits);

        /* 5. scrambling, 38.211 7.3.2.3: c_init = n_ID.  The RNTI only masks
         *    the CRC, which is what makes blind detection one decode + a cheap
         *    CRC check per candidate instead of one decode per candidate. */
        gold_seq(n_id, E_BITS, scr);
        for (k = 0; k < E_BITS; k++)
            e_sc[k] = (uint8_t)(e_bits[k] ^ scr[k]);

        /* 6. QPSK + 7. REG/CCE mapping (38.211 7.3.2.3 interleaved form) */
        nb = n_reg / L;
        half = nb / 2u;
        for (k = 0; k < MAX_SYM; k++) {
            p_sym[2 * k] = (uint32_t)qpsk(e_sc[2 * k]);
            p_sym[2 * k + 1] = (uint32_t)qpsk(e_sc[2 * k + 1]);
        }
        for (k = 0; k < nb; k++) {
            uint32_t c = k / 2u, r = k % 2u;
            uint32_t fj = (r * half + c + n_shift) % nb;
            uint32_t j;
            for (j = 0; j < L; j++) {
                p_reg[2 * (fj * L + j)] = (uint32_t)qpsk(e_sc[2 * (k * L + j)]);
                p_reg[2 * (fj * L + j) + 1] =
                    (uint32_t)qpsk(e_sc[2 * (k * L + j) + 1]);
            }
        }

        pack_bits(info, K_INFO, p_info);
        pack_bits(u_bits, NPOL, p_u);
        pack_bits(x_bits, NPOL, p_x);
        pack_bits(e_bits, E_BITS, p_e);
        pack_bits(e_sc, E_BITS, p_esc);

        p_meta[0] = rnti;
        p_meta[1] = n_info;
        p_meta[2] = nb;
        p_meta[3] = n_shift;
        p_meta[4] = 0;
        p_meta[5] = 0;
        p_meta[6] = 0;
        p_meta[7] = 0;
    }

    out[0] = TD_MAGIC;
    out[1] = nvar;
    out[2] = errs;
    out[3] = mtime() - t0;
    out[4] = runs;
    out[5] = dci;
    out[6] = n_id;
    out[7] = n_reg;
    out[8] = L;
    out[9] = n_shift;
    out[10] = 0x600Du;
    out[11] = crc;
    out[12] = 0;
    out[13] = 0;
    out[14] = 0;
    out[15] = 0;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    mmio_w(EXIT_ADDR, 0x600Du);
    for (;;) ;
}
