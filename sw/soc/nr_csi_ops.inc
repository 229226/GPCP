/* -----------------------------------------------------------------------------
 * CSI 的算子注册表 + 结果锚点块（被 proto_runner.c 通过
 * -DPROTO_OPS_INC='"nr_csi_ops.inc"' 引入）。
 *
 * 锚点块布局（与 proto/models/nr_csi.py 的 SPEC 一一对应）：
 *   [0] CQI  [1] snr_q8  [2] Ps>>40  [3] Pn>>10
 *   [4..11] A[0..3]（re,im）  [12] NCSI  [13] c_init
 * -------------------------------------------------------------------------- */
#include "csi_cqi_tab.h"
#include "csi_stages.inc"

static const proto_op_t proto_ops[] = {
    { "received", 0, cs_received },
    { "dmrs", 0, cs_dmrs },
    { "ofdm_demodulate", 0, cs_ofdm_demodulate },
    { "measure", 0, cs_measure },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

/* 运行器只认这三个契约：proto_setup / PROTO_DMA_FIELD / write_anchors。 */
static int proto_setup(const uint32_t *par) { return cs_setup(par); }
#define PROTO_DMA_FIELD dmas

static void write_anchors(uint32_t *o)
{
    o[0] = cs_cqi;
    o[1] = cs_snr_q8;
    o[2] = cs_ps40;
    o[3] = cs_pn10;
    for (int i = 0; i < 4; i++) {
        o[4 + 2 * i] = (uint32_t)(int32_t)cs_a_re[i];
        o[5 + 2 * i] = (uint32_t)(int32_t)cs_a_im[i];
    }
    o[12] = (uint32_t)NCSI;
    o[13] = cs_cinit_v;
}
