/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: cn10_w16；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef CN10_W16_LAYOUT_H
#define CN10_W16_LAYOUT_H

#define CN10_W16_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define CN10_W16_LAYOUT_NPES        16u
#define CN10_W16_LAYOUT_WORDS       32u
#define CN10_W16_LAYOUT_BANKS       32u

#define CN10_W16_SPAD_LEAF_INF_BASE   976u
#define CN10_W16_SPAD_LEAF_INF_STRIDE 1u
#define CN10_W16_SPAD_LEAF_INF(i)     (976u + 1u * (i))
#define CN10_W16_SPAD_LEAF_M0_BASE   16u
#define CN10_W16_SPAD_LEAF_M0_STRIDE 1u
#define CN10_W16_SPAD_LEAF_M0(i)     (16u + 1u * (i))
#define CN10_W16_SPAD_LEAF_M1_BASE   48u
#define CN10_W16_SPAD_LEAF_M1_STRIDE 1u
#define CN10_W16_SPAD_LEAF_M1(i)     (48u + 1u * (i))
#define CN10_W16_SPAD_LEAF_M2_BASE   80u
#define CN10_W16_SPAD_LEAF_M2_STRIDE 1u
#define CN10_W16_SPAD_LEAF_M2(i)     (80u + 1u * (i))
#define CN10_W16_SPAD_LEAF_M3_BASE   112u
#define CN10_W16_SPAD_LEAF_M3_STRIDE 1u
#define CN10_W16_SPAD_LEAF_M3(i)     (112u + 1u * (i))
#define CN10_W16_SPAD_LEAF_M4_BASE   144u
#define CN10_W16_SPAD_LEAF_M4_STRIDE 1u
#define CN10_W16_SPAD_LEAF_M4(i)     (144u + 1u * (i))
#define CN10_W16_SPAD_LEAF_M5_BASE   176u
#define CN10_W16_SPAD_LEAF_M5_STRIDE 1u
#define CN10_W16_SPAD_LEAF_M5(i)     (176u + 1u * (i))
#define CN10_W16_SPAD_LEAF_M6_BASE   208u
#define CN10_W16_SPAD_LEAF_M6_STRIDE 1u
#define CN10_W16_SPAD_LEAF_M6(i)     (208u + 1u * (i))
#define CN10_W16_SPAD_LEAF_M7_BASE   240u
#define CN10_W16_SPAD_LEAF_M7_STRIDE 1u
#define CN10_W16_SPAD_LEAF_M7(i)     (240u + 1u * (i))
#define CN10_W16_SPAD_LEAF_M8_BASE   272u
#define CN10_W16_SPAD_LEAF_M8_STRIDE 1u
#define CN10_W16_SPAD_LEAF_M8(i)     (272u + 1u * (i))
#define CN10_W16_SPAD_LEAF_M9_BASE   304u
#define CN10_W16_SPAD_LEAF_M9_STRIDE 1u
#define CN10_W16_SPAD_LEAF_M9(i)     (304u + 1u * (i))
#define CN10_W16_SPAD_SINK_MN1_BASE   336u
#define CN10_W16_SPAD_SINK_MN1_STRIDE 1u
#define CN10_W16_SPAD_SINK_MN1(i)     (336u + 1u * (i))
#define CN10_W16_SPAD_SINK_MN2_BASE   368u
#define CN10_W16_SPAD_SINK_MN2_STRIDE 1u
#define CN10_W16_SPAD_SINK_MN2(i)     (368u + 1u * (i))
#define CN10_W16_SPAD_SINK_SG_BASE   400u
#define CN10_W16_SPAD_SINK_SG_STRIDE 1u
#define CN10_W16_SPAD_SINK_SG(i)     (400u + 1u * (i))

#endif /* CN10_W16_LAYOUT_H */
