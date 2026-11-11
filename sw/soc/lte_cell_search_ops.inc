/* LTE 小区搜索（PSS 3 假设 + SSS 504 假设）的算子注册表。 */
#include "lte_cell_stages.inc"

static const proto_op_t proto_ops[] = {
    { "received", 0, cell_received },
    { "dmrs",     0, cell_dmrs },
    { "correlate", "cell_search_scalar", cell_correlate },
    { "correlate", "cell_search_cgra", cell_correlate },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

static int proto_setup(const uint32_t *par) { return cell_setup(par); }
#define PROTO_DMA_FIELD dmas

static void write_anchors(uint32_t *o)
{
    o[0] = cell_nid2;
    o[1] = cell_nid1;
    o[2] = 3u * cell_nid1 + cell_nid2;
    o[3] = cell_pss_best;
    o[4] = cell_pss_second;
    o[5] = cell_sss_best;
    o[6] = cell_sss_second;
    for (int u = 0; u < 3; u++) {
        o[7 + 2 * u] = (uint32_t)(cell_pss_e[u] >> 24);
        o[8 + 2 * u] = (uint32_t)(cell_pss_e[u] >> 24);
    }
    for (int h = 0; h < CELL_NSSS; h++)
        o[13 + h] = (uint32_t)(cell_sss_e[h] >> 24);
}
