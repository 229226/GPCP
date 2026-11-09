/* -----------------------------------------------------------------------------
 * gNB transmit chain, step 2: PBCH payload assembly on the CVA6 (G1 / A2).
 *
 * 38.212 7.1.1 + 5.4.1, 38.211 7.3.2 / 7.3.3:
 *
 *   MIB 32 bits -> [mode 1: + CRC24C] -> frozen insertion into u[512]
 *     -> polar transform d[512] (the exact butterfly A1 runs on the CGRA)
 *     -> rate matching e[720] (sub-block interleave + circular buffer +
 *        triangular interleave) -> Gold scrambling c_init = N_ID^cell
 *     -> 360 QPSK symbols at amplitude 2000.
 *
 * Two modes share one image, selected by the TD:
 *   mode 0 : K=32, no CRC   -- matches ssb_plan.py / ssb_plan_p1.py, so the
 *            A4 self-loop can decode it with the existing receive chain.
 *   mode 1 : K=56, CRC24C   -- full 38.212 7.1.1.
 * E=720 (2*(240-28)*2 PBCH data REs), NOT 864: that is the hardware receive
 * side's rate-recovery length.  QPSK amplitude 2000 matches ssb_plan_p1.QPSK.
 *
 * This benchmark touches NO CGRA kernel, NO IMEM, NO DMA: everything is scalar
 * bit work on CVA6, which is exactly where the spec puts it.  The golden model
 * is tools/nr_pbch.py::pbch_pack(), written independently from this file.
 *
 *   TD (0x8040_0000): [0] "PBCH" [1] mode [2] payload32 [3] nid_cell
 *                     [4] E [5] QPSK amplitude [6..7] reserved
 *   results 0x8001_0000 (32-bit words):
 *     [0] 0xCAFEBABE  [1] mode  [2] K  [3] E  [4] errors  [5] cycles
 *     [6] 0x600D      [7] payload32  [8] CRC24C  [9] nid_cell
 *     [10] amplitude  [11] array runs (always 0 here)
 *     [16..17]  info bits (K bits, LSB-first packed)
 *     [18..33]  u[512]      [34..49]  d[512]
 *     [50..72]  e[720]      [73..95]  e_sc[720]
 *     [96..815] 360 QPSK symbols, re then im, int32 (+-2000)
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"

#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)
#define TD_BASE          0x80400000ul
#define PACK_MAGIC       0x50424348u

#define NPOL             512
#define NW               (NPOL / 32)
#define E_MAX            720
#define AMP_DEF          2000
#define CRC_LEN          24
#define CRC_POLY         0xB2B117u
#define NC               1600
#define DEPTH            9
#include "gold_seq.inc"      /* 38.211 5.2.1 Gold sequence */

static const uint8_t SUBBLK_PERM[32] = {
    0, 1, 2, 4, 3, 5, 6, 7, 8, 9, 10, 12, 11, 13, 14, 15,
    16, 17, 18, 20, 19, 21, 22, 23, 24, 25, 26, 28, 27, 29, 30, 31
};

static uint8_t info[64], u_bits[NPOL], d_bits[NPOL];
static uint8_t e_bits[E_MAX], e_sc[E_MAX];
static uint8_t y_bits[NPOL];
static uint8_t scr[E_MAX];

static uint32_t errs;

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t bitrev9(uint32_t i)
{
    uint32_t r = 0, b;
    for (b = 0; b < DEPTH; b++)
        r |= ((i >> b) & 1u) << (DEPTH - 1u - b);
    return r;
}

/* 38.212 5.1 gCRC24C, MSB-first, 24-bit register. */
static uint32_t crc24c(const uint8_t *bits, uint32_t n)
{
    uint32_t reg = 0, i, k;
    const uint32_t mask = (1u << CRC_LEN) - 1u;
    for (i = 0; i < n; i++) {
        reg ^= (uint32_t)(bits[i] & 1u) << (CRC_LEN - 1u);
        for (k = 0; k < CRC_LEN; k++)
            reg = (reg & (1u << (CRC_LEN - 1u)))
                  ? ((reg << 1) ^ CRC_POLY) & mask
                  : (reg << 1) & mask;
    }
    return reg & mask;
}

static void pack_out(uint32_t *dst, const uint8_t *bits, uint32_t n)
{
    uint32_t i;
    for (i = 0; i < (n + 31u) / 32u; i++)
        dst[i] = 0;
    for (i = 0; i < n; i++)
        dst[i / 32u] |= (uint32_t)(bits[i] & 1u) << (i % 32u);
}

int main(void)
{
    uint32_t *out = RESULT_BASE;
    const uint32_t *td = (const uint32_t *)TD_BASE;
    uint32_t mode, payload, nid, E, amp, K, i, j, k, s, t, idx;
    uint32_t t0, crc;

    if (td[0] != PACK_MAGIC || td[4] != E_MAX || td[5] != AMP_DEF) {
        out[0] = td[0];
        out[4] = 0xDEADu;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 1;
    }
    mode = td[1];
    payload = td[2];
    nid = td[3];
    E = td[4];
    amp = td[5];
    if (mode > 1u) {
        out[4] = 0xDEADu;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 1;
    }
    K = mode ? 56u : 32u;

    t0 = mtime();

    /* ---- MIB payload (MSB-first in the 32-bit word) + optional CRC24C ---- */
    for (i = 0; i < 32u; i++)
        info[i] = (uint8_t)((payload >> (31u - i)) & 1u);
    crc = mode ? crc24c(info, 32u) : 0u;
    if (mode)
        for (i = 0; i < CRC_LEN; i++)
            info[32u + i] = (uint8_t)((crc >> (CRC_LEN - 1u - i)) & 1u);

    /* ---- frozen insertion: info indices are {i : bitrev9(i) >= N-K} ---- */
    for (i = 0; i < NPOL; i++)
        u_bits[i] = 0;
    idx = 0;
    for (i = 0; i < NPOL && idx < K; i++)
        if (bitrev9(i) >= NPOL - K)
            u_bits[i] = info[idx++];
    if (idx != K)
        errs++;

    /* ---- polar transform N=512 (the A1 butterfly, done scalar) ---- */
    for (i = 0; i < NPOL; i++)
        d_bits[i] = u_bits[i];
    for (s = 1u; s < NPOL; s *= 2u)
        for (i = 0; i < NPOL; i += 2u * s)
            for (j = 0; j < s; j++)
                d_bits[i + j] ^= d_bits[i + j + s];

    /* ---- 38.212 5.4.1 rate matching: sub-block interleave ---- */
    idx = 0;
    for (j = 0; j < 32u; j++)
        for (i = 0; i < NPOL / 32u; i++)
            y_bits[idx++] = d_bits[i * 32u + SUBBLK_PERM[j]];

    /* ---- 38.212 5.4.1.2 circular buffer (repetition, E > N) then the
     *      5.4.1.3 triangular interleave: cells are row-major lower-triangle,
     *      read column-major.  cell (i,j) has row-major index i*(i+1)/2+j. ---- */
    t = 1;
    while (t * (t + 1u) / 2u < E)
        t++;
    {
        uint8_t z[E_MAX];
        for (k = 0; k < E; k++)
            z[k] = y_bits[k % NPOL];
        idx = 0;
        for (j = 0; j < t; j++)
            for (i = j; i < t; i++) {
                k = i * (i + 1u) / 2u + j;
                if (k < E)
                    e_bits[idx++] = z[k];
            }
    }
    if (idx != E)
        errs++;

    /* ---- Gold scrambling (c_init = N_ID^cell) ---- */
    gold_seq(nid, E, scr);
    for (i = 0; i < E; i++)
        e_sc[i] = (uint8_t)(e_bits[i] ^ scr[i]);

    /* ---- results ---- */
    out[0] = 0xCAFEBABEu;
    out[1] = mode;
    out[2] = K;
    out[3] = E;
    out[4] = errs;
    out[5] = mtime() - t0;
    out[6] = 0x600Du;
    out[7] = payload;
    out[8] = crc;
    out[9] = nid;
    out[10] = amp;
    out[11] = 0;                     /* array runs: none, pure CVA6 */
    pack_out(&out[16], info, K);     /* 2 words (K <= 64) */
    pack_out(&out[18], u_bits, NPOL);
    pack_out(&out[34], d_bits, NPOL);
    pack_out(&out[50], e_bits, E);
    pack_out(&out[73], e_sc, E);
    for (i = 0; i < E / 2u; i++) {
        out[96 + 2u * i + 0] = (uint32_t)(int32_t)
            ((1 - 2 * (int32_t)e_sc[2u * i]) * (int32_t)amp);
        out[96 + 2u * i + 1] = (uint32_t)(int32_t)
            ((1 - 2 * (int32_t)e_sc[2u * i + 1u]) * (int32_t)amp);
    }

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
