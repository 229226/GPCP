/* -----------------------------------------------------------------------------
 * PUSCH 的算子注册表 + 结果锚点块（被 proto_runner.c 通过
 * -DPROTO_OPS_INC='"nr_pusch_ops.inc"' 引入）。
 *
 * 锚点块布局（与 proto/models/nr_pusch.py 的 SPEC 一一对应）：
 *   [0] crc_ok [1] k0 [2] HARQ 传输数 [3] TP 标志
 *   [4..7]  h（re,im 两个符号）  [8] DMRS c_init0  [9] DMRS c_init1
 *   [10..1149] 1040 个 LDPC 硬判决（与 nr_pusch 的黄金码字逐位）
 * -------------------------------------------------------------------------- */
#include "../include/ldpc_bg2.h"
#include "pdsch_cn_prog.h"
#include "pdsch_bn_prog.h"
#include "pusch_stages.inc"

static const proto_op_t proto_ops[] = {
    { "received", 0, us_received },
    { "dmrs", 0, us_dmrs },
    { "ofdm_demodulate", 0, us_ofdm_demodulate },
    { "estimate", 0, us_estimate },
#if HARQ_TX == 1
    { "equalise", 0, us_equalise },
#ifdef TP
    { "transform", 0, us_transform },
#endif
    { "demap", 0, us_demap },
#else
    { "equalise_demap", 0, us_equalise_demap },
#endif
    { "decode", 0, us_decode },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

/* 运行器只认这三个契约：proto_setup / PROTO_DMA_FIELD / write_anchors。 */
static int proto_setup(const uint32_t *par) { return us_setup(par); }
#define PROTO_DMA_FIELD dmas

static void write_anchors(uint32_t *o)
{
    o[0] = us_crc_ok;
    o[1] = us_k0;
    o[2] = (uint32_t)HARQ_TX;
#ifdef TP
    o[3] = 1u;
#else
    o[3] = 0u;
#endif
    o[4] = (uint32_t)us_hre[0][0];
    o[5] = (uint32_t)us_him[0][0];
    o[6] = (uint32_t)us_hre[0][1];
    o[7] = (uint32_t)us_him[0][1];
    o[8] = dmrs_cinit(0);
    o[9] = dmrs_cinit(1);
    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t i = 0; i < ZC; i++)
            o[10 + b * ZC + i] = (us_hard[b][i] < 0) ? 1u : 0u;
}
