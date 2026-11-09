/* NR PDSCH HARQ（两传输 Chase 合并）的算子注册表。 */
#include "../include/ldpc_bg2.h"
#include "../include/ldpc_enc_tables.h"
#include "ldpc_enc.inc"
#include "pdsch_cn_prog.h"
#include "pdsch_bn_prog.h"
#include "pdsch_harq_stages.inc"

static const proto_op_t proto_ops[] = {
    { "received",        0, harq_received },
    { "dmrs",            0, harq_dmrs },
    { "ofdm_demodulate", 0, harq_ofdm_demodulate },
    { "estimate",        0, harq_estimate },
    { "equalise_demap",  0, harq_equalise_demap },
    { "decode",          0, harq_decode },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

static int proto_setup(const uint32_t *par) { return harq_setup(par); }
#define PROTO_DMA_FIELD dmas

static void write_anchors(uint32_t *o)
{
    uint32_t b, i;
    o[0] = qs_crc_ok;
    o[1] = (uint32_t)harq_hre[0][0];
    o[2] = (uint32_t)harq_him[0][0];
    o[3] = (uint32_t)harq_hre[0][1];
    o[4] = (uint32_t)harq_him[0][1];
    o[5] = harq_k0[0] / (USABLE / 4);
    o[6] = harq_k0[1] / (USABLE / 4);
    o[7] = HARQ_NTX;
    for (b = 0; b < BG2_COLS; b++)
        for (i = 0; i < ZC; i++)
            o[8 + b * ZC + i] = (qs_hard[b][i] < 0) ? 1u : 0u;
}
