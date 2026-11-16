/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: cn10_w0；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef CN10_W0_LAYOUT_H
#define CN10_W0_LAYOUT_H

#define CN10_W0_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define CN10_W0_LAYOUT_NPES        16u
#define CN10_W0_LAYOUT_WORDS       32u
#define CN10_W0_LAYOUT_BANKS       32u

#define CN10_W0_SPAD_LEAF_INF_BASE   960u
#define CN10_W0_SPAD_LEAF_INF_STRIDE 1u
#define CN10_W0_SPAD_LEAF_INF(i)     (960u + 1u * (i))
#define CN10_W0_SPAD_LEAF_M0_BASE   0u
#define CN10_W0_SPAD_LEAF_M0_STRIDE 1u
#define CN10_W0_SPAD_LEAF_M0(i)     (0u + 1u * (i))
#define CN10_W0_SPAD_LEAF_M1_BASE   32u
#define CN10_W0_SPAD_LEAF_M1_STRIDE 1u
#define CN10_W0_SPAD_LEAF_M1(i)     (32u + 1u * (i))
#define CN10_W0_SPAD_LEAF_M2_BASE   64u
#define CN10_W0_SPAD_LEAF_M2_STRIDE 1u
#define CN10_W0_SPAD_LEAF_M2(i)     (64u + 1u * (i))
#define CN10_W0_SPAD_LEAF_M3_BASE   96u
#define CN10_W0_SPAD_LEAF_M3_STRIDE 1u
#define CN10_W0_SPAD_LEAF_M3(i)     (96u + 1u * (i))
#define CN10_W0_SPAD_LEAF_M4_BASE   128u
#define CN10_W0_SPAD_LEAF_M4_STRIDE 1u
#define CN10_W0_SPAD_LEAF_M4(i)     (128u + 1u * (i))
#define CN10_W0_SPAD_LEAF_M5_BASE   160u
#define CN10_W0_SPAD_LEAF_M5_STRIDE 1u
#define CN10_W0_SPAD_LEAF_M5(i)     (160u + 1u * (i))
#define CN10_W0_SPAD_LEAF_M6_BASE   192u
#define CN10_W0_SPAD_LEAF_M6_STRIDE 1u
#define CN10_W0_SPAD_LEAF_M6(i)     (192u + 1u * (i))
#define CN10_W0_SPAD_LEAF_M7_BASE   224u
#define CN10_W0_SPAD_LEAF_M7_STRIDE 1u
#define CN10_W0_SPAD_LEAF_M7(i)     (224u + 1u * (i))
#define CN10_W0_SPAD_LEAF_M8_BASE   256u
#define CN10_W0_SPAD_LEAF_M8_STRIDE 1u
#define CN10_W0_SPAD_LEAF_M8(i)     (256u + 1u * (i))
#define CN10_W0_SPAD_LEAF_M9_BASE   288u
#define CN10_W0_SPAD_LEAF_M9_STRIDE 1u
#define CN10_W0_SPAD_LEAF_M9(i)     (288u + 1u * (i))
#define CN10_W0_SPAD_SINK_MN1_BASE   320u
#define CN10_W0_SPAD_SINK_MN1_STRIDE 1u
#define CN10_W0_SPAD_SINK_MN1(i)     (320u + 1u * (i))
#define CN10_W0_SPAD_SINK_MN2_BASE   352u
#define CN10_W0_SPAD_SINK_MN2_STRIDE 1u
#define CN10_W0_SPAD_SINK_MN2(i)     (352u + 1u * (i))
#define CN10_W0_SPAD_SINK_SG_BASE   384u
#define CN10_W0_SPAD_SINK_SG_STRIDE 1u
#define CN10_W0_SPAD_SINK_SG(i)     (384u + 1u * (i))

#endif /* CN10_W0_LAYOUT_H */
