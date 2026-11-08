/* GPCP SoC - interrupt-driven DMA + double buffering on the bare-metal core
 *
 * Same slot pipeline as dma_bm.c, but the CPU never polls the CGRA: the
 * copy-engine and array "done" events raise a level interrupt which the SoC
 * PLIC forwards to CVA6 as machine-external (cause 11).  The trap handler
 * claims the PLIC source, clears the CGRA status bits, completes the source and
 * publishes a flag; the main loop sleeps in `wfi` until the flag appears.
 *
 * Data path per slot (2 slots):
 *   dma_in(slot0 -> page 0)  -> irq
 *   dma_in(slot1 -> page 1)  ||  array(page 0)  -> irq(s)
 *   dma_out(page 0 -> out0)  ||  array(page 1)  -> irq(s)
 *   dma_out(page 1 -> out1)  -> irq
 *
 * Result area (RESULT_BASE, 32-bit words):
 *   [0..31]   slot 0 H[p].re, H[p].im
 *   [32..63]  slot 1
 *   [64]      marker 0xCAFEBABE
 *   [65]      interrupts taken
 *   [66]      spurious/unexpected traps
 *   [67]      on-chip error count (timeouts + software self-check)
 *   [68]      slot count
 *   [69]      epilogue marker 0x600D
 */

#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "cplx_mul_prog.h"      /* 编译器生成的单抽头复乘/LS 估计 */

#define CGRA_BASE    0x50000000ul
#define PLIC_BASE    0x0C000000ul
#define PLIC_PRIO(id)     (PLIC_BASE + 4u * (id))
#define PLIC_ENABLE(ctx)  (PLIC_BASE + 0x1000u + 4u * (ctx))
#define PLIC_THRESH(ctx)  (PLIC_BASE + 0x2000u + 4u * (ctx))
#define PLIC_CLAIM(ctx)   (PLIC_BASE + 0x200000u + 4u * (ctx))

#define CLINT_MTIMECMP  0x02004000ul
#define CLINT_MTIME     0x0200BFF8ul
#define EXIT_ADDR       0x40010000ul
#define RESULT_BASE     ((uint32_t *)0x80010000ul)

#define SLOTS   2
#define CYCLES   CPLX_MUL_PROG_CYCLES
#define CGRA_SRC_ID 1u

#define MSTATUS_MIE 0x8u
#define MIE_MEIE    0x800u     /* machine external */
#define MIE_MTIE    0x80u

/* The copy engine moves 64-bit beats, so both ends must be 8-byte aligned */
static int32_t slots[SLOTS][64] __attribute__((aligned(8)));
static int32_t out[SLOTS][32] __attribute__((aligned(8)));

static volatile uint32_t irq_flags;      /* bits published by the handler */
static volatile uint32_t irq_count;
static volatile uint32_t bad_traps;
static uint32_t errs;


/* --------------------------------------------------------------- trap entry */
void trap_handler(void)
{
    uint64_t cause;
    __asm__ __volatile__("csrr %0, mcause" : "=r"(cause));

    if (cause == 0x800000000000000Bull) {          /* machine external */
        uint32_t id = mmio_r(PLIC_CLAIM(0));
        if (id == CGRA_SRC_ID) {
            uint32_t st = gpcp_cgra_irq_status(CGRA_BASE);
            irq_flags |= st;
            gpcp_cgra_irq_clear(CGRA_BASE, st);    /* claim first, then clear */
            irq_count++;
        } else {
            bad_traps++;
        }
        mmio_w(PLIC_CLAIM(0), id);                 /* complete */
    } else if (cause == 0x8000000000000007ull) {   /* machine timer: disarm */
        mmio_w(CLINT_MTIMECMP, 0xFFFFFFFFu);
        mmio_w(CLINT_MTIMECMP + 4, 0xFFFFFFFFu);
        bad_traps++;
    } else {
        bad_traps++;
    }
}

/* ------------------------------------------------------------------ helpers */
static void plic_setup(void)
{
    mmio_w(PLIC_PRIO(CGRA_SRC_ID), 1);             /* priority > 0 */
    mmio_w(PLIC_ENABLE(0), 1u << CGRA_SRC_ID);
    mmio_w(PLIC_THRESH(0), 0);
}

static void irq_unmask(void)
{
    uint64_t mie = MIE_MEIE;
    __asm__ __volatile__("csrw mie, %0" :: "r"(mie));
    __asm__ __volatile__("csrs mstatus, %0" :: "r"((uint64_t)MSTATUS_MIE));
}

/* Wait until the handler published one of `need`; the CPU idles in wfi.
 * MIE is disabled only around the test so that an interrupt arriving in the
 * check->wfi window cannot slip through (CVA6 wakes from wfi on irq_i[1]
 * even when MIE is clear, so this cannot deadlock). */
static void wait_flags(uint32_t need, uint32_t *spins)
{
    uint32_t guard = 0;
    while (!(irq_flags & need)) {
        __asm__ __volatile__("csrci mstatus, 8");
        if (!(irq_flags & need))
            __asm__ __volatile__("wfi");
        __asm__ __volatile__("csrsi mstatus, 8");
        if (++guard > 1000000u) { errs++; break; }
    }
    irq_flags &= ~need;
    *spins += guard;
}

static void dma_idle(uint32_t *spins)
{
    uint32_t guard = 0;
    while (gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_BUSY) {
        if (++guard > 200000u) { errs++; return; }
    }
    *spins += guard;
}

static void dma_start(uintptr_t src, uint32_t dst, uint32_t len, uint32_t page,
                      uint32_t dir, uint32_t stride, uint32_t row, uint32_t *spins)
{
    dma_idle(spins);
    gpcp_cgra_dma_start(CGRA_BASE, src, dst, len, page, dir, stride, row);
}

static void dma_in(int s, uint32_t page, uint32_t *spins)
{
    dma_start((uintptr_t)&slots[s][0], 0, 64, page, 0, 32, 4, spins);
}

static void dma_out(int s, uint32_t page, uint32_t *spins)
{
    dma_start((uintptr_t)&out[s][0], GPCP_SPAD_IDX(16, 0), 32, page, 1, 32, 2, spins);
}

static void array_start(uint32_t page)
{
    gpcp_cgra_array_start(CGRA_BASE, page, CYCLES);
}

/* ------------------------------------------------------------------ payload */
static void fill_slot(int s)
{
    for (int i = 0; i < 64; i++) {
        int v = ((s * 31 + i * 17 + 11) % 20001) - 10000;
        int k = i & 3;
        slots[s][i] = (k >= 2) ? (v >= 0 ? 23170 : -23170) : v;
    }
}

static void load_program(void)
{
    if (GPCP_CGRA_MAX_CYCLES < CYCLES) { errs++; return; }
    for (uint32_t i = 0; i < CYCLES * 16; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, cplx_mul_prog[i]);
}

static int32_t sx16_s(int32_t v)
{
    v &= 0xFFFF;
    return (v & 0x8000) ? v - 0x10000 : v;
}

static uint32_t self_check(void)
{
    uint32_t bad = 0;
    for (int s = 0; s < SLOTS; s++) {
        for (int p = 0; p < 16; p++) {
            int32_t yr = sx16_s(slots[s][p * 4 + 0]);
            int32_t yi = sx16_s(slots[s][p * 4 + 1]);
            int32_t xr = sx16_s(slots[s][p * 4 + 2]);
            int32_t xi = sx16_s(slots[s][p * 4 + 3]);
            int32_t hre = (yr * xr + yi * xi) >> 15;
            int32_t him = (yi * xr - yr * xi) >> 15;
            if ((uint32_t)out[s][2 * p + 0] != ((uint32_t)hre & 0xFFFFFFFFu))
                bad++;
            if ((uint32_t)out[s][2 * p + 1] != ((uint32_t)him & 0xFFFFFFFFu))
                bad++;
        }
    }
    return bad;
}

/* --------------------------------------------------------------------- main */
int main(void)
{
    uint32_t *res = RESULT_BASE;
    uint32_t spins = 0;

    for (int s = 0; s < SLOTS; s++) {
        fill_slot(s);
        for (int i = 0; i < 32; i++)
            out[s][i] = 0;
    }

    load_program();

    plic_setup();
    gpcp_cgra_irq_enable(CGRA_BASE, GPCP_CGRA_IRQ_ARR | GPCP_CGRA_IRQ_DMA);
    irq_unmask();

    /* ---- slot 0: load page 0, wait for the copy-engine interrupt ---- */
    dma_in(0, 0, &spins);
    wait_flags(GPCP_CGRA_IRQ_DMA, &spins);

    /* ---- prefetch slot 1 while the array works on page 0 ---- */
    dma_in(1, 1, &spins);
    array_start(0);
    wait_flags(GPCP_CGRA_IRQ_ARR | GPCP_CGRA_IRQ_DMA, &spins);

    /* ---- copy out page 0 while the array works on page 1 ---- */
    dma_out(0, 0, &spins);
    array_start(1);
    wait_flags(GPCP_CGRA_IRQ_ARR | GPCP_CGRA_IRQ_DMA, &spins);

    /* ---- drain the second slot ---- */
    dma_out(1, 1, &spins);
    wait_flags(GPCP_CGRA_IRQ_DMA, &spins);

    gpcp_cgra_irq_enable(CGRA_BASE, 0);
    mmio_w(PLIC_ENABLE(0), 0);
    __asm__ __volatile__("csrci mstatus, 8");

    for (int s = 0; s < SLOTS; s++)
        for (int i = 0; i < 32; i++)
            res[s * 32 + i] = (uint32_t)out[s][i];

    res[64] = 0xCAFEBABEu;
    res[65] = irq_count;
    res[66] = bad_traps;
    res[67] = errs + self_check();
    res[68] = (uint32_t)SLOTS;
    res[69] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
