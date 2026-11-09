/* -----------------------------------------------------------------------------
 * PDCCH 的算子注册表 + 结果锚点块（被 proto_runner.c 通过
 * -DPROTO_OPS_INC='"pdcch_ops.inc"' 引入；换场景只换这一份）。
 *
 * 锚点块布局（与 proto/models/nr_pdcch.py 的 SPEC 一一对应）：
 *   [0] dci_tx [1] dci_rx [2] rnti [3] rnti_match [4] crc_ok
 *   [5] llr_bad [6] llr_amin [7] llr_amax [8] n_id [9] max_sym
 *   [10..41]   发射 DCI 比特
 *   [42..73]   译码 DCI 比特
 *   [74..97]   CRC24C 比特（去掩码）
 *   [98..529]  e_sc（加扰码字 432 bit）
 * -------------------------------------------------------------------------- */
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

#include "pdcch_stages.inc"

static const proto_op_t proto_ops[] = {
    { "transport_block", 0, ps_transport_block },
    { "encode", 0, ps_encode },
    { "modulate", 0, ps_modulate },
    { "dmrs", 0, ps_dmrs },
    { "map_to_grid", 0, ps_map_to_grid },
    { "ofdm_modulate", 0, ps_ofdm_modulate },
    { "channel", 0, ps_channel },
    { "estimate", 0, ps_estimate },
    { "equalise_demap", 0, ps_equalise_demap },
    { "decode", 0, ps_decode },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

/* 运行器只认这三个契约：proto_setup / PROTO_DMA_FIELD / write_anchors。 */
static int proto_setup(const uint32_t *par) { return ps_setup(par); }
#define PROTO_DMA_FIELD dma_n

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
