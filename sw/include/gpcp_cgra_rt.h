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

/* 数据缓存全失效（"引擎写 RAM → CPU 读"必须调用）。
 *
 * CVA6 的 D$ 是 WT（写穿）：CPU 写会同时落到 RAM，所以"CPU 写 → 引擎读"天然
 * 一致；但引擎直接写 RAM **不会**让 D$ 失效，CPU 再读同一行会命中缓存里的旧
 * 值。本配置 RVZiCbom=0（没有 cbo.inval），DcacheFlushOnFence=0（fence 不刷），
 * 但 CSR_DCACHE(0x7c1) 的使能位有一个可用语义：WT missunit 里"关闭立即生效、
 * 重新使能要走 FLUSH 流程"（`enable_i && !enable_q` → FLUSH/DRAIN）。
 * 所以把使能位写 0 再写 1 = 排空写缓冲 + 整片 D$ 失效（32 KiB 约 4k 拍）。 */
static inline void rt_dcache_flush(void)
{
#ifdef GPCP_CPU_PROFILE
    uint32_t t0 = cpu_prof_now();
#endif
    uint64_t on = 1;
    __asm__ __volatile__("csrw 0x7c1, zero" ::: "memory");
    __asm__ __volatile__("csrw 0x7c1, %0" :: "r"(on) : "memory");
    __asm__ __volatile__("fence" ::: "memory");
#ifdef GPCP_CPU_PROFILE
    g_cpu_prof.cache += cpu_prof_now() - t0;
#endif
}

/* 启动阵列（选页）并等它跑完 */
static inline int rt_array_run(uintptr_t base, uint32_t page, uint32_t cycles)
{
#ifdef GPCP_CPU_PROFILE
    uint32_t t0 = cpu_prof_now();
#endif
    gpcp_cgra_array_start(base, page, cycles);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
#ifdef GPCP_CPU_PROFILE
    g_cpu_prof.array_cfg += cpu_prof_now() - t0;
#endif
    return rt_wait_array(base);
}

/* 发起一次搬运（不等待）。写完 start 后还要确认 start 真的生效
 * （BUSY 置位 或 done 被清掉），否则调用方无法区分"旧的 done"和"这次的完成"。 */
static inline int rt_dma_begin(uintptr_t base, uintptr_t ram, uint32_t spad_idx,
                               uint32_t words, uint32_t page, uint32_t dir_out,
                               uint32_t stride, uint32_t row)
{
#ifdef GPCP_CPU_PROFILE
    uint32_t t0 = cpu_prof_now();
#endif
    int rc = 0;
    uint32_t g = 0;
    while (gpcp_cgra_read(base, GPCP_CGRA_DMA_CTRL) & GPCP_CGRA_DMA_BUSY) {
        if (++g > RT_SPIN_LIMIT) {
            rc = -1;
            goto out;
        }
    }
    gpcp_cgra_dma_start(base, ram, spad_idx, words, page, dir_out, stride, row);
    g = 0;
    while (1) {
        uint32_t s = gpcp_cgra_read(base, GPCP_CGRA_DMA_CTRL);
        if ((s & GPCP_CGRA_DMA_BUSY) || !(s & GPCP_CGRA_DMA_DONE))
            break;                          /* start 已生效（或已瞬间完成） */
        if (++g > 4096u) {
            rc = -1;
            break;
        }
    }
out:
#ifdef GPCP_CPU_PROFILE
    g_cpu_prof.dma_cfg += cpu_prof_now() - t0;
#endif
    return rc;
}

/* 等搬运完成。此时"done=1 且 busy=0"就意味着这次传输已经结束
 * （不可能再是上一次遗留的 done，因为 begin 已经确认 start 生效）。 */
static inline int rt_dma_wait(uintptr_t base)
{
    uint32_t g = 0;
    uint32_t s = gpcp_cgra_read(base, GPCP_CGRA_DMA_CTRL);
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
