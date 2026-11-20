/*
 * GPCP CGRA - bare-metal usage example (compiles for RV32/RV64, no libc needed)
 *
 * Runs a 4x4 integer matrix multiply on the CGRA. The microcode is generated
 * by the compiler (cgra/kernels/matmul.yaml -> matmul_prog.h): this file only
 * stages A and B into the scratchpad, loads the program and reads the products
 * back out of the scratchpad.
 */
#include "gpcp_cgra.h"
#include "../soc/matmul_prog.h"

#ifndef GPCP_CGRA_BASE
#define GPCP_CGRA_BASE 0x40000000u   /* M1 SoC address map */
#endif

static const int32_t A[4][4] = {
    {1, 2, 3, 4},
    {5, 6, 7, 8},
    {9, 1, 2, 3},
    {4, 5, 6, 7},
};

static const int32_t B[4][4] = {
    {1, 0, -1, 2},
    {3, 2, 1, 0},
    {-2, 1, 4, -3},
    {0, 5, 2, 1},
};

static void put_u32(uint32_t v)
{
    /* M1: MMIO write to the UART TX register */
    extern void uart_putc(char);
    char buf[11];
    int i;
    buf[0] = '0';
    buf[1] = 'x';
    for (i = 0; i < 8; i++)
        buf[2 + i] = "0123456789abcdef"[(v >> (28 - 4 * i)) & 0xF];
    buf[10] = '\n';
    for (i = 0; i < 11; i++)
        uart_putc(buf[i]);
}

/* scratchpad flat index = bank*32 + word (the same 10-bit space the LD/ST
 * immediates use); the host window is byte addressed, hence *4. */
static void spad_w(uint32_t idx, uint32_t val)
{
    gpcp_cgra_write(GPCP_CGRA_BASE, GPCP_CGRA_SPAD_BASE + idx * 4u, val);
}
static uint32_t spad_r(uint32_t idx)
{
    return gpcp_cgra_read(GPCP_CGRA_BASE, GPCP_CGRA_SPAD_BASE + idx * 4u);
}

int main(void)
{
    uint32_t pe, k, i;

    /* PE pe computes C[row][col]; stage A[row][k] at flat k+8*pe and B[k][col]
     * at flat 4+k+8*pe (the addresses matmul.yaml declares). */
    for (pe = 0; pe < GPCP_CGRA_NPES; pe++) {
        uint32_t row = pe / 4, col = pe % 4;
        for (k = 0; k < 4; k++) {
            spad_w(k + 8 * pe, (uint32_t)A[row][k]);
            spad_w(4 + k + 8 * pe, (uint32_t)B[k][col]);
        }
    }

    /* load the compiler-generated program, run it, poll for completion */
    for (i = 0; i < MATMUL_PROG_CYCLES * GPCP_CGRA_NPES; i++)
        gpcp_cgra_write(GPCP_CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, matmul_prog[i]);
    gpcp_cgra_array_start(GPCP_CGRA_BASE, 0, MATMUL_PROG_CYCLES);
    while (!(gpcp_cgra_read(GPCP_CGRA_BASE, GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE))
        ;

    for (pe = 0; pe < GPCP_CGRA_NPES; pe++)
        put_u32(spad_r(256 + pe));
    return 0;
}
