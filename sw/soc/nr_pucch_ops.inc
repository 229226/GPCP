/* -----------------------------------------------------------------------------
 * PUCCH 的算子注册表 + 结果锚点块（被 proto_runner.c 通过
 * -DPROTO_OPS_INC='"nr_pucch_ops.inc"' 引入）。
 *
 * 锚点块布局（与 proto/models/nr_pucch.py 的 SPEC 一一对应）：
 *   [0] bit [1] winner [2] m_CS [3] NCAND [4] taps [5] 分部复核计数
 *   [6..13]  C[0]/C[1] 的 re,im,pw 低/高
 *   [14..77] 2 x 32 个 PE 分部（与 run-pucch 逐值）
 * -------------------------------------------------------------------------- */
#include "prach_corr_prog.h"
#include "prach_corr_layout.h"
#include "zc_corr.inc"

#define PUCCH_NCAND 2u
#define PUCCH_CS_ACK 0u
#define PUCCH_CS_NACK 6u

static uint32_t pp_part[2 * 32];

static int pp_correlate(void)
{
    return zc_search(PUCCH_NCAND, pp_part, 0);
}

static const proto_op_t proto_ops[] = {
    { "received",  zc_received  },
    { "dmrs",      zc_dmrs      },
    { "correlate", pp_correlate },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

/* 运行器只认这三个契约：proto_setup / PROTO_DMA_FIELD / write_anchors。 */
static int proto_setup(const uint32_t *par) { return zc_setup(par); }
#define PROTO_DMA_FIELD dmas

static void write_anchors(uint32_t *o)
{
    static const uint32_t mcs[PUCCH_NCAND] = { PUCCH_CS_ACK, PUCCH_CS_NACK };
    uint32_t win = zc_win;
    o[0] = (win == 0u) ? 1u : 0u;         /* 1 = ACK/SR */
    o[1] = win;
    o[2] = mcs[win];
    o[3] = PUCCH_NCAND;
    o[4] = ZC_TAPS;
    o[5] = nchk;
    for (uint32_t c = 0; c < PUCCH_NCAND; c++) {
        o[6 + 4 * c] = (uint32_t)zc_re[c];
        o[7 + 4 * c] = (uint32_t)zc_im[c];
        o[8 + 4 * c] = (uint32_t)zc_pw[c];
        o[9 + 4 * c] = (uint32_t)((uint64_t)zc_pw[c] >> 32);
    }
    for (uint32_t i = 0; i < 2 * 32; i++)
        o[14 + i] = pp_part[i];
}
