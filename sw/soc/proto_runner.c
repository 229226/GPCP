/* -----------------------------------------------------------------------------
 * docs/20 §6 第 5 步：协议基准的通用运行器。
 *
 * 它**不含协议算法**：阶段表由 proto/emit.py 从 ir.json + plan-base.json 生成
 * （build/proto/<scenario>/plan.h），每个阶段的实现按 op 从 proto_ops[] 里取。
 * 阶段实现在 sw/soc/pdcch_stages.inc，与手写 benchmark sw/soc/pdcch_loop_bm.c
 * **共用同一份代码**——所以"与 run-pdcch-loop 逐值一致"是一个有意义的判据，
 * 不是把 C 复制一份糊过去（docs/20 §8 规则 4）。
 *
 * 统一结果区（docs/20 §4）：
 *   [0] 0x50524F54  [1] errors   [2] cycles  [3] array_runs  [4] dma
 *   [5] busy        [6] irq      [7] overrun [8] params_hash [9] 0x600D
 *   [10 .. 10+3N-1] 每阶段 (id, cycles, 该阶段的阵列运行增量)
 *   [10+3N ..]      场景锚点（本文件末尾的 write_anchors：协议上应得的那些值）
 *
 * 锚点块是"场景的结果适配器"，与阶段实现一一对应；换场景时在这里加一个
 * 适配器（阶段表本身不用改）。
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"      /* rt_array_run / rt_dcache_flush */

#include "polar_inword_s0_prog.h"
#include "polar_inword_s1_prog.h"
#include "polar_inword_s2_prog.h"
#include "polar_inword_s3_prog.h"
#include "polar_inword_s4_prog.h"
#include "polar_word_d1_prog.h"
#include "polar_word_d2_prog.h"
#include "polar_word_d4_prog.h"
#include "polar_word_d8_prog.h"
#include "polar_node_prog.h"
#include "pdcch_luts.h"                   /* E=432 逆速率匹配 */

#include "plan.h"                         /* 生成物：阶段表 + 预算 + 参数摘要 */

#define PS_TRACK_STATS 1                  /* 让 .inc 统计 busy / dma（仅运行器开） */
#include "pdcch_stages.inc"

typedef int (*proto_op_fn)(void);
typedef struct {
    const char *op;
    proto_op_fn fn;
} proto_op_t;

/* 动词 -> 实现。新增场景时在这里登记它的阶段实现（docs/20 §2 的动词名）。 */
static const proto_op_t proto_ops[] = {
    { "transport_block",  ps_transport_block  },
    { "encode",           ps_encode           },
    { "modulate",         ps_modulate         },
    { "dmrs",             ps_dmrs             },
    { "map_to_grid",      ps_map_to_grid      },
    { "ofdm_modulate",    ps_ofdm_modulate    },
    { "channel",          ps_channel          },
    { "estimate",         ps_estimate         },
    { "equalise_demap",   ps_equalise_demap   },
    { "decode",           ps_decode           },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

static int same(const char *a, const char *b)
{
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

/* 锚点块（场景结果适配器）：协议上应得的那些值，供 proto_eval.py 逐项对账。 */
static void write_anchors(uint32_t *o)
{
    uint32_t i;
    o[0] = ps_dci_word;          /* 发射 DCI */
    o[1] = ps_dci_rx_word;       /* 译码 DCI */
    o[2] = ps_rnti;              /* 发射 RNTI */
    o[3] = ps_rnti_match;        /* 盲检命中 */
    o[4] = ps_crc_ok;
    o[5] = ps_llr_bad;
    o[6] = ps_llr_amin;
    o[7] = ps_llr_amax;
    o[8] = ps_n_id;
    o[9] = ps_max_sym;
    for (i = 0; i < A_DCI; i++)
        o[10 + i] = dci_bits[i];
    for (i = 0; i < A_DCI; i++)
        o[10 + A_DCI + i] = rx_info[i];
    for (i = 0; i < CRC_LEN; i++)
        o[10 + 2 * A_DCI + i] = rx_crc[i];
    for (i = 0; i < E_BITS; i++)
        o[10 + 2 * A_DCI + CRC_LEN + i] = e_sc[i];
}

int main(void)
{
    uint32_t *out = RESULT_BASE;
    const uint32_t *par = (const uint32_t *)PARAM_BASE;
    uint32_t t0, i, bad = 0;

    if (ps_setup(par) != 0) {
        out[0] = 0x0BADC4B1u;
        out[9] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        mmio_w(EXIT_ADDR, 0x600Du);
        return 0;
    }

    t0 = mtime();
    for (i = 0; i < PROTO_N_STAGES; i++) {
        const proto_stage_t *st = &proto_stages[i];
        proto_op_fn fn = 0;
        uint32_t c0, r0, k;

        for (k = 0; k < N_OPS; k++)
            if (same(proto_ops[k].op, st->op)) {
                fn = proto_ops[k].fn;
                break;
            }
        c0 = mtime();
        r0 = runs;
        if (!fn || fn() != 0)
            bad++;
        out[10 + 3 * i + 0] = st->id;
        out[10 + 3 * i + 1] = mtime() - c0;
        out[10 + 3 * i + 2] = runs - r0;
    }

    write_anchors(out + 10 + 3 * PROTO_N_STAGES);

    out[0] = 0x50524F54u;                   /* 'PROT' */
    out[1] = errs + bad;
    out[2] = mtime() - t0;
    out[3] = runs;
    out[4] = dma_n;
    out[5] = busy;
    out[6] = 0;                             /* 轮询，不用中断 */
    out[7] = (PROTO_BUDGET_CYCLES && out[2] > PROTO_BUDGET_CYCLES) ? 1u : 0u;
    out[8] = PROTO_PARAMS_HASH;
    out[9] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    mmio_w(EXIT_ADDR, 0x600Du);
    return 0;
}
