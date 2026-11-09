/* -----------------------------------------------------------------------------
 * SSB 的算子注册表 + 结果锚点块（被 proto_runner.c 通过
 * -DPROTO_OPS_INC='"nr_ssb_ops.inc"' 引入）。
 *
 * 两个 correlate 用 impl 区分（PSS 3 个假设 / SSS 336 个假设），运行器按
 * plan.h 的 impl 字段派发。
 *
 * 锚点块布局（与 proto/models/nr_ssb.py 的 SPEC 一一对应）：
 *   [0] N_ID2 [1] N_ID1 [2] i_SSB [3] n_hf [4] DMRS c_init
 *   [5..8]  h（符号 1/3）  [9] SSS best>>10  [10] SSS true>>10
 *   [11..42] MIB 32 bit
 * -------------------------------------------------------------------------- */
#include "pss_corr_prog.h"
#include "pss_corr_layout.h"
#include "polar_node_prog.h"
#include "ssb_fid_luts.h"

#define RESULT_BASE      ((uint32_t *)0x80070000ul)
#define PARAM_BASE       0x80600000ul
#include "ssb_stages.inc"

static int ss_noop(void) { return 0; }

static const proto_op_t proto_ops[] = {
    { "received",       0,          ss_received        },
    { "ofdm_demodulate",0,          ss_ofdm_demodulate },
    { "correlate",      "pss_corr", ss_correlate_pss   },
    { "correlate",      "sss_corr", ss_correlate_sss   },
    /* PSS/SSS 的参考序列由相关搜索自己生成；只有 PBCH 那种建 Gold */
    { "dmrs",           "ref_pss",  ss_noop            },
    { "dmrs",           "ref_sss",  ss_noop            },
    { "dmrs",           "ref_pbch", ss_dmrs            },
    { "estimate",       0,          ss_estimate        },
    { "equalise_demap", 0,          ss_equalise_demap  },
    { "decode",         0,          ss_decode          },
};

#define N_OPS (sizeof(proto_ops) / sizeof(proto_ops[0]))

/* 运行器只认这三个契约：proto_setup / PROTO_DMA_FIELD / write_anchors。 */
static int proto_setup(const uint32_t *par) { return ss_setup(par); }
#define PROTO_DMA_FIELD dmas

static void write_anchors(uint32_t *o)
{
    o[0] = ss_nid2;
    o[1] = ss_nid1;
    o[2] = I_SSB;
    o[3] = N_HF;
    o[4] = ss_dmrs_ci;
    o[5] = (uint32_t)ss_hre[1];
    o[6] = (uint32_t)ss_him[1];
    o[7] = (uint32_t)ss_hre[3];
    o[8] = (uint32_t)ss_him[3];
    o[9] = ss_sbest10;
    o[10] = ss_strue10;
    for (uint32_t k = 0; k < KINFO; k++)
        o[11 + k] = ss_mib[k];
    o[43] = dbg_wait;          /* 诊断：等待/自旋计数 */
    o[44] = dbg_corr_spin;
    o[45] = dbg_array_spin;
    o[46] = nblk;
}
