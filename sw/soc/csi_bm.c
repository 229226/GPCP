/* -----------------------------------------------------------------------------
 * CSI-RS (38.211 7.4.1.5) + wideband CQI report on the SoC.
 *
 *   1 OFDM symbol -> 4096-point FFT (ssb_fft.inc) -> 240-RE window
 *     -> 80 CSI-RS REs (density 3) -> LS estimate A[i] = Y[r_i]*conj(r_i)/|s|^2
 *     -> Ps = |mean(A)|^2, Pn = sum|A[i]-A[i-1]|^2 / (2(N-1))
 *     -> snr_q8 = Ps*256/Pn -> CQI by 38.214 threshold table (no log needed)
 *
 * The adjacent-difference noise estimate is blind to a uniform front-end gain,
 * so this gate stays tight even though the absolute FFT scale is not.
 *
 * Result area (0x8007_0000, 32-bit words):
 *   [0] 0xCAFEBABE  [1] errors  [2] cycles  [3] array runs  [4] DMAs
 *   [5] CQI         [6] 0x600D  [7] snr_q8   [8] Ps>>40      [9] Pn>>10
 *   [10..17] A[0..3] (re,im)    [18] NCSI     [19] c_init
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80070000ul)
#define TD_RAM        0x80400000ul

/* ---- ssb_fft.inc prerequisites ---- */
#define NPES          16
#define RING_DEPTH    1024
#define DESC_BASE     0x80020000ul

#define NFFT          4096
#define CP_LEN        288
#define BIN0          (NFFT / 2 + 1)
#define SYM_WORDS     (2 * (CP_LEN + NFFT))
#define NRE           240
#define DENSITY       3
#define NCSI          (NRE / DENSITY)
#define QPSK          2000
#define CSI_NID       1
#define CSI_NSLOT     0
#define CSI_L         0
#define N_SYMB_SLOT   14
#define GOLD_NC       1600
#define GOLD_LEN      (2 * NCSI + GOLD_NC + 31)
#define FRONT_GAIN    4          /* sp << 4, same as the other CSIs */

#include "csi_cqi_tab.h"

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, dmas;
static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}
static uint32_t gold_x1[GOLD_LEN], gold_x2[GOLD_LEN];

/* ------------------------------------------------------------ 38.211 Gold */
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

static uint32_t csi_cinit(void)
{
    uint64_t v = ((uint64_t)1 << 10)
                 * (uint64_t)(N_SYMB_SLOT * CSI_NSLOT + CSI_L + 1)
                 * (uint64_t)(2 * CSI_NID + 1) + (uint64_t)CSI_NID;
    return (uint32_t)(v & 0x7FFFFFFFu);
}

#include "ssb_fft.inc"          /* fft4096() + rev4() */

int main(void)
{
    uint32_t *out = RESULT_BASE;
    int32_t *td = (int32_t *)TD_RAM;
    uint32_t t0 = mtime();
    const uint32_t c_init = csi_cinit();

    gold_init(c_init);

    /* ---- 4096-point FFT of the single CSI-RS symbol ---- */
    int32_t *sp = td + 2 * CP_LEN;
    fft4096(sp);                    /* X[k] at sp[rev4(k)] */

    /* ---- LS estimate on the 80 CSI-RS REs (density 3) ---- */
    int64_t ar[NCSI], ai[NCSI];
    for (int i = 0; i < NCSI; i++) {
        uint32_t idx = rev4(BIN0 + (uint32_t)(i * DENSITY));
        int64_t xr = (int64_t)sp[2 * idx] << FRONT_GAIN;
        int64_t xi = (int64_t)sp[2 * idx + 1] << FRONT_GAIN;
        int32_t c0 = (int32_t)gold_bit((uint32_t)(2 * i));
        int32_t c1 = (int32_t)gold_bit((uint32_t)(2 * i + 1));
        int64_t dr = (1 - 2 * c0) * QPSK;
        int64_t di = (1 - 2 * c1) * QPSK;
        /* Keep the raw scale: dividing by |r|^2 here would quantise the
           noise difference to 0/1 and destroy the estimate. */
        ar[i] = xr * dr + xi * di;             /* X * conj(r) */
        ai[i] = xi * dr - xr * di;
    }

    int64_t sumr = 0, sumi = 0;
    for (int i = 0; i < NCSI; i++) { sumr += ar[i]; sumi += ai[i]; }
    int64_t mr = sumr / NCSI, mi = sumi / NCSI;
    int64_t ps = mr * mr + mi * mi;

    int64_t acc = 0;
    for (int i = 1; i < NCSI; i++) {
        int64_t dr = ar[i] - ar[i - 1];
        int64_t di = ai[i] - ai[i - 1];
        acc += dr * dr + di * di;
    }
    int64_t pn = acc / (2 * (NCSI - 1));
    if (pn <= 0) pn = 1;
    /* Q8 ratio without overflowing: ps ~ 1e16, ps<<8 does not fit int64. */
    int64_t ratio = ps / pn;
    int64_t frac = (ps % pn) * 256 / pn;
    int32_t snr_q8 = (int32_t)(ratio * 256 + frac);

    int32_t cqi = 0;
    for (int c = 1; c <= 15; c++)
        if (snr_q8 >= csi_cqi_thr_q8[c]) cqi = (int32_t)c;

    out[0] = 0xCAFEBABEu;
    out[1] = errs;
    out[2] = mtime() - t0;
    out[3] = 0;
    out[4] = 0;
    out[5] = (uint32_t)cqi;
    out[6] = 0x600Du;
    out[7] = (uint32_t)snr_q8;
    out[8] = (uint32_t)(ps >> 40);
    out[9] = (uint32_t)(pn >> 10);
    for (int i = 0; i < 4; i++) {
        out[10 + 2 * i] = (uint32_t)(int32_t)ar[i];
        out[11 + 2 * i] = (uint32_t)(int32_t)ai[i];
    }
    out[18] = (uint32_t)NCSI;
    out[19] = c_init;

    *(volatile uint32_t *)EXIT_ADDR = 1u;
    return 0;
}
