/* -----------------------------------------------------------------------------
 * CGRA 性能计数器（rtl/cgra/cgra_top.sv 的 REG_PERF_*）。
 *
 * 软件写 PERF_CTRL[0] 清零；读 PERF_BASE + 4*idx。
 * 计数宽度 32 bit，按 100 MHz / 40M 周期上限设计（> 400 s 才会回卷）。
 * -------------------------------------------------------------------------- */
#ifndef GPCP_PERF_H
#define GPCP_PERF_H

#include "soc_io.h"

#define CGRA_PERF_CTRL   0x50000068ul
#define CGRA_PERF_BASE   0x50000100ul
#define CGRA_PERF_N      64

/* 计数器索引（与 cgra_top.sv 的 perf_counters 映射一致）。 */
#define PERF_CYCLES              0
#define PERF_ARRAY_BUSY          1
#define PERF_ARRAY_RUNS          2
#define PERF_INST_ISSUED         3
#define PERF_INST_NOP            4
#define PERF_INST_MAC            5
#define PERF_INST_MUL            6
#define PERF_INST_LD             7
#define PERF_INST_ST             8
#define PERF_INST_ALU            9
#define PERF_SPAD_READ           10
#define PERF_SPAD_WRITE          11
#define PERF_SPAD_BANK_CONFLICT  12
#define PERF_SPAD_WORD_CONFLICT  13
#define PERF_DMA_BUSY            14
#define PERF_DMA_BEATS           15
#define PERF_DMA_AR_WAIT         16
#define PERF_DMA_AW_WAIT         17
#define PERF_DMA_B_WAIT          18
#define PERF_DMA_DATA_WAIT       19
#define PERF_DMA_STARTS          20
#define PERF_RING_DONE           21
/* 互斥状态账本（每拍只归一类，54+55+56 = PERF_CYCLES）：
 *   阵列忙 = PERF_ARRAY_BUSY；DMA = 54；DMA 等待 = 55（54 的子集）；CPU = 56。 */
#define PERF_STATE_DMA           54
#define PERF_STATE_DMA_WAIT      55
#define PERF_STATE_CPU           56
#define PERF_PE_ACTIVE0          22
#define PERF_PE_MAC0             38

static inline void perf_clear(void)
{
    mmio_w(CGRA_PERF_CTRL, 1u);
}

static inline uint32_t perf_read(uint32_t idx)
{
    return mmio_r(CGRA_PERF_BASE + 4u * idx);
}

/* CPU 子账本（仅 proto 运行器编译时开启 GPCP_CPU_PROFILE）。
 * 计时口径：区间内 PERF_STATE_CPU 的增量——只累计"阵列与 DMA 都空闲"的周期，
 * 因此与轮询等待（那时阵列/DMA 忙）天然不重叠，各项相加不会超过 CPU 状态。 */
#ifdef GPCP_CPU_PROFILE
typedef struct {
    uint32_t imem;       /* 指令存储器装载（寄存器配置部分） */
    uint32_t array_cfg;  /* 阵列启动/页选择写 */
    uint32_t dma_cfg;    /* DMA 描述符启动写 */
    uint32_t cache;      /* D-cache 维护 */
} cpu_prof_t;

static cpu_prof_t g_cpu_prof;

static inline uint32_t cpu_prof_now(void)
{
    return perf_read(PERF_STATE_CPU);
}
#endif

#endif /* GPCP_PERF_H */
