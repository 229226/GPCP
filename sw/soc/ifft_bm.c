/* -----------------------------------------------------------------------------
 * gNB transmit chain, step 3 (G1/A3): OFDM IFFT + cyclic prefix.
 *
 * Zero new microcode: the hardware FFT is Q15 radix-4 DIF with a 1/4 scale per
 * stage, so for N=4096 its output is DFT(a)/4096 in base-4 digit-reversed
 * order.  Hence
 *
 *     IDFT(X)[n] = conj( FFT_hw(conj(X))[rev4(n)] )
 *
 * The conjugate input is prepared by CVA6 (the model's dump-ifft TD), the two
 * generated images fft_stage_prog.h / fft_block_prog.h are reused unchanged,
 * and CVA6 takes the conjugate of the result, undoes rev4, applies TD_SCALE
 * and prepends the CP.  No kernel, no RTL, no compiler change.
 *
 *   TD (0x8040_0000): magic, N, CP, TD_SCALE, sym, QPSK, -, -, then N conj bins
 *   work (0x8030_0000): in-place FFT scratch (2*N int32 words)
 *   LUT (0x8010_0000): tools/fft_plan.py lut, same as run-fft / run-ssbfft
 *   descriptors (0x8004_0000): moved above the result area so the 17k-word
 *     dump cannot overlap the descriptor ring.
 *
 * Result area (0x8001_0000, 32-bit words):
 *   [0 .. 2N-1]                 raw FFT output (digit-reversed, re then im)
 *   [2N .. 2N+2(CP+N)-1]        TX symbol: CP samples followed by N samples
 *   [META+0]  marker 0x1FF7A3B1  [META+1] cycles  [META+2] array runs
 *   [META+3]  errors/timeouts   [META+15] epilogue 0x600D
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"

#define CGRA_BASE    0x50000000ul
#define EXIT_ADDR    0x40010000ul
#define RESULT_BASE  ((uint32_t *)0x80010000ul)
#define INPUT_BASE   0x80400000ul
#define WORK_RAM     0x80300000ul
#define DESC_BASE    0x80040000ul
#define RING_DEPTH   1024
#define NPES         16
#define CP_LEN       288
#define TD_SCALE     256
#define HDR_WORDS    8
#define MAGIC        0x1FF70001ul

static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;
static uint32_t errs, runs;

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

#include "ssb_fft.inc"          /* fft4096(), rev4(), the two generated images */

#define META    (2 * FFT_N + 2 * (CP_LEN + FFT_N))
#define NWORDS  (META + 16)

int main(void)
{
    uint32_t *res = RESULT_BASE;
    const uint32_t *in = (const uint32_t *)INPUT_BASE;
    int32_t *work = (int32_t *)WORK_RAM;
    uint32_t *td;
    uint32_t t0, cyc, i, k;

    if (in[0] != MAGIC || in[1] != FFT_N || in[2] != CP_LEN ||
        in[3] != TD_SCALE) {
        res[0] = 0x0BADBAD0u;
        res[NWORDS - 1] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 0;
    }

    for (i = 0; i < 2 * FFT_N; i++)
        work[i] = (int32_t)in[HDR_WORDS + i];

    t0 = mtime();
    fft4096(work);
    cyc = mtime() - t0;

    /* raw spectrum: the checker compares this with fft_plan.planned_fft() */
    for (i = 0; i < 2 * FFT_N; i++)
        res[i] = (uint32_t)work[i];

    /* CVA6 post-processing: conj + rev4 + TD_SCALE + CP */
    td = res + 2 * FFT_N;
    for (k = 0; k < CP_LEN; k++) {
        uint32_t t = FFT_N - CP_LEN + k;
        uint32_t r = rev4(t);
        td[2 * k]     = (uint32_t)(work[2 * r] * TD_SCALE);
        td[2 * k + 1] = (uint32_t)(-work[2 * r + 1] * TD_SCALE);
    }
    for (k = 0; k < FFT_N; k++) {
        uint32_t r = rev4(k);
        td[2 * (CP_LEN + k)]     = (uint32_t)(work[2 * r] * TD_SCALE);
        td[2 * (CP_LEN + k) + 1] = (uint32_t)(-work[2 * r + 1] * TD_SCALE);
    }

    res[META + 0]  = 0x1FF7A3B1u;
    res[META + 1]  = cyc;
    res[META + 2]  = runs;
    res[META + 3]  = errs;
    res[META + 15] = 0x600Du;

    /* CVA6 keeps stores in a write-through buffer: without the fence the last
     * store (the epilogue) is still in flight when EXIT_ADDR ends the sim. */
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
