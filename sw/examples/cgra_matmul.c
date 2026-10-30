/*
 * GPCP CGRA - bare-metal usage example (compiles for RV32/RV64, no libc needed)
 *
 * Runs a 4x4 integer matrix multiply on the CGRA and prints the result through
 * a simple UART hook that the M1 SoC provides.
 */
#include "gpcp_cgra.h"

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

int main(void)
{
    static uint32_t prog[4 * GPCP_CGRA_NPES];
    static uint32_t pre[GPCP_CGRA_NPES * 8 * 2];
    uint32_t pe, k, n = 0;

    for (k = 0; k < 4; k++)
        for (pe = 0; pe < GPCP_CGRA_NPES; pe++)
            prog[k * GPCP_CGRA_NPES + pe] =
                gpcp_cgra_inst(GPCP_OP_MAC, k, 4 + k, 0, 0);

    for (pe = 0; pe < GPCP_CGRA_NPES; pe++) {
        uint32_t row = pe / 4, col = pe % 4;
        for (k = 0; k < 4; k++) {
            pre[n++] = pe * 8 + k;
            pre[n++] = (uint32_t)A[row][k];
        }
        for (k = 0; k < 4; k++) {
            pre[n++] = pe * 8 + 4 + k;
            pre[n++] = (uint32_t)B[k][col];
        }
    }

    gpcp_cgra_task_t task = {prog, 6, pre, n};
    if (gpcp_cgra_run(GPCP_CGRA_BASE, &task) != 0)
        return 1;

    for (pe = 0; pe < GPCP_CGRA_NPES; pe++) {
        put_u32(gpcp_cgra_pe_acc(GPCP_CGRA_BASE, pe));
    }
    return 0;
}
