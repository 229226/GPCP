/* -----------------------------------------------------------------------------
 * T0: measure the fixed cost of one CGRA array run (AGENTS.md §6 judgment 3).
 *
 * Deliberately does NOT modify rt_array_run(): mtime() brackets the existing
 * runtime call, so every other benchmark keeps the same behaviour.  IMEM is
 * filled with the compiler-generated fft_stage image (no hand-written
 * microcode) so the instructions issued are real, and `cycles` only truncates
 * how many of them run.  Five cycle counts x many repetitions give
 *
 *     T(c) = fixed_overhead + per_cycle_cost * c
 *
 * The residual against run-ifft's 1938 cycles/run (which also pays for the
 * DMA descriptor ring and page ping-pong) is the DMA/orchestration cost.
 *
 * Result area (0x8001_0000, 32-bit words):
 *   [0] marker [1] repetitions [2] number of points [3] IMEM cycles
 *   [4 + 3p] cycles p   [5 + 3p] mean cycles   [6 + 3p] timeouts
 *   [23] epilogue 0x600D
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"
#include "fft_stage_prog.h"      /* compiler-generated, 448 cycles x 16 PEs */

#define CGRA_BASE    0x50000000ul
#define EXIT_ADDR    0x40010000ul
#define RESULT_BASE  ((uint32_t *)0x80010000ul)
#define NPES         16
#define REPS         64
#define NPTS         5
#define META         24

static const uint32_t pts[NPTS] = {1u, 8u, 32u, 128u, FFT_STAGE_PROG_CYCLES};

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

int main(void)
{
    uint32_t *res = RESULT_BASE;
    uint32_t i, p, r;

    for (i = 0; i < FFT_STAGE_PROG_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u,
                        fft_stage_prog[i]);

    for (p = 0; p < NPTS; p++) {
        uint64_t total = 0;
        uint32_t bad = 0;
        for (r = 0; r < REPS; r++) {
            uint32_t t0 = mtime();
            bad += (uint32_t)rt_array_run(CGRA_BASE, 0, pts[p]);
            total += (uint32_t)(mtime() - t0);
        }
        res[4 + 3 * p]     = pts[p];
        res[5 + 3 * p]     = (uint32_t)(total / REPS);
        res[6 + 3 * p]     = bad;
    }

    res[0] = 0x0FF1C1A1u;
    res[1] = REPS;
    res[2] = NPTS;
    res[3] = FFT_STAGE_PROG_CYCLES;
    res[META - 1] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
