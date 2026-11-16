/* -----------------------------------------------------------------------------
 * docs/protocols/README.md 第 5 步：协议基准的通用运行器。
 *
 * 它**不含协议算法**：阶段表由 proto/emit.py 从 ir.json + plan-base.json 生成
 * （build/proto/<scenario>/plan.h），每个阶段的实现按 op 从 proto_ops[] 里取。
 * 算子库与锚点块按场景放在 sw/soc/<scenario>_ops.inc（编译期由
 * -DPROTO_OPS_INC 选择），阶段实现本身与手写 benchmark 共用同一个 .inc
 * ——所以"与手写基准逐值一致"是有意义的判据，不是把 C 复制一份糊过去。
 *
 * 统一结果区（docs/protocols/README.md）：
 *   [0] 0x50524F54  [1] errors   [2] cycles  [3] array_runs  [4] dma
 *   [5] busy        [6] irq      [7] overrun [8] params_hash [9] 0x600D
 *   [10 .. 10+3N-1] 每阶段 (id, cycles, 该阶段的阵列运行增量)
 *   [10+3N ..]      场景锚点块（<scenario>_ops.inc 的 write_anchors）
 * ----------------------------------------------------------------------------- */
#define GPCP_CPU_PROFILE 1                /* 打开 CPU 子账本（perf.h / rt 头文件） */
#include "../../include/gpcp_cgra.h"
#include "../../include/soc_io.h"
#include "../../include/gpcp_cgra_rt.h"      /* rt_array_run / rt_dcache_flush */
#include "../../include/perf.h"              /* CGRA 性能计数器 */

#include "plan.h"                         /* 生成物：阶段表 + 预算 + 参数摘要 */

/* 场景算子库：含各 *_prog.h、阶段实现、proto_ops[] 与 write_anchors()。
 * 运行器只认识 proto_op_t / proto_stages[] 这两个契约。 */
#ifndef PROTO_OPS_INC
#error "编译 proto_runner.c 必须给出 -DPROTO_OPS_INC='\"<scenario>_ops.inc\"'"
#endif

typedef int (*proto_op_fn)(void);
typedef struct {
    const char *op;
    const char *impl;      /* NULL = 该动词只有一种实现 */
    proto_op_fn fn;
} proto_op_t;

#define PS_TRACK_STATS 1                  /* 让阶段实现统计 busy / DMA */
#include PROTO_OPS_INC

static int same(const char *a, const char *b)
{
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

/* 性能计数块（结果区固定偏移，见 docs/protocols/README.md）：
 *   [1200] magic [1201] 阶段数 [1202] 每阶段键数
 *   每阶段 17 字：id + 16 个增量（9 个硬件计数 + 3 个互斥状态 + 4 个 CPU 子账本）
 *   [1400] magic [1401] 计数器个数 [1402..] 整跑快照
 */
#define PERF_STAGE_WORD 1200u
#define PERF_FULL_WORD  1400u
#define PERF_MAGIC      0x50455246u      /* 'PERF' */
#define PERF_KEYS       16u

static uint32_t perf_dma_wait(void)
{
    return perf_read(PERF_DMA_AR_WAIT) + perf_read(PERF_DMA_AW_WAIT)
         + perf_read(PERF_DMA_B_WAIT) + perf_read(PERF_DMA_DATA_WAIT);
}

/* 16 个增量：前 9 个是硬件计数，接着 3 个互斥状态，最后 4 个 CPU 子账本。
 * 账本恒等式：阵列忙(0) + DMA(9) + CPU(11) = 本阶段墙钟周期；
 *             0 + 9 + 11 = perf_cycles，CPU 子账本之和 ≤ 11。 */
static void perf_snapshot(uint32_t *v)
{
    v[0]  = perf_read(PERF_ARRAY_BUSY);
    v[1]  = perf_read(PERF_INST_ISSUED);
    v[2]  = perf_read(PERF_INST_NOP);
    v[3]  = perf_read(PERF_INST_MAC);
    v[4]  = perf_read(PERF_SPAD_READ);
    v[5]  = perf_read(PERF_SPAD_WRITE);
    v[6]  = perf_read(PERF_SPAD_BANK_CONFLICT);
    v[7]  = perf_read(PERF_DMA_BEATS);
    v[8]  = perf_dma_wait();
    v[9]  = perf_read(PERF_STATE_DMA);
    v[10] = perf_read(PERF_STATE_DMA_WAIT);
    v[11] = perf_read(PERF_STATE_CPU);
    v[12] = g_cpu_prof.imem;
    v[13] = g_cpu_prof.array_cfg;
    v[14] = g_cpu_prof.dma_cfg;
    v[15] = g_cpu_prof.cache;
}

int main(void)
{
    uint32_t *out = RESULT_BASE;
    const uint32_t *par = (const uint32_t *)PARAM_BASE;
    uint32_t t0, i, bad = 0;

    if (proto_setup(par) != 0) {
        out[0] = 0x0BADC4B1u;
        out[9] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        mmio_w(EXIT_ADDR, 0x600Du);
        return 0;
    }

    perf_clear();
    out[PERF_STAGE_WORD + 0] = PERF_MAGIC;
    out[PERF_STAGE_WORD + 1] = PROTO_N_STAGES;
    out[PERF_STAGE_WORD + 2] = PERF_KEYS;

    {
        uint32_t runs_at_stage0 = runs;   /* setup 里的阵列运行不计入阶段账本 */
        t0 = mtime();
        for (i = 0; i < PROTO_N_STAGES; i++) {
            const proto_stage_t *st = &proto_stages[i];
            proto_op_fn fn = 0;
            uint32_t c0, r0, k, p0[PERF_KEYS], p1[PERF_KEYS];

            for (k = 0; k < N_OPS; k++)
                if (same(proto_ops[k].op, st->op)
                    && (!proto_ops[k].impl || same(proto_ops[k].impl, st->impl))) {
                    fn = proto_ops[k].fn;
                    break;
                }
            perf_snapshot(p0);
            c0 = mtime();
            r0 = runs;
            if (!fn || fn() != 0)
                bad++;
            out[10 + 3 * i + 0] = st->id;
            out[10 + 3 * i + 1] = mtime() - c0;
            out[10 + 3 * i + 2] = runs - r0;
            perf_snapshot(p1);
            out[PERF_STAGE_WORD + 3 + i * (PERF_KEYS + 1u)] = st->id;
            for (k = 0; k < PERF_KEYS; k++)
                out[PERF_STAGE_WORD + 4 + i * (PERF_KEYS + 1u) + k] = p1[k] - p0[k];
        }

        write_anchors(out + 10 + 3 * PROTO_N_STAGES);

        out[PERF_FULL_WORD + 0] = PERF_MAGIC;
        out[PERF_FULL_WORD + 1] = CGRA_PERF_N;
        for (i = 0; i < CGRA_PERF_N; i++)
            out[PERF_FULL_WORD + 2 + i] = perf_read(i);

        out[0] = 0x50524F54u;               /* 'PROT' */
        out[1] = errs + bad;
        out[2] = mtime() - t0;
        out[3] = runs - runs_at_stage0;     /* 只统计阶段内的阵列运行 */
        out[4] = PROTO_DMA_FIELD;
        out[5] = busy;
        out[6] = 0;                         /* 轮询，不用中断 */
        out[7] = (PROTO_BUDGET_CYCLES && out[2] > PROTO_BUDGET_CYCLES) ? 1u : 0u;
        out[8] = PROTO_PARAMS_HASH;
        out[9] = 0x600Du;
    }

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    mmio_w(EXIT_ADDR, 0x600Du);
    return 0;
}
