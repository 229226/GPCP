/* LTE PUSCH 的算子注册表 + 锚点块（布局与 lte_pdsch 相同）。 */
#include "lte_ul_stages.inc"

static const proto_op_t proto_ops[] = {
    { "received",        0, lu_received },
    { "transport_block", 0, lu_transport_block },
    { "encode",          0, lu_encode },
    { "modulate",        0, lu_modulate },
    { "dmrs",            0, lu_dmrs },
    { "map_to_grid",     0, lu_map_to_grid },
    { "ofdm_modulate",   0, lu_ofdm_modulate },
    { "channel",         0, lu_channel },
    { "ofdm_demodulate", 0, lu_ofdm_demodulate },
    { "estimate",        0, lu_estimate },
    { "equalise_demap",  0, lu_equalise_demap },
    { "decode",          0, lu_decode },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

static int proto_setup(const uint32_t *par) { return lu_setup(par); }
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
    o[992] = (uint32_t)lu_hre[0]; o[993] = (uint32_t)lu_him[0];
    o[994] = (uint32_t)lu_hre[1]; o[995] = (uint32_t)lu_him[1];
}
