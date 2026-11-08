/* GPCP SoC - descriptor ring + burst copy test
 *
 * The copy engine reads descriptors straight out of SoC RAM: software only
 * programs RING_BASE / RING_SIZE / RING_TAIL, and the engine walks the ring,
 * writes each descriptor's status word, advances RING_HEAD and raises a
 * completion interrupt per descriptor (when the descriptor asks for it).
 *
 * Pipeline (2 slots, ring depth 8):
 *   desc0  in  slot0 -> page 0     desc1  in  slot1 -> page 1   (tail = 2)
 *   ... engine runs both back to back, CPU wakes up twice ...
 *   CPU: array(page 0), array(page 1)
 *   desc2  out page 0 -> out0      desc3  out page 1 -> out1   (tail = 4)
 *   ... engine runs both, CPU wakes up twice ...
 *
 * Result area (RESULT_BASE, 32-bit words):
 *   [0..31]   slot 0 H[p].re, H[p].im
 *   [32..63]  slot 1
 *   [64] marker, [65] irqs, [66] bad traps, [67] errs+self-check
 *   [68] ring head at the end, [69] epilogue 0x600D
 *   [70..73] descriptor status words 0..3
 *   [74] ring ctrl/status snapshot
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

#define EXIT_ADDR       0x40010000ul
#define RESULT_BASE     ((uint32_t *)0x80010000ul)
#define CLINT_MTIME     0x0200BFF8ul

#define SLOTS   2
#define CYCLES   CPLX_MUL_PROG_CYCLES
#define CGRA_SRC_ID 1u
#define MSTATUS_MIE 0x8u
#define MIE_MEIE    0x800u

/* ring storage: 8 descriptors, 32-byte aligned, and 8-byte aligned payloads */
static uint32_t desc[8][8] __attribute__((aligned(64)));
static int32_t  slots[SLOTS][64] __attribute__((aligned(8)));
static int32_t  out[SLOTS][32] __attribute__((aligned(8)));

static volatile uint32_t irq_flags;
static volatile uint32_t irq_count;
static volatile uint32_t bad_traps;
static uint32_t errs;


/* --------------------------------------------------------------- trap entry */
void trap_handler(void)
{
    uint64_t cause;
    __asm__ __volatile__("csrr %0, mcause" : "=r"(cause));

    if (cause == 0x800000000000000Bull) {
        uint32_t id = mmio_r(PLIC_CLAIM(0));
        if (id == CGRA_SRC_ID) {
            uint32_t st = gpcp_cgra_irq_status(CGRA_BASE);
            irq_flags |= st;
            gpcp_cgra_irq_clear(CGRA_BASE, st);    /* claim first, then clear */
            irq_count++;
        } else {
            bad_traps++;
        }
        mmio_w(PLIC_CLAIM(0), id);
    } else {
        bad_traps++;
    }
}

static void plic_setup(void)
{
    mmio_w(PLIC_PRIO(CGRA_SRC_ID), 1);
    mmio_w(PLIC_ENABLE(0), 1u << CGRA_SRC_ID);
    mmio_w(PLIC_THRESH(0), 0);
}

static void irq_unmask(void)
{
    uint64_t mie = MIE_MEIE;
    __asm__ __volatile__("csrw mie, %0" :: "r"(mie));
    __asm__ __volatile__("csrs mstatus, %0" :: "r"((uint64_t)MSTATUS_MIE));
}

/* Wait for `n` completed descriptors.  The engine has a progress counter, so
 * the wait is level-based: a completion interrupt that overlaps the handler
 * cannot be "lost" the way a W1C status bit can. */
static void wait_done(uint32_t n)
{
    uint32_t guard = 0;
    while (gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_RING_DONE) < n) {
        __asm__ __volatile__("csrci mstatus, 8");
        if (gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_RING_DONE) < n)
            __asm__ __volatile__("wfi");
        __asm__ __volatile__("csrsi mstatus, 8");
        if (++guard > 200000u) { errs++; break; }
    }
}

/* --------------------------------------------------------------- payload */
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

/* --------------------------------------------------------------- descriptors */
static void desc_set(int idx, uintptr_t src, uint32_t dst, uint32_t len,
                     uint32_t page, uint32_t dir_out, uint32_t stride, uint32_t row)
{
    desc[idx][0] = (uint32_t)src;
    desc[idx][1] = (dst & 0xFFFFu) | (page ? GPCP_DMA_DESC_PAGE1 : 0u) |
                   (dir_out ? GPCP_DMA_DESC_OUT : 0u) | GPCP_DMA_DESC_IRQ;
    desc[idx][2] = len & 0xFFFFu;
    desc[idx][3] = (stride & 0xFFFFu) | (row << 16);
    desc[idx][4] = 0;
    for (int i = 5; i < 8; i++)
        desc[idx][i] = 0;
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
            if ((uint32_t)out[s][2 * p + 0] != ((uint32_t)hre & 0xFFFFFFFFu)) bad++;
            if ((uint32_t)out[s][2 * p + 1] != ((uint32_t)him & 0xFFFFFFFFu)) bad++;
        }
    }
    return bad;
}

/* --------------------------------------------------------------------- main */
int main(void)
{
    uint32_t *res = RESULT_BASE;

    for (int s = 0; s < SLOTS; s++) {
        fill_slot(s);
        for (int i = 0; i < 32; i++)
            out[s][i] = 0;
    }
    load_program();

    /* ring: 2 input copies first, the output copies are pushed later */
    desc_set(0, (uintptr_t)&slots[0][0], 0,                  64, 0, 0, 32, 4);
    desc_set(1, (uintptr_t)&slots[1][0], 0,                  64, 1, 0, 32, 4);
    desc_set(2, (uintptr_t)&out[0][0],   GPCP_SPAD_IDX(16, 0), 32, 0, 1, 32, 2);
    desc_set(3, (uintptr_t)&out[1][0],   GPCP_SPAD_IDX(16, 0), 32, 1, 1, 32, 2);

    plic_setup();
    gpcp_cgra_irq_enable(CGRA_BASE, GPCP_CGRA_IRQ_ARR | GPCP_CGRA_IRQ_DMA);
    irq_unmask();

    gpcp_cgra_ring_start(CGRA_BASE, (uintptr_t)&desc[0][0], 8, 2);
    uint32_t t0 = mmio_r(CLINT_MTIME);
    wait_done(2);                       /* both input descriptors done */
    res[75] = mmio_r(CLINT_MTIME) - t0; /* 2 x 64 words, burst copy */

    gpcp_cgra_array_start(CGRA_BASE, 0, CYCLES);
    {
        uint32_t g = 0;
        while (!(gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE)) {
            if (++g > 100000u) { errs++; break; }
        }
    }
    gpcp_cgra_array_start(CGRA_BASE, 1, CYCLES);
    {
        uint32_t g = 0;
        while (!(gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE)) {
            if (++g > 100000u) { errs++; break; }
        }
    }

    gpcp_cgra_ring_push(CGRA_BASE, 4);  /* let the engine drain both outputs */
    uint32_t t1 = mmio_r(CLINT_MTIME);
    wait_done(4);
    res[76] = mmio_r(CLINT_MTIME) - t1; /* 2 x 32 words, burst copy */

    gpcp_cgra_irq_enable(CGRA_BASE, 0);
    gpcp_cgra_ring_push(CGRA_BASE, 4);
    mmio_w(PLIC_ENABLE(0), 0);
    __asm__ __volatile__("csrci mstatus, 8");

    /* ---- throughput probe: one 1024-word linear copy (engine limited) ---- */
    {
        static uint32_t big[1024] __attribute__((aligned(8)));
        for (int i = 0; i < 1024; i++)
            big[i] = (uint32_t)(i + 1);
        plic_setup();
        gpcp_cgra_irq_enable(CGRA_BASE, GPCP_CGRA_IRQ_DMA);
        irq_unmask();
        uint32_t t2 = mmio_r(CLINT_MTIME);
        gpcp_cgra_dma_start(CGRA_BASE, (uintptr_t)&big[0], 0, 1024, 0, 0, 0, 0);
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
        res[77] = mmio_r(CLINT_MTIME) - t2;
        gpcp_cgra_irq_enable(CGRA_BASE, 0);
        mmio_w(PLIC_ENABLE(0), 0);
        __asm__ __volatile__("csrci mstatus, 8");
    }

    for (int s = 0; s < SLOTS; s++)
        for (int i = 0; i < 32; i++)
            res[s * 32 + i] = (uint32_t)out[s][i];

    res[64] = 0xCAFEBABEu;
    res[65] = irq_count;
    res[66] = bad_traps;
    res[67] = errs + self_check();
    res[68] = gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_RING_HEAD);
    res[69] = 0x600Du;
    for (int d = 0; d < 4; d++)
        res[70 + d] = desc[d][4];
    res[74] = gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_RING_CTRL);

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
