/* -----------------------------------------------------------------------------
 * PRACH 的算子注册表 + 结果锚点块（被 proto_runner.c 通过
 * -DPROTO_OPS_INC='"nr_prach_ops.inc"' 引入）。
 *
 * 锚点块布局（与 proto/models/nr_prach.py 的 SPEC 一一对应）：
 *   [0] preamble [1] TA [2] best_d [3] second_d [4] N_CS [5] L_RA [6] taps
 *   [7] 分部复核计数
 *   [8..285] 139 个相关值（re,im），与 msg1_correlations 逐值
 * -------------------------------------------------------------------------- */
#include "prach_corr_prog.h"
#include "prach_corr_layout.h"
#include "zc_corr.inc"

#define ZC_N_CS 15u         /* zero-correlation zone（SIB1 zczc 0） */

/* correlate 动词在这里是"搜索 + 归约 + 峰值"（PRACH 的判决就是峰值）。 */
static int zc_correlate_all(void)
{
    return zc_search(ZC_L_RA, 0, 1);
}

static const proto_op_t proto_ops[] = {
    { "received",  zc_received  },
    { "dmrs",      zc_dmrs      },
    { "correlate", zc_correlate_all },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

/* 运行器只认这三个契约：proto_setup / PROTO_DMA_FIELD / write_anchors。 */
static int proto_setup(const uint32_t *par) { return zc_setup(par); }
#define PROTO_DMA_FIELD dmas

static void write_anchors(uint32_t *o)
{
    o[0] = zc_best_d / ZC_N_CS;           /* preamble */
    o[1] = zc_best_d % ZC_N_CS;           /* timing advance */
    o[2] = zc_best_d;
    o[3] = zc_second_d;
    o[4] = ZC_N_CS;
    o[5] = ZC_L_RA;
    o[6] = ZC_TAPS;
    o[7] = nchk;
    for (uint32_t d = 0; d < ZC_L_RA; d++) {
        o[8 + 2 * d] = (uint32_t)zc_re[d];
        o[9 + 2 * d] = (uint32_t)zc_im[d];
    }
}
