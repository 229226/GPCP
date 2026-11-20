/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: mmse_dual；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef MMSE_DUAL_LAYOUT_H
#define MMSE_DUAL_LAYOUT_H

#define MMSE_DUAL_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define MMSE_DUAL_LAYOUT_NPES        16u
#define MMSE_DUAL_LAYOUT_WORDS       32u
#define MMSE_DUAL_LAYOUT_BANKS       32u

#define MMSE_DUAL_SPAD_LEAF_H00I_BASE   321u
#define MMSE_DUAL_SPAD_LEAF_H00I_STRIDE 0u
#define MMSE_DUAL_SPAD_LEAF_H00I(i)     (321u + 0u * (i))
#define MMSE_DUAL_SPAD_LEAF_H00R_BASE   320u
#define MMSE_DUAL_SPAD_LEAF_H00R_STRIDE 0u
#define MMSE_DUAL_SPAD_LEAF_H00R(i)     (320u + 0u * (i))
#define MMSE_DUAL_SPAD_LEAF_H01I_BASE   325u
#define MMSE_DUAL_SPAD_LEAF_H01I_STRIDE 0u
#define MMSE_DUAL_SPAD_LEAF_H01I(i)     (325u + 0u * (i))
#define MMSE_DUAL_SPAD_LEAF_H01R_BASE   324u
#define MMSE_DUAL_SPAD_LEAF_H01R_STRIDE 0u
#define MMSE_DUAL_SPAD_LEAF_H01R(i)     (324u + 0u * (i))
#define MMSE_DUAL_SPAD_LEAF_H10I_BASE   323u
#define MMSE_DUAL_SPAD_LEAF_H10I_STRIDE 0u
#define MMSE_DUAL_SPAD_LEAF_H10I(i)     (323u + 0u * (i))
#define MMSE_DUAL_SPAD_LEAF_H10R_BASE   322u
#define MMSE_DUAL_SPAD_LEAF_H10R_STRIDE 0u
#define MMSE_DUAL_SPAD_LEAF_H10R(i)     (322u + 0u * (i))
#define MMSE_DUAL_SPAD_LEAF_H11I_BASE   327u
#define MMSE_DUAL_SPAD_LEAF_H11I_STRIDE 0u
#define MMSE_DUAL_SPAD_LEAF_H11I(i)     (327u + 0u * (i))
#define MMSE_DUAL_SPAD_LEAF_H11R_BASE   326u
#define MMSE_DUAL_SPAD_LEAF_H11R_STRIDE 0u
#define MMSE_DUAL_SPAD_LEAF_H11R(i)     (326u + 0u * (i))
#define MMSE_DUAL_SPAD_LEAF_Y1I_BASE   1u
#define MMSE_DUAL_SPAD_LEAF_Y1I_STRIDE 2u
#define MMSE_DUAL_SPAD_LEAF_Y1I(i)     (1u + 2u * (i))
#define MMSE_DUAL_SPAD_LEAF_Y1R_BASE   0u
#define MMSE_DUAL_SPAD_LEAF_Y1R_STRIDE 2u
#define MMSE_DUAL_SPAD_LEAF_Y1R(i)     (0u + 2u * (i))
#define MMSE_DUAL_SPAD_LEAF_Y2I_BASE   33u
#define MMSE_DUAL_SPAD_LEAF_Y2I_STRIDE 2u
#define MMSE_DUAL_SPAD_LEAF_Y2I(i)     (33u + 2u * (i))
#define MMSE_DUAL_SPAD_LEAF_Y2R_BASE   32u
#define MMSE_DUAL_SPAD_LEAF_Y2R_STRIDE 2u
#define MMSE_DUAL_SPAD_LEAF_Y2R(i)     (32u + 2u * (i))
#define MMSE_DUAL_SPAD_LEAF_ZERO_BASE   328u
#define MMSE_DUAL_SPAD_LEAF_ZERO_STRIDE 0u
#define MMSE_DUAL_SPAD_LEAF_ZERO(i)     (328u + 0u * (i))
#define MMSE_DUAL_SPAD_SINK_N0I_BASE   544u
#define MMSE_DUAL_SPAD_SINK_N0I_STRIDE 1u
#define MMSE_DUAL_SPAD_SINK_N0I(i)     (544u + 1u * (i))
#define MMSE_DUAL_SPAD_SINK_N0R_BASE   512u
#define MMSE_DUAL_SPAD_SINK_N0R_STRIDE 1u
#define MMSE_DUAL_SPAD_SINK_N0R(i)     (512u + 1u * (i))
#define MMSE_DUAL_SPAD_SINK_N1I_BASE   608u
#define MMSE_DUAL_SPAD_SINK_N1I_STRIDE 1u
#define MMSE_DUAL_SPAD_SINK_N1I(i)     (608u + 1u * (i))
#define MMSE_DUAL_SPAD_SINK_N1R_BASE   576u
#define MMSE_DUAL_SPAD_SINK_N1R_STRIDE 1u
#define MMSE_DUAL_SPAD_SINK_N1R(i)     (576u + 1u * (i))
#define MMSE_DUAL_SPAD_SINK_YMAX_BASE   640u
#define MMSE_DUAL_SPAD_SINK_YMAX_STRIDE 1u
#define MMSE_DUAL_SPAD_SINK_YMAX(i)     (640u + 1u * (i))

#endif /* MMSE_DUAL_LAYOUT_H */
