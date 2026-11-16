/* 高阶 QAM 纯接收的算子注册表（16QAM / 64QAM 共用）。
 *
 * 激励：2 符号 TD + 尾部 [TB 3 字, qm, rv, n_RNTI, magic]。
 * 阶段：received -> dmrs -> estimate(FFT+LS) -> equalise_demap(QAM max-log)
 *       -> decode(LDPC)。
 */
#include "../../include/ldpc_bg2.h"
#include "../../include/ldpc_enc_tables.h"
#include "ldpc_enc.inc"
#include "pdsch_cn_prog.h"
#include "pdsch_bn_prog.h"
#include "pdsch_stages.inc"

static int qs_rx_received(void) { return 0; }

static const proto_op_t proto_ops[] = {
    { "received",        0, qs_rx_received },
    { "dmrs",            0, qs_dmrs },
    { "transform", "fft4096", qs_transform },
    { "estimate", 0, qs_estimate },
    { "equalise", 0, qs_equalise },
    { "demap", 0, qs_demap },
    { "decode",          0, qs_decode },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

static int proto_setup(const uint32_t *par) { return qs_rx_setup(par); }
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
