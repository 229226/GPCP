/* -----------------------------------------------------------------------------
 * FFT probe (SoC level): run the same 4096-point engine the SSB program uses on
 * the same time-domain symbol, then export the whole spectrum.
 *
 * Used to separate "engine/port wrong" from "SSB program plumbing wrong":
 * the input is exactly build/ssb/td.bin symbol 1 (loaded at 0x8040_0000),
 * so the exported bins can be compared with tools/ssb_plan.py's float FFT.
 *
 * Results at 0x8001_0000 (words):
 *   [0 .. 8191]  4096 complex bins, X[k] = data[rev4(k)] (re, im)
 *   [8192]       0x600D
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#define DESC_BASE        0x80020000ul
#define RING_DEPTH       1024
#define FFT_N            4096
#define SYM_BASE         0x80408000ul          /* symbol 1 of build/ssb/td.bin */


/* globals expected by ssb_fft.inc */
#define NPES             16
static uint32_t prog[1];
static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;
static uint32_t errs, runs;

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; break; }
    }
}

#include "ssb_fft.inc"

int main(void)
{
    int32_t *td = (int32_t *)SYM_BASE;
    fft4096(td);
    for (uint32_t k = 0; k < FFT_N; k++) {
        uint32_t src = 2u * rev4(k);
        RESULT_BASE[2u * k] = (uint32_t)td[src];
        RESULT_BASE[2u * k + 1u] = (uint32_t)td[src + 1u];
    }
    RESULT_BASE[2u * FFT_N] = 0x600D;
    *(volatile uint32_t *)EXIT_ADDR = 0;
    while (1) { }
    return 0;
}
