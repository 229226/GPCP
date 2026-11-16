/* -----------------------------------------------------------------------------
 * MIMO 的算子注册表 + 结果锚点块（被 proto_runner.c 通过
 * -DPROTO_OPS_INC='"nr_mimo_ops.inc"' 引入）。
 *
 * 锚点块布局（与 proto/models/nr_mimo.py 的 SPEC 一一对应）：
 *   [0] CGRA MMSE 运行数 [1] 数据 RE 数 [2] lambda [3] 比特数
 *   [4..11]  4 个信道系数（re,im）
 *   [12..191] 180 个 RE 的 4 硬判比特（低 4 位）
 * -------------------------------------------------------------------------- */
#include "mmse_2x1_prog.h"
#include "mimo_stages.inc"

static const proto_op_t proto_ops[] = {
    { "received", 0, ms_received },
    { "dmrs", 0, ms_dmrs },
    { "ofdm_demodulate", 0, ms_ofdm_demodulate },
    { "estimate", 0, ms_estimate },
    { "mimo_equalise", 0, ms_mimo_equalise },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

/* 运行器只认这三个契约：proto_setup / PROTO_DMA_FIELD / write_anchors。 */
static int proto_setup(const uint32_t *par) { return ms_setup(par); }
#define PROTO_DMA_FIELD mmse_dmas

static void write_anchors(uint32_t *o)
{
    o[0] = mmse_runs;
    o[1] = NDATA;
    o[2] = (uint32_t)ms_lam;
    o[3] = NBITS;
    for (int rx = 0; rx < 2; rx++)
        for (int l = 0; l < 2; l++) {
            o[4 + 4 * rx + 2 * l] = (uint32_t)(int32_t)ms_hre[rx][l];
            o[5 + 4 * rx + 2 * l] = (uint32_t)(int32_t)ms_him[rx][l];
        }
    for (int i = 0; i < NDATA; i++)
        o[12 + i] = ms_bits[i];
}
