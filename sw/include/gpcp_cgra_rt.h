/* -----------------------------------------------------------------------------
 * GPCP CGRA M1 运行时骨架：spad 双页 ping-pong + 拷贝引擎/阵列重叠
 *
 * 约定（来自 docs/07 §4 与 AGENTS §4 的硬规矩）：
 *   - 阵列用页 A（BUF_PAGE），拷贝引擎同时只能写/读**另一个**页（DMA_PAGE）；
 *   - 阵列是静态调度，`RT_MAX_CYCLES` 拍后自己停；启动后必须"先看到 BUSY 或 DONE，
 *     再等 BUSY 清零"（start 是 posted write，第一次轮询可能读到旧的 idle）；
 *   - 拷贝引擎单 outstanding：同一时刻只能有一个传输，但可以和阵列并行。
 *
 * 典型用法（逐行处理，行数 > 页容量时循环使用两页）：
 *
 *     rt_dma_begin(base, src(0), 0, n, page(0), 0, stride, row);   // 预取第 0 行
 *     rt_dma_wait(base);
 *     for (r = 0; r < rows; r++) {
 *         if (r + 1 < rows) rt_dma_begin(base, src(r+1), 0, n, (r+1)&1, 0, stride, row);
 *         rt_array_run(base, r & 1, cycles);       // 与预取并行
 *         if (r + 1 < rows) rt_dma_wait(base);     // 预取若已完即时返回
 *         rt_dma_begin(base, dst(r), spad_out, m, (r)&1, 1, stride, row);
 *         rt_dma_wait(base);
 *     }
 * ----------------------------------------------------------------------------- */
#ifndef GPCP_CGRA_RT_H
#define GPCP_CGRA_RT_H

#include "../include/gpcp_cgra.h"

#define RT_SPIN_LIMIT  200000u

/* 等待阵列完成（start 是 posted write：先等 start 生效，再等 BUSY 清零） */
static inline int rt_wait_array(uintptr_t base)
{
    uint32_t g = 0;
    uint32_t s = gpcp_cgra_read(base, GPCP_CGRA_STATUS);
    while (!(s & (GPCP_CGRA_STATUS_BUSY | GPCP_CGRA_STATUS_DONE))) {
        if (++g > RT_SPIN_LIMIT)
            return -1;
        s = gpcp_cgra_read(base, GPCP_CGRA_STATUS);
    }
    g = 0;
    while (s & GPCP_CGRA_STATUS_BUSY) {
        if (++g > RT_SPIN_LIMIT)
            return -1;
        s = gpcp_cgra_read(base, GPCP_CGRA_STATUS);
    }
    return 0;
}

/* 启动阵列（选页）并等它跑完 */
static inline int rt_array_run(uintptr_t base, uint32_t page, uint32_t cycles)
{
    gpcp_cgra_array_start(base, page, cycles);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    return rt_wait_array(base);
}

/* 发起一次搬运（不等待）；spad 索引 / 页由调用者选，保证与阵列页不同 */
static inline int rt_dma_begin(uintptr_t base, uintptr_t ram, uint32_t spad_idx,
                               uint32_t words, uint32_t page, uint32_t dir_out,
                               uint32_t stride, uint32_t row)
{
    uint32_t g = 0;
    while (gpcp_cgra_read(base, GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_BUSY) {
        if (++g > RT_SPIN_LIMIT)
            return -1;
    }
    gpcp_cgra_dma_start(base, ram, spad_idx, words, page, dir_out, stride, row);
    return 0;
}

/* 等上一次搬运完成。注意 start 由 RTL 清 done，所以先等"旧 done 消失（start 生效）"，
 * 再等 busy 清零 —— 这样既不用额外写 CLRDONE，也不会被上一次遗留的 done 骗过。 */
static inline int rt_dma_wait(uintptr_t base)
{
    uint32_t g = 0;
    uint32_t s = gpcp_cgra_read(base, GPCP_CGRA_DMA_CTRL);
    while ((s & (GPCP_CGRA_DMA_BUSY | GPCP_CGRA_DMA_DONE)) == GPCP_CGRA_DMA_DONE) {
        if (++g > RT_SPIN_LIMIT)
            return -1;
        s = gpcp_cgra_read(base, GPCP_CGRA_DMA_CTRL);
    }
    g = 0;
    while (s & GPCP_CGRA_DMA_BUSY) {
        if (++g > RT_SPIN_LIMIT)
            return -1;
        s = gpcp_cgra_read(base, GPCP_CGRA_DMA_CTRL);
    }
    return 0;
}

/* 一次完整的"发起 + 等完"（预取/搬出都可用） */
static inline int rt_dma_run(uintptr_t base, uintptr_t ram, uint32_t spad_idx,
                             uint32_t words, uint32_t page, uint32_t dir_out,
                             uint32_t stride, uint32_t row)
{
    if (rt_dma_begin(base, ram, spad_idx, words, page, dir_out, stride, row))
        return -1;
    return rt_dma_wait(base);
}

#endif
