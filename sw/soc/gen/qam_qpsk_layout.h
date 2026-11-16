/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: qam_qpsk；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef QAM_QPSK_LAYOUT_H
#define QAM_QPSK_LAYOUT_H

#define QAM_QPSK_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define QAM_QPSK_LAYOUT_NPES        16u
#define QAM_QPSK_LAYOUT_WORDS       32u
#define QAM_QPSK_LAYOUT_BANKS       32u

#define QAM_QPSK_SPAD_LEAF_AMP_BASE   8u
#define QAM_QPSK_SPAD_LEAF_AMP_STRIDE 32u
#define QAM_QPSK_SPAD_LEAF_AMP(i)     (8u + 32u * (i))
#define QAM_QPSK_SPAD_LEAF_B0_BASE   0u
#define QAM_QPSK_SPAD_LEAF_B0_STRIDE 32u
#define QAM_QPSK_SPAD_LEAF_B0(i)     (0u + 32u * (i))
#define QAM_QPSK_SPAD_LEAF_ONE_BASE   6u
#define QAM_QPSK_SPAD_LEAF_ONE_STRIDE 32u
#define QAM_QPSK_SPAD_LEAF_ONE(i)     (6u + 32u * (i))
#define QAM_QPSK_SPAD_LEAF_Q0_BASE   1u
#define QAM_QPSK_SPAD_LEAF_Q0_STRIDE 32u
#define QAM_QPSK_SPAD_LEAF_Q0(i)     (1u + 32u * (i))
#define QAM_QPSK_SPAD_SINK_ILEV_BASE   512u
#define QAM_QPSK_SPAD_SINK_ILEV_STRIDE 32u
#define QAM_QPSK_SPAD_SINK_ILEV(i)     (512u + 32u * (i))
#define QAM_QPSK_SPAD_SINK_QLEV_BASE   513u
#define QAM_QPSK_SPAD_SINK_QLEV_STRIDE 32u
#define QAM_QPSK_SPAD_SINK_QLEV(i)     (513u + 32u * (i))

#endif /* QAM_QPSK_LAYOUT_H */
