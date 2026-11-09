/* -----------------------------------------------------------------------------
 * LTE PDSCH 的算子注册表 + 结果锚点块（阶段 3「跨制式」）。
 *
 * 锚点块布局（与 proto/models/lte_pdsch.py 的 SPEC 一一对应）：
 *   [0] crc_ok [1] tb_ok [2] rv [3] 0
 *   [4..107]  104 个译码后信息位
 *   [112..191] 80 bit TB
 *   [192..991] 800 bit 加扰码字 e_sc（发射链自环的强判据）
 * -------------------------------------------------------------------------- */
#include "lte_stages.inc"

static const proto_op_t proto_ops[] = {
    { "received",       0, lte_received             },
    { "transport_block",0, lte_transport_block      },
    { "encode",         0, lte_encode               },
    { "modulate",       0, lte_modulate             },
    { "dmrs",           0, lte_dmrs                 },
    { "map_to_grid",    0, lte_map_to_grid          },
    { "ofdm_modulate",  0, lte_ofdm_modulate        },
    { "channel",        0, lte_channel              },
    { "ofdm_demodulate",0, lte_ofdm_demodulate      },
    { "estimate",       0, lte_estimate             },
    { "equalise_demap", 0, lte_equalise_demap       },
    { "decode",         0, lte_decode               },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

/* 运行器只认这三个契约：proto_setup / PROTO_DMA_FIELD / write_anchors。 */
static int proto_setup(const uint32_t *par) { return lte_setup(par); }
#define PROTO_DMA_FIELD dmas

static void write_anchors(uint32_t *o)
{
    o[0] = lte_crc_ok;
    o[1] = lte_tb_ok;
    o[2] = lte_rv;
    o[3] = 0;
    for (int i = 0; i < K_INFO; i++)
        o[4 + i] = rx_info[i] ? 1u : 0u;
    for (int i = K_INFO; i < NPOL; i++)
        o[4 + i] = 0;
    for (int i = 0; i < A_TB; i++)
        o[112 + i] = tb_bits[i];
    for (int i = 0; i < E_BITS; i++)
        o[192 + i] = e_sc[i];
    o[992] = (uint32_t)hre[0]; o[993] = (uint32_t)him[0];
    o[994] = (uint32_t)hre[1]; o[995] = (uint32_t)him[1];
    for (int i = 0; i < 8; i++) o[996 + i] = (uint32_t)llr_buf[i];
    for (int i = 0; i < 8; i++) o[1004 + i] = (uint32_t)llr_d0[i];
}
