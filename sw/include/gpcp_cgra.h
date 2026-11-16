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

#ifdef GPCP_CPU_PROFILE
#include "perf.h"                       /* CPU 子账本：只累计阵列/DMA 都空闲的周期 */
#endif

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
#define GPCP_CGRA_SPAD_BASE    0xA000u   /* 0xA000 + bank*128 + word*4 (64x32) */

/* ----------------------------------------------------- copy engine (DMA) */
#define GPCP_CGRA_DMA_SRC      0x0010u   /* W: source byte address (SoC RAM)   */
#define GPCP_CGRA_DMA_DST      0x0014u   /* W: spad index (bank*32+word)       */
#define GPCP_CGRA_DMA_LEN      0x0018u   /* W: number of 32-bit words (<=1024) */
#define GPCP_CGRA_DMA_CTRL     0x001Cu   /* W: [0] start [1] dir(1=spad->RAM)
                                                 [2] clear done
                                            R: [0] busy [1] done            */
#define GPCP_CGRA_BUF_PAGE     0x0020u   /* W: array page select (0/1)         */
#define GPCP_CGRA_DMA_PAGE     0x0024u   /* W: copy-engine page select (0/1)   */
#define GPCP_CGRA_DMA_STRIDE   0x0028u   /* W: 2D copy, spad stride between rows */
#define GPCP_CGRA_DMA_ROW      0x002Cu   /* W: 2D copy, words per row (0 = linear) */

#define GPCP_CGRA_DMA_START    (1u << 0)
#define GPCP_CGRA_DMA_DIR_OUT  (1u << 1)   /* spad -> RAM */
#define GPCP_CGRA_DMA_CLRDONE  (1u << 2)
#define GPCP_CGRA_DMA_TGT_IMEM (1u << 3)   /* 目标改成 IMEM（批量装程序） */

#define GPCP_CGRA_DMA_BUSY     (1u << 0)
#define GPCP_CGRA_DMA_DONE     (1u << 1)

/* ------------------------------------------------------ array run (one store) */
/* A run needs the cycle count, the scratchpad page and the start pulse.  All
 * three travel in a single 32-bit store to NUM_CYCLES: [31] start, [16] page,
 * [15:0] cycles.  This is deliberate - the CPU's write buffer (CVA6 WTDU)
 * folds two 32-bit stores inside the same 8-byte word into one 64-bit AXI
 * write, which silently drops one of them; writing CTRL twice (clear-done then
 * start) or NUM_CYCLES-then-CTRL is therefore unsafe without a fence. */
#define GPCP_CGRA_RUN_START  (1u << 31)
#define GPCP_CGRA_RUN_PAGE1  (1u << 16)

/* ------------------------------------------------------- completion interrupt */
#define GPCP_CGRA_IRQ_STATUS   0x0040u   /* R: [0] array done [1] dma done  */
                                         /* W: write 1 to clear (W1C)       */
#define GPCP_CGRA_IRQ_ENABLE   0x0044u   /* R/W: same bit layout            */
#define GPCP_CGRA_IRQ_ARR      (1u << 0)
#define GPCP_CGRA_IRQ_DMA      (1u << 1)

/* ------------------------------------------------------------ descriptor ring */
#define GPCP_CGRA_RING_BASE    0x0048u   /* W/R: byte address of descriptor 0 */
#define GPCP_CGRA_RING_SIZE    0x004Cu   /* W/R: ring depth (power of two)    */
#define GPCP_CGRA_RING_HEAD    0x0050u   /* R: next descriptor the engine fetches */
#define GPCP_CGRA_RING_TAIL    0x0054u   /* R/W: one past the last valid entry */
#define GPCP_CGRA_RING_CTRL    0x0058u   /* W: [0] enable                     */
                                         /* R: [0] enabled [1] idle [2] busy  */
#define GPCP_CGRA_RING_DONE    0x005Cu   /* R: descriptors completed so far   */
#define GPCP_CGRA_DESC_MODE    0x0064u   /* W: [0] 1 = 64B 复合描述符 */
#define GPCP_CGRA_WIN_STATUS   0x0070u   /* R: [0] 输出写窗口越界（sticky）
                                             W: [0] 写 1 清除                   */
#define GPCP_CGRA_ROT_ZC       0x006Cu   /* W/R: 循环旋转的模（0 视为 32） */
#define GPCP_CGRA_ROT_BASE     0x0080u   /* W/R: 0x80 + bank*4，每 bank 7 bit 表项 */
#define GPCP_CGRA_DBG_STATE    0x0060u   /* R: engine FSM state (debug)       */
#define GPCP_CGRA_RING_EN      (1u << 0)
#define GPCP_CGRA_RING_IDLE    (1u << 1)
#define GPCP_CGRA_RING_BUSY    (1u << 2)

/* 32-byte descriptor (8-byte aligned, engine fetches 4 beats):
 *   +0x00 src (u32, 8-byte aligned)      +0x04 dst | page<<16 | dir<<17 |
 *   +0x08 len (u16 words)                       irq<<18
 *   +0x0C stride (u16) | row<<16         +0x10 status (engine writes 0xC0DE0000|len)
 *   +0x14..+0x1F reserved
 * 字段位定义只在 rtl/cgra/cgra_desc64.yml；软件一律用 gpcp_desc64.h 的
 * gpcp_d64_set_*（32B 描述符就是 64B 描述符的前 4 个字），不要再加裸位移宏。 */
#define GPCP_DMA_DESC_BYTES   32u
#define GPCP_DMA_DESC_MAGIC   0xC0DE0000u

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
#define GPCP_OP_LD    14u   /* dst = spad[imm[9:0]]  ({bank[4:0],word[4:0]}) */
#define GPCP_OP_ST    15u   /* spad[imm[9:0]] = a                          */

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

static inline void gpcp_cgra_array_start(uintptr_t base, uint32_t page, uint32_t cycles)
{
    gpcp_cgra_write(base, GPCP_CGRA_NUM_CYCLES,
                    (cycles & 0xFFFFu) | GPCP_CGRA_RUN_START |
                    (page ? GPCP_CGRA_RUN_PAGE1 : 0u));
}

/* ---------------------------------------------------------- descriptor ring */
static inline void gpcp_cgra_ring_start(uintptr_t base, uintptr_t ring_addr,
                                        uint32_t depth, uint32_t tail)
{
    gpcp_cgra_write(base, GPCP_CGRA_RING_BASE, (uint32_t)ring_addr);
    gpcp_cgra_write(base, GPCP_CGRA_RING_SIZE, depth);
    gpcp_cgra_write(base, GPCP_CGRA_RING_HEAD, 0);
    gpcp_cgra_write(base, GPCP_CGRA_RING_TAIL, tail);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");   /* tail before enable */
    gpcp_cgra_write(base, GPCP_CGRA_RING_CTRL, GPCP_CGRA_RING_EN);
}

/* push more work: the engine picks it up as soon as it runs out of descriptors */
static inline void gpcp_cgra_ring_push(uintptr_t base, uint32_t tail)
{
    gpcp_cgra_write(base, GPCP_CGRA_RING_TAIL, tail);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
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

/* ------------------------------------------------------- copy engine (DMA) */
/* Program one descriptor and start it.  The descriptor is latched on the
 * start write, so the earlier descriptor stores must reach the CGRA first:
 * with a posted store buffer (CVA6 WTDU) the start write can otherwise
 * overtake them, e.g. leaving ROW at 0 and turning a 2D copy into a linear
 * one.  The `fence` makes this explicit. */
static inline void gpcp_cgra_dma_start(uintptr_t base, uintptr_t ram_addr,
                                       uint32_t spad_idx, uint32_t words,
                                       uint32_t page, uint32_t dir_out,
                                       uint32_t stride, uint32_t row)
{
    gpcp_cgra_write(base, GPCP_CGRA_DMA_SRC, (uint32_t)ram_addr);
    gpcp_cgra_write(base, GPCP_CGRA_DMA_DST, spad_idx);
    gpcp_cgra_write(base, GPCP_CGRA_DMA_LEN, words);
    gpcp_cgra_write(base, GPCP_CGRA_DMA_STRIDE, stride);
    gpcp_cgra_write(base, GPCP_CGRA_DMA_ROW, row);
    gpcp_cgra_write(base, GPCP_CGRA_DMA_PAGE, page);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    gpcp_cgra_write(base, GPCP_CGRA_DMA_CTRL,
                    GPCP_CGRA_DMA_START | (dir_out ? GPCP_CGRA_DMA_DIR_OUT : 0u));
}

/* ------------------------------------------------------ IMEM bulk load (DMA) */
/* 把 RAM 里的程序直接打进指令存储器：拷贝引擎以 8B/beat 读 RAM，一个 beat
 * 写两条指令（word index = 0..8191，行 = idx>>4、lane = idx&15）。
 * 一次最多 1024 字（DMA_LEN 上限），超过的分块；每块都要等 done + 清 done。
 * 相比逐字 AXI-Lite 写（~4 拍/字），这是 2 字/beat 的突发，快一个数量级。 */
static inline int gpcp_cgra_imem_load_body(uintptr_t base, const uint32_t *prog,
                                           uint32_t words)
{
    uint32_t off = 0;
    if (!prog || words == 0u || words > GPCP_CGRA_IMEM_WORDS)
        return -1;
    while (off < words) {
        uint32_t n = words - off;
        uint32_t guard = 0;
        if (n > 1024u)
            n = 1024u;
        while (gpcp_cgra_read(base, GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_BUSY)
            if (++guard > 100000u)
                return -2;
        gpcp_cgra_write(base, GPCP_CGRA_DMA_SRC,
                        (uint32_t)(uintptr_t)(prog + off));
        gpcp_cgra_write(base, GPCP_CGRA_DMA_DST, off);      /* IMEM word index */
        gpcp_cgra_write(base, GPCP_CGRA_DMA_LEN, n);
        gpcp_cgra_write(base, GPCP_CGRA_DMA_STRIDE, 0);
        gpcp_cgra_write(base, GPCP_CGRA_DMA_ROW, 0);
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        gpcp_cgra_write(base, GPCP_CGRA_DMA_CTRL,
                        GPCP_CGRA_DMA_START | GPCP_CGRA_DMA_TGT_IMEM);
        guard = 0;
        while (!(gpcp_cgra_read(base, GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_DONE))
            if (++guard > 100000u)
                return -3;
        gpcp_cgra_write(base, GPCP_CGRA_DMA_CTRL, GPCP_CGRA_DMA_CLRDONE);
        off += n;
    }
    return 0;
}

static inline int gpcp_cgra_imem_load(uintptr_t base, const uint32_t *prog,
                                      uint32_t words)
{
#ifdef GPCP_CPU_PROFILE
    uint32_t t0 = cpu_prof_now();
    int rc = gpcp_cgra_imem_load_body(base, prog, words);
    g_cpu_prof.imem += cpu_prof_now() - t0;
    return rc;
#else
    return gpcp_cgra_imem_load_body(base, prog, words);
#endif
}

/* -------------------------------------------------- completion interrupt (CGRA) */
/* The CGRA raises a level interrupt while STATUS has an enabled bit set; clear
 * it with a W1C write after claiming the source at the PLIC (claim first, then
 * clear, otherwise the level may drop before the claim sees it). */
static inline void gpcp_cgra_irq_enable(uintptr_t base, uint32_t mask)
{
    gpcp_cgra_write(base, GPCP_CGRA_IRQ_ENABLE, mask);
}

static inline uint32_t gpcp_cgra_irq_status(uintptr_t base)
{
    return gpcp_cgra_read(base, GPCP_CGRA_IRQ_STATUS);
}

static inline void gpcp_cgra_irq_clear(uintptr_t base, uint32_t mask)
{
    gpcp_cgra_write(base, GPCP_CGRA_IRQ_STATUS, mask);
}

/* scratchpad helpers -- geometry comes from the compiler-generated
 * gpcp_layout.h (cgra/target.yaml).  Do not hard-code words-per-bank here. */
#include "gpcp_layout.h"
#define GPCP_SPAD_FLAT_IDX(flat)    (flat)
#define GPCP_SPAD_FLAT_ADDR(flat)   (GPCP_CGRA_SPAD_BASE + ((flat) * 4u))
#define GPCP_SPAD_IDX(bank, word)   ((((bank) & GPCP_LAYOUT_IDX_BANK_MASK) << GPCP_LAYOUT_WORD_BITS) | ((word) & GPCP_LAYOUT_WORD_MASK))
#define GPCP_SPAD_ADDR(bank, word)  (GPCP_CGRA_SPAD_BASE + ((bank) * (GPCP_LAYOUT_WORDS * 4u)) + ((word) * 4u))
#define GPCP_SPAD_PAGE(bank)        (((bank) >> GPCP_LAYOUT_WORD_BITS) & 1u)

#endif /* GPCP_CGRA_H */
