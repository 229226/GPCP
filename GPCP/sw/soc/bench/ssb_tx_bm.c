/* -----------------------------------------------------------------------------
 * gNB transmit chain, step 4 (G1/A4): full 4-symbol SSB waveform.
 *
 * Extends A3's conjugate IFFT to all four 38.211 SSB OFDM symbols (PSS / PBCH /
 * SSS / PBCH).  Still zero new microcode: the same fft_stage/fft_block images
 * are reloaded by fft4096() for each symbol; CVA6 prepares the conjugate
 * spectra, undoes rev4, applies TD_SCALE and prepends the CP.
 *
 * The result area is the TD file the existing P1 receiver expects
 * (ssb_plan_p1.dump_td_bin): NSYM blocks of (CP_LEN+NFFT) complex int32 pairs.
 * tools/checks/check_ssb_tx.py writes it out and run-ssb-tx then feeds it straight
 * into ssb_fid_bm.bin, closing the hardware loopback (MIB 32/32).
 *
 *   TD (0x8040_0000): magic4, N, CP, TD_SCALE, NSYM, QPSK, -, -,
 *                     then NSYM x N conj bins (int32 re/im)
 *   work (0x8030_0000): in-place FFT scratch
 *   descriptors (0x8004_0000): above the result dump
 *
 * Result area (0x8001_0000, 32-bit words):
 *   [s*SYM_WORDS .. (s+1)*SYM_WORDS-1]  TX symbol s (CP + NFFT complex pairs)
 *   [META+0] marker 0x1FF7A4B1  [META+1] cycles  [META+2] array runs
 *   [META+3] errors/timeouts   [META+15] epilogue 0x600D
 * ----------------------------------------------------------------------------- */
#include "../../include/gpcp_cgra.h"
#include "../../include/soc_io.h"
#include "../../include/gpcp_cgra_rt.h"      /* rt_dcache_flush */

#define CGRA_BASE    0x50000000ul
#define EXIT_ADDR    0x40010000ul
#define RESULT_BASE  ((uint32_t *)0x80040000ul)   /* 结果区 140 KB：挪到环(0x80020000)之上 */
#define INPUT_BASE   0x80400000ul
#include "../../include/gpcp_ring.h"   /* 描述符环基址跟着 .bss 走（不再硬编码） */
#define DESC_BASE     GPCP_DESC_BASE
#define RING_DEPTH   1024
#define NPES         16
#define CP_LEN       288
#define TD_SCALE     256
#define NSYM         4
#define HDR_WORDS    8
#define MAGIC4       0x1FF70004ul

static uint32_t (*desc)[8] = (uint32_t (*)[8])_ring_base;
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

#define SYM_WORDS   (2 * (CP_LEN + FFT_N))
#define META        (NSYM * SYM_WORDS)
#define NWORDS      (META + 16)

int main(void)
{
    uint32_t *res = RESULT_BASE;
    const uint32_t *in = (const uint32_t *)INPUT_BASE;
    int32_t *sym0 = (int32_t *)(INPUT_BASE + HDR_WORDS * 4u);
    uint32_t t0, k, s;

    if (in[0] != MAGIC4 || in[1] != FFT_N || in[2] != CP_LEN ||
        in[3] != TD_SCALE || in[4] != NSYM) {
        res[0] = 0x0BADBAD0u;
        res[NWORDS - 1] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 0;
    }

    t0 = mtime();
    for (s = 0; s < NSYM; s++) {
        /* Each symbol has its own RAM window, --load'ed with the conjugate
         * spectrum and transformed in place.  The CPU never writes these
         * words, so no stale D$ line from a CPU store can shadow the engine's
         * in-place result (AGENTS.md 4.2).  Flush before reading it back. */
        int32_t *buf = sym0 + s * 2 * FFT_N;
        uint32_t *td;

        fft4096(buf);
        rt_dcache_flush();

        /* conj + rev4 + TD_SCALE + CP, straight into the P1 TD layout */
        td = res + s * SYM_WORDS;
        for (k = 0; k < CP_LEN; k++) {
            uint32_t t = FFT_N - CP_LEN + k;
            uint32_t r = rev4(t);
            td[2 * k]     = (uint32_t)(buf[2 * r] * TD_SCALE);
            td[2 * k + 1] = (uint32_t)(-buf[2 * r + 1] * TD_SCALE);
        }
        for (k = 0; k < FFT_N; k++) {
            uint32_t r = rev4(k);
            td[2 * (CP_LEN + k)]     = (uint32_t)(buf[2 * r] * TD_SCALE);
            td[2 * (CP_LEN + k) + 1] = (uint32_t)(-buf[2 * r + 1] * TD_SCALE);
        }
    }

    res[META + 0]  = 0x1FF7A4B1u;
    res[META + 1]  = mtime() - t0;
    res[META + 2]  = runs;
    res[META + 3]  = errs;
    res[META + 15] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
