/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: mmse_2x1；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef MMSE_2X1_LAYOUT_H
#define MMSE_2X1_LAYOUT_H

#define MMSE_2X1_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define MMSE_2X1_LAYOUT_NPES        16u
#define MMSE_2X1_LAYOUT_WORDS       32u
#define MMSE_2X1_LAYOUT_BANKS       32u

#define MMSE_2X1_SPAD_LEAF_H1I_BASE   32u
#define MMSE_2X1_SPAD_LEAF_H1I_STRIDE 1u
#define MMSE_2X1_SPAD_LEAF_H1I(i)     (32u + 1u * (i))
#define MMSE_2X1_SPAD_LEAF_H1R_BASE   0u
#define MMSE_2X1_SPAD_LEAF_H1R_STRIDE 1u
#define MMSE_2X1_SPAD_LEAF_H1R(i)     (0u + 1u * (i))
#define MMSE_2X1_SPAD_LEAF_H2I_BASE   96u
#define MMSE_2X1_SPAD_LEAF_H2I_STRIDE 1u
#define MMSE_2X1_SPAD_LEAF_H2I(i)     (96u + 1u * (i))
#define MMSE_2X1_SPAD_LEAF_H2R_BASE   64u
#define MMSE_2X1_SPAD_LEAF_H2R_STRIDE 1u
#define MMSE_2X1_SPAD_LEAF_H2R(i)     (64u + 1u * (i))
#define MMSE_2X1_SPAD_LEAF_Y1I_BASE   160u
#define MMSE_2X1_SPAD_LEAF_Y1I_STRIDE 1u
#define MMSE_2X1_SPAD_LEAF_Y1I(i)     (160u + 1u * (i))
#define MMSE_2X1_SPAD_LEAF_Y1R_BASE   128u
#define MMSE_2X1_SPAD_LEAF_Y1R_STRIDE 1u
#define MMSE_2X1_SPAD_LEAF_Y1R(i)     (128u + 1u * (i))
#define MMSE_2X1_SPAD_LEAF_Y2I_BASE   224u
#define MMSE_2X1_SPAD_LEAF_Y2I_STRIDE 1u
#define MMSE_2X1_SPAD_LEAF_Y2I(i)     (224u + 1u * (i))
#define MMSE_2X1_SPAD_LEAF_Y2R_BASE   192u
#define MMSE_2X1_SPAD_LEAF_Y2R_STRIDE 1u
#define MMSE_2X1_SPAD_LEAF_Y2R(i)     (192u + 1u * (i))
#define MMSE_2X1_SPAD_SINK_NUMI_BASE   288u
#define MMSE_2X1_SPAD_SINK_NUMI_STRIDE 1u
#define MMSE_2X1_SPAD_SINK_NUMI(i)     (288u + 1u * (i))
#define MMSE_2X1_SPAD_SINK_NUMR_BASE   256u
#define MMSE_2X1_SPAD_SINK_NUMR_STRIDE 1u
#define MMSE_2X1_SPAD_SINK_NUMR(i)     (256u + 1u * (i))

#endif /* MMSE_2X1_LAYOUT_H */
