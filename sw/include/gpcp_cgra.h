/*
 * GPCP CGRA - software ABI (milestone M0)
 *
 * This header is shared by:
 *   - the C++ bus functional model used in Verilator simulation (M0)
 *   - bare-metal / Linux drivers running on the RISC-V core (M1+)
 *
 * Keep it free of libc dependencies so it can be used with -nostdlib.
 */
#ifndef GPCP_CGRA_H
#define GPCP_CGRA_H

#include <stdint.h>
#include <stddef.h>

/* ------------------------------------------------------------------ geometry */
#define GPCP_CGRA_N            4u
#define GPCP_CGRA_NPES         (GPCP_CGRA_N * GPCP_CGRA_N)
#define GPCP_CGRA_MAX_CYCLES   512u
#define GPCP_CGRA_IMEM_WORDS   8192u

/* ------------------------------------------------------------- register map */
#define GPCP_CGRA_CTRL         0x0000u   /* W: [0] start, [1] clear done       */
#define GPCP_CGRA_STATUS       0x0000u   /* R: [0] busy, [1] done, [31:16] cyc */
#define GPCP_CGRA_NUM_CYCLES   0x0004u
#define GPCP_CGRA_CYCLE_CNT    0x0008u
#define GPCP_CGRA_ID           0x000Cu

#define GPCP_CGRA_PE_BASE      0x1000u   /* 0x1000 + pe*32 + reg*4             */
#define GPCP_CGRA_PE_ACC       0x1200u   /* 0x1200 + pe*4                      */
#define GPCP_CGRA_IMEM_BASE    0x2000u   /* 0x2000 + idx*4, idx=cycle*NPES+pe  */

#define GPCP_CGRA_CTRL_START   (1u << 0)
#define GPCP_CGRA_CTRL_CLRDONE (1u << 1)

#define GPCP_CGRA_STATUS_BUSY  (1u << 0)
#define GPCP_CGRA_STATUS_DONE  (1u << 1)

/* ------------------------------------------------------------ instruction set */
#define GPCP_OP_NOP   0u
#define GPCP_OP_MOV   1u
#define GPCP_OP_ADD   2u
#define GPCP_OP_SUB   3u
#define GPCP_OP_MUL   4u
#define GPCP_OP_MAC   5u
#define GPCP_OP_LACC  6u
#define GPCP_OP_SHR   7u
#define GPCP_OP_SHL   8u
#define GPCP_OP_AND   9u
#define GPCP_OP_XOR   10u
#define GPCP_OP_MAX   11u
#define GPCP_OP_MIN   12u
#define GPCP_OP_MACL  13u

#define GPCP_SRC_R0   0u
#define GPCP_SRC_N    8u
#define GPCP_SRC_E    9u
#define GPCP_SRC_S    10u
#define GPCP_SRC_W    11u
#define GPCP_SRC_ZERO 12u
#define GPCP_SRC_IMM  13u
#define GPCP_SRC_ACC  14u
#define GPCP_SRC_LINK 15u

static inline uint32_t gpcp_cgra_inst(uint32_t op, uint32_t src_a, uint32_t src_b,
                                      uint32_t dst, uint32_t imm)
{
    return ((op & 0xFu) << 27) | ((src_a & 0xFu) << 23) |
           ((src_b & 0xFu) << 19) | ((dst & 0x7u) << 16) | (imm & 0xFFFFu);
}

static inline uint32_t gpcp_cgra_pe_reg_addr(uint32_t pe, uint32_t reg)
{
    return GPCP_CGRA_PE_BASE + pe * 32u + reg * 4u;
}

/* ---------------------------------------------------------------- MMIO access */
static inline void gpcp_cgra_write(uintptr_t base, uint32_t off, uint32_t val)
{
    *(volatile uint32_t *)(base + off) = val;
}

static inline uint32_t gpcp_cgra_read(uintptr_t base, uint32_t off)
{
    return *(volatile uint32_t *)(base + off);
}

/* ------------------------------------------------------------ task descriptor */
typedef struct {
    const uint32_t *prog;       /* cycle*NPES words                              */
    uint32_t        cycles;     /* 1 .. GPCP_CGRA_MAX_CYCLES                     */
    const uint32_t *preload;    /* pairs {word_index, value}, word_index = pe*8+r */
    uint32_t        preload_len;
} gpcp_cgra_task_t;

/* Load program + preload into the CGRA and start it (polling completion). */
static inline int gpcp_cgra_run(uintptr_t base, const gpcp_cgra_task_t *task)
{
    uint32_t i;
    if (!task || task->cycles == 0u || task->cycles > GPCP_CGRA_MAX_CYCLES)
        return -1;

    gpcp_cgra_write(base, GPCP_CGRA_CTRL, GPCP_CGRA_CTRL_CLRDONE);
    for (i = 0; i < task->cycles * GPCP_CGRA_NPES; i++)
        gpcp_cgra_write(base, GPCP_CGRA_IMEM_BASE + i * 4u, task->prog[i]);

    for (i = 0; i < task->preload_len; i++) {
        uint32_t idx = task->preload[2 * i];
        uint32_t val = task->preload[2 * i + 1];
        if (idx < GPCP_CGRA_NPES * 8u)
            gpcp_cgra_write(base, GPCP_CGRA_PE_BASE + idx * 4u, val);
    }

    gpcp_cgra_write(base, GPCP_CGRA_NUM_CYCLES, task->cycles);
    gpcp_cgra_write(base, GPCP_CGRA_CTRL, GPCP_CGRA_CTRL_START);

    while (!(gpcp_cgra_read(base, GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE))
        ; /* M1: replace with an interrupt-driven wait */

    return 0;
}

static inline uint32_t gpcp_cgra_pe_acc(uintptr_t base, uint32_t pe)
{
    return gpcp_cgra_read(base, GPCP_CGRA_PE_ACC + pe * 4u);
}

#endif /* GPCP_CGRA_H */
