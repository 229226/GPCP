/* -----------------------------------------------------------------------------
 * Single variable block through the streaming BN microcode (debug bench)
 *
 * Builds one block's BN input in RAM (24 message slots + LLR + hard), DMAes it
 * into the scratchpad, runs the BN image twice (Zc = 20 > 16 PEs), DMAes it
 * back and copies all 26 x 20 words to the result area for a value-by-value
 * comparison with tools/check_pdsch_bn1.py.
 *
 * Results at 0x8001_0000: [0] marker [1] runs [2] DMA [3] errors [4] 0x600D
 *                          [8..] 26 x 20 words (bank-major)
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"

#define CGRA_BASE   0x50000000ul
#define EXIT_ADDR   0x40010000ul
#define RESULT_BASE ((uint32_t *)0x80010000ul)
#define ZC          20
#define NPES        16
#define BNSLOT      24
#define CYC_BN      130
#define LCG_SEED    0xB1u

#define BNB_BASE    0x80030000ul

static int32_t (*bnb)[ZC] = (int32_t (*)[ZC])BNB_BASE;

static inline void mmio_w(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static inline uint32_t mmio_r(uintptr_t a) { return *(volatile uint32_t *)a; }
#define SPAD_IDX(bank, word) GPCP_SPAD_IDX((bank), (word))

static uint32_t prog[2][CYC_BN * NPES];
static uint32_t runs, dmas, errs;

static void emit(uint32_t which, uint32_t cyc, uint32_t pe, uint32_t op,
                 uint32_t a, uint32_t b, uint32_t dst, uint32_t imm)
{
    prog[which][cyc * NPES + pe] = gpcp_cgra_inst(op, a, b, dst, imm);
}

static void build_bn(uint32_t which, uint32_t word_base)
{
    for (uint32_t pe = 0; pe < NPES; pe++) {
        uint32_t w = word_base + pe;
        if (w >= ZC)
            continue;
        uint32_t cyc = 0;
        emit(which, cyc++, pe, GPCP_OP_XOR, 1, 1, 1, 0);
        for (uint32_t k = 0; k < BNSLOT; k++) {
            emit(which, cyc++, pe, GPCP_OP_LD, 0, 0, 0, SPAD_IDX(k, w));
            emit(which, cyc++, pe, GPCP_OP_ADD, 1, 0, 1, 0);
        }
        emit(which, cyc++, pe, GPCP_OP_LD, 0, 0, 0, SPAD_IDX(BNSLOT, w));
        emit(which, cyc++, pe, GPCP_OP_ADD, 1, 0, 1, 0);
        for (uint32_t k = 0; k < BNSLOT; k++) {
            emit(which, cyc++, pe, GPCP_OP_LD, 0, 0, 0, SPAD_IDX(k, w));
            emit(which, cyc++, pe, GPCP_OP_SUB, 1, 0, 2, 0);
            emit(which, cyc++, pe, GPCP_OP_ST, 2, 0, 0, SPAD_IDX(k, w));
        }
        emit(which, cyc++, pe, GPCP_OP_SHR, 1, 0, 0, 31);
        emit(which, cyc++, pe, GPCP_OP_ST, 0, 0, 0, SPAD_IDX(BNSLOT + 1, w));
    }
}

static void load_prog(uint32_t which)
{
    for (uint32_t i = 0; i < CYC_BN * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, prog[which][i]);
}

static void array_run(void)
{
    gpcp_cgra_array_start(CGRA_BASE, 0, CYC_BN);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY)
        if (++spins > 200000u) { errs++; break; }
    runs++;
}

static void dma_2d(uintptr_t ram, uint32_t spad, uint32_t len, uint32_t dir)
{
    uint32_t g = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_BUSY)
        if (++g > 200000u) { errs++; return; }
    gpcp_cgra_dma_start(CGRA_BASE, ram, spad, len, 0, dir, 32, ZC);
    g = 0;
    while (!(mmio_r(CGRA_BASE + GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_DONE))
        if (++g > 200000u) { errs++; return; }
    dmas++;
}

static uint32_t lcg = LCG_SEED;
static int32_t next_val(void)
{
    lcg = lcg * 1664525u + 1013904223u;
    return (int32_t)((lcg >> 16) & 0x3FFu) - 512;
}

int main(void)
{
    uint32_t *out = RESULT_BASE;

    for (uint32_t k = 0; k < BNSLOT; k++)
        for (uint32_t i = 0; i < ZC; i++)
            bnb[k][i] = next_val();          /* message slots */
    for (uint32_t i = 0; i < ZC; i++) {
        bnb[BNSLOT][i] = next_val();         /* LLR */
        bnb[BNSLOT + 1][i] = 0;              /* hard (stale) */
    }

    build_bn(0, 0);
    build_bn(1, 16);

    dma_2d((uintptr_t)&bnb[0][0], 0, (BNSLOT + 2) * ZC, 0);
    load_prog(0);
    array_run();
    load_prog(1);
    array_run();
    dma_2d((uintptr_t)&bnb[0][0], 0, (BNSLOT + 2) * ZC, 1);

    for (uint32_t k = 0; k < BNSLOT + 2; k++)
        for (uint32_t i = 0; i < ZC; i++)
            out[8 + k * ZC + i] = (uint32_t)bnb[k][i];

    out[0] = 0xCAFEBABEu;
    out[1] = runs;
    out[2] = dmas;
    out[3] = errs;
    out[4] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
