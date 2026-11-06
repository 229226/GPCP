/* -----------------------------------------------------------------------------
 * 2x2 MIMO channel estimation with FDM-orthogonal DMRS (T4b-1).
 *
 *   TD block 0 (Rx antenna 0), TD block 1 (Rx antenna 1), one symbol each
 *     -> one 4096-point FFT per antenna
 *     -> 60 pilot REs (every 4th); even pilot index -> layer 0, odd -> layer 1
 *     -> per (antenna, layer) LS average over that layer's 30 pilots
 *     -> four H_SCALE-scaled coefficients
 *
 * Result area (0x8007_0000):
 *   [0] 0xCAFEBABE  [1] errors  [2] cycles  [3] 0  [4] 0  [5] 0  [6] 0x600D
 *   [7] 0
 *   [8..15] h[0][0], h[0][1], h[1][0], h[1][1] as (re, im) pairs
 *   [16] pilots per layer   [17] c_init layer 0   [18] c_init layer 1
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)
#define TD_RAM        0x80400000ul

#define NPES          16
#define RING_DEPTH    1024
#define DESC_BASE     0x80020000ul

#define NFFT          4096
#define CP_LEN        288
#define BIN0          (NFFT / 2 + 1)
#define SYM_WORDS     (2 * (CP_LEN + NFFT))
#define NRE           240
#define NPILOT        60
#define NP_LAYER      (NPILOT / 2)
#define QPSK          2000
#define H_SCALE       16
#define FRONT_GAIN    4
#define GOLD_NC       1600
#define GOLD_LEN      (2 * NP_LAYER + GOLD_NC + 31)

#define CSI_CINIT0    0xC01u
#define CSI_CINIT1    0xC02u

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, dmas;
static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;
static uint32_t gold_x1[GOLD_LEN], gold_x2[GOLD_LEN];

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

static void gold_init(uint32_t c_init)
{
    for (uint32_t i = 0; i < GOLD_LEN; i++) { gold_x1[i] = 0; gold_x2[i] = 0; }
    gold_x1[0] = 1;
    for (uint32_t i = 0; i < 31u; i++) gold_x2[i] = (c_init >> i) & 1u;
    for (uint32_t i = 0; i + 31u < GOLD_LEN; i++) {
        gold_x1[i + 31] = gold_x1[i + 3] ^ gold_x1[i];
        gold_x2[i + 31] = gold_x2[i + 3] ^ gold_x2[i + 2]
                        ^ gold_x2[i + 1] ^ gold_x2[i];
    }
}

static uint32_t gold_bit(uint32_t n)
{
    return gold_x1[GOLD_NC + n] ^ gold_x2[GOLD_NC + n];
}

#include "ssb_fft.inc"          /* fft4096() + rev4() */

int main(void)
{
    uint32_t *out = RESULT_BASE;
    int32_t *td = (int32_t *)TD_RAM;
    uint32_t t0 = mtime();

    static int32_t pre[2][NP_LAYER], pim[2][NP_LAYER];
    const uint32_t cinits[2] = { CSI_CINIT0, CSI_CINIT1 };
    for (int l = 0; l < 2; l++) {
        gold_init(cinits[l]);
        for (int i = 0; i < NP_LAYER; i++) {
            int32_t c0 = (int32_t)gold_bit((uint32_t)(2 * i));
            int32_t c1 = (int32_t)gold_bit((uint32_t)(2 * i + 1));
            pre[l][i] = (1 - 2 * c0) * QPSK;
            pim[l][i] = (1 - 2 * c1) * QPSK;
        }
    }

    const int64_t den = (int64_t)NP_LAYER * 2 * QPSK * QPSK / H_SCALE;
    int32_t hre[2][2], him[2][2];

    for (int rx = 0; rx < 2; rx++) {
        int32_t *sp = td + (size_t)rx * SYM_WORDS + 2 * CP_LEN;
        fft4096(sp);

        int64_t accr[2] = { 0, 0 }, acci[2] = { 0, 0 };
        int idx = 0;
        for (int r = 0; r < NRE; r += 4) {
            uint32_t ip = rev4(BIN0 + (uint32_t)r);
            int64_t yr = (int64_t)sp[2 * ip] << FRONT_GAIN;
            int64_t yi = (int64_t)sp[2 * ip + 1] << FRONT_GAIN;
            int l = idx & 1;
            int p = idx >> 1;
            int32_t dr = pre[l][p], di = pim[l][p];
            accr[l] += yr * dr + yi * di;
            acci[l] += yi * dr - yr * di;
            idx++;
        }
        for (int l = 0; l < 2; l++) {
            hre[rx][l] = (int32_t)(accr[l] / den);
            him[rx][l] = (int32_t)(acci[l] / den);
        }
    }

    out[0] = 0xCAFEBABEu;
    out[1] = errs;
    out[2] = mtime() - t0;
    out[3] = 0;
    out[4] = 0;
    out[5] = 0;
    out[6] = 0x600Du;
    out[7] = 0;
    for (int rx = 0; rx < 2; rx++)
        for (int l = 0; l < 2; l++) {
            out[8 + 4 * rx + 2 * l] = (uint32_t)hre[rx][l];
            out[9 + 4 * rx + 2 * l] = (uint32_t)him[rx][l];
        }
    out[16] = (uint32_t)NP_LAYER;
    out[17] = CSI_CINIT0;
    out[18] = CSI_CINIT1;

    *(volatile uint32_t *)EXIT_ADDR = 1u;
    return 0;
}
