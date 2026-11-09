/* -----------------------------------------------------------------------------
 * PDSCH 的算子注册表 + 结果锚点块（被 proto_runner.c 通过
 * -DPROTO_OPS_INC='"pdsch_ops.inc"' 引入）。
 *
 * 锚点块布局（与 proto/models/nr_pdsch.py 的 SPEC 一一对应）：
 *   [0] tb_bad [1] crc_ok [2] llr_bad [3] llr_first_off
 *   [4] hre0 [5] him0 [6] hre1 [7] him1
 *   [8] dmrs c_init0 [9] dmrs c_init1 [10] rv [11] qm
 *   [12] max|sym| [13] LLR 个数
 *   [14..93]    发射 TB 比特（80）
 *   [94..1133]  1040 个 LDPC 硬判决（块优先，与手写基准同序）
 * -------------------------------------------------------------------------- */
#include "../include/ldpc_bg2.h"
#include "../include/ldpc_enc_tables.h"
#include "ldpc_enc.inc"
#include "pdsch_cn_prog.h"
#include "pdsch_bn_prog.h"

#include "pdsch_stages.inc"

static const proto_op_t proto_ops[] = {
    { "transport_block", 0, qs_transport_block },
    { "encode", 0, qs_encode },
    { "modulate", 0, qs_modulate },
    { "dmrs", 0, qs_dmrs },
    { "map_to_grid", 0, qs_map_to_grid },
    { "ofdm_modulate", 0, qs_ofdm_modulate },
    { "channel", 0, qs_channel },
    { "estimate", 0, qs_estimate },
    { "equalise_demap", 0, qs_equalise_demap },
    { "decode", 0, qs_decode },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

/* 运行器只认这三个契约：proto_setup / PROTO_DMA_FIELD / write_anchors。 */
static int proto_setup(const uint32_t *par) { return qs_setup(par); }
#define PROTO_DMA_FIELD dmas

static void write_anchors(uint32_t *o)
{
    uint32_t i, b;
    o[0] = qs_tb_bad;
    o[1] = qs_crc_ok;
    o[2] = qs_llr_bad;
    o[3] = qs_llr_first;
    o[4] = (uint32_t)qs_hre[0];
    o[5] = (uint32_t)qs_him[0];
    o[6] = (uint32_t)qs_hre[1];
    o[7] = (uint32_t)qs_him[1];
    o[8] = dmrs_cinit(0);
    o[9] = dmrs_cinit(1);
    o[10] = qs_rv;
    o[11] = qs_qm;
    o[12] = qs_sym_max;
    o[13] = E_BITS;
    for (i = 0; i < A_TB; i++)
        o[14 + i] = tb_bits[i];
    for (b = 0; b < BG2_COLS; b++)
        for (i = 0; i < ZC; i++)
            o[14 + A_TB + b * ZC + i] = (qs_hard[b][i] < 0) ? 1u : 0u;
}
