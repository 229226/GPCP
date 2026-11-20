/* -----------------------------------------------------------------------------
 * gNB transmit chain, step 1: the PBCH polar transform (N = 512) on the CGRA.
 *
 * 38.212 7.1.1: the PBCH payload becomes a 512-bit polar codeword by the
 * butterfly x[i] ^= x[i + s] for every (i & s) == 0, s = 1, 2, 4, ... 256.
 * That is exactly the nine stage kernels cgra/kernels/polar_inword_s*.yaml
 * (s = 1..16 inside a 32-bit word) and polar_word_d*.yaml (d = 1..8 words);
 * the compiler generated them long ago but they had never run on hardware.
 *
 * Storage: 512 bits = 16 words of 32 bits, one word per PE (bit p of word w is
 * codeword bit 32*w + p), so a stage is one array run over 16 PEs:
 *   in-word : w ^= (w >> s) & mask    mask preloaded at flat word 32 (broadcast)
 *   word    : w[i] ^= w[i + d]        in place
 *
 * Payload assembly (CRC24, frozen positions, the 38.212 5.4.1 rate matcher onto
 * E = 864), scrambling and QPSK stay on CVA6 for now: this gate owns only the
 * transform, which is the part that needs the array.
 *
 *   TD (0x8040_0000): magic, nbits, -, -, -, -, -, -, then nbits/32 words of u
 *   results 0x8001_0000: [0] marker [1] stages [2] array runs [3] errors
 *                        [4] 0x600D, [16..31] the 512 coded bits
 * ----------------------------------------------------------------------------- */
#include "../../include/gpcp_cgra.h"
#include "../../include/soc_io.h"
#include "../../include/gpcp_cgra_rt.h"

#include "polar_inword_s0_prog.h"
#include "polar_inword_s1_prog.h"
#include "polar_inword_s2_prog.h"
#include "polar_inword_s3_prog.h"
#include "polar_inword_s4_prog.h"
#include "polar_word_d1_prog.h"
#include "polar_word_d2_prog.h"
#include "polar_word_d4_prog.h"
#include "polar_word_d8_prog.h"

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)
#define TD_BASE          0x80400000ul
#define TD_MAGIC         0x50424348u   /* "PBCH" */
#define NW               16            /* 16 words x 32 bits = 512 bits */
#define NPES             16
#define MASK_WORD        (GPCP_LAYOUT_REGION_CONST * GPCP_LAYOUT_WORDS)            /* in-word stage mask (broadcast) */
#define WMASK_WORD       (GPCP_LAYOUT_REGION_DATA * GPCP_LAYOUT_WORDS)            /* word-stage mask, one word per PE */

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs;

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
    uint32_t m = 0;
    for (uint32_t p = 0; p < 32; p++)
        if ((p & s) == 0)
            m |= 1u << p;
    return m;
}

int main(void)
{
    uint32_t *out = RESULT_BASE;
    const uint32_t *td = (const uint32_t *)TD_BASE;
    uint32_t i;
    uint32_t t0;

    if (td[0] != TD_MAGIC || td[1] != NW * 32) {
        out[0] = td[0];
        out[3] = 0xDEADu;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 1;
    }

    /* codeword input (u, frozen bits already zeroed by the caller) */
    for (i = 0; i < NW; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(i), td[8 + i]);

    t0 = mtime();
    /* in-word stages: s = 1, 2, 4, 8, 16 */
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
    /* word-to-word stages: d = 1, 2, 4, 8 words (= 32, 64, 128, 256 bits).
     * Only the words with (i & d) == 0 take part, so each stage gets a per-PE
     * mask: 0xFFFFFFFF for those words, 0 for the rest. */
    for (i = 0; i < 4; i++) {
        uint32_t d = 1u << i, w2;
        for (w2 = 0; w2 < NW; w2++)
            gpcp_cgra_write(CGRA_BASE,
                            GPCP_SPAD_FLAT_ADDR(WMASK_WORD + w2),
                            (w2 & d) ? 0u : 0xFFFFFFFFu);
        switch (i) {
        case 0: load_and_run(polar_word_d1_prog, POLAR_WORD_D1_PROG_CYCLES); break;
        case 1: load_and_run(polar_word_d2_prog, POLAR_WORD_D2_PROG_CYCLES); break;
        case 2: load_and_run(polar_word_d4_prog, POLAR_WORD_D4_PROG_CYCLES); break;
        default: load_and_run(polar_word_d8_prog, POLAR_WORD_D8_PROG_CYCLES); break;
        }
    }

    for (i = 0; i < NW; i++)
        out[16 + i] = gpcp_cgra_read(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(i));

    out[0] = 0xCAFEBABEu;
    out[1] = 9;
    out[2] = runs;
    out[3] = errs;
    out[4] = 0x600Du;
    out[5] = mtime() - t0;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
