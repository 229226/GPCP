/* GPCP SoC - slot-synchronous scheduler skeleton (CLINT time base)
 *
 * This is the "real slot loop" version of irq_bm: instead of waiting for each
 * completion in a straight line, the CLINT machine timer defines the slot
 * cadence (one OFDM slot every SLOT_PERIOD cycles) and a machine-timer
 * interrupt wakes the scheduler.  The scheduler then services the slot
 * (descriptor push -> array run -> descriptor drain) and goes back to sleep
 * (wfi) until the next tick.  The CGRA completion interrupt is the second
 * source and only counts progress; the main loop polls progress counters,
 * not interrupt edges, so an interrupt that overlaps a handler is never lost.
 *
 * Per slot the array runs the 15-cycle LS channel estimate (chnest16) and the
 * copy engine moves 64 words in (Yr,Yi,Xr,Xi per subcarrier) and 48 words out
 * (Hr,Hi).  Double buffering: slot k uses scratchpad page k & 1.
 *
 * Result area (RESULT_BASE, 32-bit words):
 *   [0..255]    8 slots x 16 PEs x {Hr, Hi}
 *   [256]       marker 0xCAFEBABE      [257] timer ticks
 *   [258]       slots completed         [259] deadline overruns
 *   [260]       interrupts taken        [261] unexpected traps
 *   [262]       error count             [263] cycles for the whole run
 *   [264]       slot period             [265] epilogue 0x600D
 *   [266..273]  per-slot service cycles
 *   [274..281]  per-slot tick timestamps (mtime)
 */

#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "cplx_mul_prog.h"      /* 编译器生成的单抽头复乘/LS 估计 */

#define CGRA_BASE      0x50000000ul
#define PLIC_BASE      0x0C000000ul
#define PLIC_PRIO(id)     (PLIC_BASE + 4u * (id))
#define PLIC_ENABLE(ctx)  (PLIC_BASE + 0x1000u + 4u * (ctx))
#define PLIC_THRESH(ctx)  (PLIC_BASE + 0x2000u + 4u * (ctx))
#define PLIC_CLAIM(ctx)   (PLIC_BASE + 0x200000u + 4u * (ctx))

#define CLINT_MTIMECMP 0x02004000ul
#define CLINT_MTIME    0x0200BFF8ul
#define EXIT_ADDR      0x40010000ul
#define RESULT_BASE    ((uint32_t *)0x80010000ul)

#define SLOTS          8
#define NPES           16
#define WORDS_IN       64      /* 4 words per bank (Yr,Yi,Xr,Xi) */
#define WORDS_OUT      32      /* 2 words per bank (Hr,Hi)      */
#define CYC_LS         CPLX_MUL_PROG_CYCLES
#define SLOT_PERIOD    1200    /* cycles between slot ticks (1 tick = 1 slot) */

#define MSTATUS_MIE    0x8u
#define MIE_MTIE       0x80u
#define MIE_MEIE       0x800u
#define CAUSE_TIMER    0x8000000000000007ull
#define CAUSE_EXTERNAL 0x800000000000000Bull
#define CGRA_SRC_ID    1u

static int32_t  in[SLOTS][WORDS_IN]  __attribute__((aligned(8)));
static int32_t  out[SLOTS][WORDS_OUT] __attribute__((aligned(8)));

static volatile uint32_t ticks, irq_count, arr_count, ring_done_count, bad_traps;
static volatile uint32_t slot_pending;
static uint32_t overruns, errs, slots_done;
static uint32_t t_start[SLOTS], t_end[SLOTS];

static uint64_t next_tick;


/* --------------------------------------------------------------- trap entry */
void trap_handler(void)
{
    uint64_t cause;
    __asm__ __volatile__("csrr %0, mcause" : "=r"(cause));

    if (cause == CAUSE_TIMER) {
        /* the cadence is absolute: keep the ideal schedule so a late handler
         * shows up as a deadline overrun instead of silently stretching time */
        ticks++;
        next_tick += SLOT_PERIOD;
        *(volatile uint64_t *)CLINT_MTIMECMP = next_tick;
        if (slot_pending)
            overruns++;           /* previous slot not serviced yet */
        slot_pending = 1;
    } else if (cause == CAUSE_EXTERNAL) {
        uint32_t id = mmio_r(PLIC_CLAIM(0));
        if (id == CGRA_SRC_ID) {
            uint32_t st = gpcp_cgra_irq_status(CGRA_BASE);
            if (st & GPCP_CGRA_IRQ_ARR) arr_count++;
            gpcp_cgra_irq_clear(CGRA_BASE, st);
            ring_done_count = gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_RING_DONE);
            irq_count++;
        } else {
            bad_traps++;
        }
        mmio_w(PLIC_CLAIM(0), id);
    } else {
        bad_traps++;
    }
}

/* ------------------------------------------------------------ wait helpers */
/* The slot service sleeps in wfi between steps: the CGRA completion interrupt
 * (PLIC source 1) wakes it, so the CPU is idle whenever the array or the copy
 * engine is working. */
static void wait_dma(void)
{
    uint32_t g = 0;
    while (!(gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_DONE)) {
        __asm__ __volatile__("csrci mstatus, 8");
        if (!(gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_DONE))
            __asm__ __volatile__("wfi");
        __asm__ __volatile__("csrsi mstatus, 8");
        if (++g > 200000u) { errs++; break; }
    }
}

static void array_run(uint32_t page)
{
    uint32_t want = arr_count + 1;
    uint32_t g = 0;
    gpcp_cgra_array_start(CGRA_BASE, page, CYC_LS);
    while (arr_count < want) {
        __asm__ __volatile__("csrci mstatus, 8");
        if (arr_count < want)
            __asm__ __volatile__("wfi");
        __asm__ __volatile__("csrsi mstatus, 8");
        if (++g > 200000u) { errs++; break; }
    }
}

/* one slot job: 64 words in (4 per bank), 15-cycle LS estimate, 32 words out */
static void slot_job(uint32_t k)
{
    uint32_t page = k & 1u;
    gpcp_cgra_dma_start(CGRA_BASE, (uintptr_t)&in[k][0], 0, WORDS_IN, page,
                        0, 32, 4);
    wait_dma();
    array_run(page);
    gpcp_cgra_dma_start(CGRA_BASE, (uintptr_t)&out[k][0], GPCP_SPAD_IDX(16, 0),
                        WORDS_OUT, page, 1, 32, 2);
    wait_dma();
}

/* --------------------------------------------------------------- microcode */
/* ----------------------------------------------------------------- payload */
static int32_t pseudo(int seed, int mod)
{
    return (seed % mod) - (mod / 2);
}

static void fill_slot(int s)
{
    for (int i = 0; i < NPES; i++) {
        in[s][i * 4 + 0] = pseudo(s * 97 + i * 37 + 13, 8001);
        in[s][i * 4 + 1] = pseudo(s * 53 + i * 29 + 71, 8001);
        in[s][i * 4 + 2] = ((s + i) & 1) ? 23170 : -23170;
        in[s][i * 4 + 3] = ((s + i) & 2) ? 23170 : -23170;
    }
}

static int32_t sx16s(int32_t v)
{
    v &= 0xFFFF;
    return (v & 0x8000) ? v - 0x10000 : v;
}

static uint32_t self_check(void)
{
    uint32_t bad = 0;
    for (int s = 0; s < SLOTS; s++) {
        for (int p = 0; p < NPES; p++) {
            int32_t yr = sx16s(in[s][p * 4 + 0]);
            int32_t yi = sx16s(in[s][p * 4 + 1]);
            int32_t xr = sx16s(in[s][p * 4 + 2]);
            int32_t xi = sx16s(in[s][p * 4 + 3]);
            int32_t hr = (yr * xr + yi * xi) >> 15;
            int32_t hi = (yi * xr - yr * xi) >> 15;
            if (out[s][p * 2 + 0] != hr || out[s][p * 2 + 1] != hi)
                bad++;
        }
    }
    return bad;
}

/* --------------------------------------------------------------------- main */
int main(void)
{
    uint32_t *res = RESULT_BASE;

    for (int s = 0; s < SLOTS; s++)
        fill_slot(s);

    for (uint32_t i = 0; i < CYC_LS * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, cplx_mul_prog[i]);

    /* PLIC: CGRA completion is source 1 */
    mmio_w(PLIC_PRIO(CGRA_SRC_ID), 1);
    mmio_w(PLIC_ENABLE(0), 1u << CGRA_SRC_ID);
    mmio_w(PLIC_THRESH(0), 0);
    gpcp_cgra_irq_enable(CGRA_BASE, GPCP_CGRA_IRQ_ARR | GPCP_CGRA_IRQ_DMA);

    uint32_t t0 = mmio_r(CLINT_MTIME);
    next_tick = (uint64_t)t0 + SLOT_PERIOD;
    *(volatile uint64_t *)CLINT_MTIMECMP = next_tick;

    uint64_t mie = MIE_MTIE | MIE_MEIE;
    __asm__ __volatile__("csrw mie, %0" :: "r"(mie));
    __asm__ __volatile__("csrs mstatus, %0" :: "r"((uint64_t)MSTATUS_MIE));

    /* the slot loop: sleep until a tick, service one slot, sleep again */
    while (slots_done < SLOTS) {
        __asm__ __volatile__("wfi");
        if (!slot_pending)
            continue;
        slot_pending = 0;

        uint32_t k = slots_done;
        t_start[k] = mmio_r(CLINT_MTIME);
        slot_job(k);
        t_end[k] = mmio_r(CLINT_MTIME);
        slots_done++;
    }

    /* stop the time base and the CGRA interrupts */
    __asm__ __volatile__("csrci mstatus, 8");
    next_tick = ~0ull;
    *(volatile uint64_t *)CLINT_MTIMECMP = next_tick;
    gpcp_cgra_irq_enable(CGRA_BASE, 0);
    mmio_w(PLIC_ENABLE(0), 0);
    uint32_t t1 = mmio_r(CLINT_MTIME);

    for (int s = 0; s < SLOTS; s++)
        for (int i = 0; i < WORDS_OUT; i++)
            res[s * WORDS_OUT + i] = (uint32_t)out[s][i];

    res[256] = 0xCAFEBABEu;
    res[257] = ticks;
    res[258] = slots_done;
    res[259] = overruns;
    res[260] = irq_count;
    res[261] = bad_traps;
    res[262] = errs + self_check();
    res[263] = t1 - t0;
    res[264] = SLOT_PERIOD;
    res[265] = 0x600Du;
    for (int s = 0; s < SLOTS; s++) {
        res[266 + s] = t_end[s] - t_start[s];
        res[274 + s] = t_start[s];
    }

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
