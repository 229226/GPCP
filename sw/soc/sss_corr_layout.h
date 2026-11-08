/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: sss_corr；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef SSS_CORR_LAYOUT_H
#define SSS_CORR_LAYOUT_H

#define SSS_CORR_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define SSS_CORR_LAYOUT_NPES        16u
#define SSS_CORR_LAYOUT_WORDS       32u
#define SSS_CORR_LAYOUT_BANKS       32u

#define SSS_CORR_SPAD_LEAF_R0_BASE   256u
#define SSS_CORR_SPAD_LEAF_R0_STRIDE 32u
#define SSS_CORR_SPAD_LEAF_R0(i)     (256u + 32u * (i))
#define SSS_CORR_SPAD_LEAF_R1_BASE   257u
#define SSS_CORR_SPAD_LEAF_R1_STRIDE 32u
#define SSS_CORR_SPAD_LEAF_R1(i)     (257u + 32u * (i))
#define SSS_CORR_SPAD_LEAF_R2_BASE   258u
#define SSS_CORR_SPAD_LEAF_R2_STRIDE 32u
#define SSS_CORR_SPAD_LEAF_R2(i)     (258u + 32u * (i))
#define SSS_CORR_SPAD_LEAF_R3_BASE   259u
#define SSS_CORR_SPAD_LEAF_R3_STRIDE 32u
#define SSS_CORR_SPAD_LEAF_R3(i)     (259u + 32u * (i))
#define SSS_CORR_SPAD_LEAF_R4_BASE   260u
#define SSS_CORR_SPAD_LEAF_R4_STRIDE 32u
#define SSS_CORR_SPAD_LEAF_R4(i)     (260u + 32u * (i))
#define SSS_CORR_SPAD_LEAF_R5_BASE   261u
#define SSS_CORR_SPAD_LEAF_R5_STRIDE 32u
#define SSS_CORR_SPAD_LEAF_R5(i)     (261u + 32u * (i))
#define SSS_CORR_SPAD_LEAF_R6_BASE   262u
#define SSS_CORR_SPAD_LEAF_R6_STRIDE 32u
#define SSS_CORR_SPAD_LEAF_R6(i)     (262u + 32u * (i))
#define SSS_CORR_SPAD_LEAF_R7_BASE   263u
#define SSS_CORR_SPAD_LEAF_R7_STRIDE 32u
#define SSS_CORR_SPAD_LEAF_R7(i)     (263u + 32u * (i))
#define SSS_CORR_SPAD_LEAF_XI0_BASE   1u
#define SSS_CORR_SPAD_LEAF_XI0_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XI0(i)     (1u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XI1_BASE   3u
#define SSS_CORR_SPAD_LEAF_XI1_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XI1(i)     (3u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XI2_BASE   5u
#define SSS_CORR_SPAD_LEAF_XI2_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XI2(i)     (5u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XI3_BASE   7u
#define SSS_CORR_SPAD_LEAF_XI3_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XI3(i)     (7u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XI4_BASE   9u
#define SSS_CORR_SPAD_LEAF_XI4_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XI4(i)     (9u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XI5_BASE   11u
#define SSS_CORR_SPAD_LEAF_XI5_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XI5(i)     (11u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XI6_BASE   13u
#define SSS_CORR_SPAD_LEAF_XI6_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XI6(i)     (13u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XI7_BASE   15u
#define SSS_CORR_SPAD_LEAF_XI7_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XI7(i)     (15u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XR0_BASE   0u
#define SSS_CORR_SPAD_LEAF_XR0_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XR0(i)     (0u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XR1_BASE   2u
#define SSS_CORR_SPAD_LEAF_XR1_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XR1(i)     (2u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XR2_BASE   4u
#define SSS_CORR_SPAD_LEAF_XR2_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XR2(i)     (4u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XR3_BASE   6u
#define SSS_CORR_SPAD_LEAF_XR3_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XR3(i)     (6u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XR4_BASE   8u
#define SSS_CORR_SPAD_LEAF_XR4_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XR4(i)     (8u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XR5_BASE   10u
#define SSS_CORR_SPAD_LEAF_XR5_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XR5(i)     (10u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XR6_BASE   12u
#define SSS_CORR_SPAD_LEAF_XR6_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XR6(i)     (12u + 16u * (i))
#define SSS_CORR_SPAD_LEAF_XR7_BASE   14u
#define SSS_CORR_SPAD_LEAF_XR7_STRIDE 16u
#define SSS_CORR_SPAD_LEAF_XR7(i)     (14u + 16u * (i))
#define SSS_CORR_SPAD_SINK_PART_I_BASE   769u
#define SSS_CORR_SPAD_SINK_PART_I_STRIDE 2u
#define SSS_CORR_SPAD_SINK_PART_I(i)     (769u + 2u * (i))
#define SSS_CORR_SPAD_SINK_PART_R_BASE   768u
#define SSS_CORR_SPAD_SINK_PART_R_STRIDE 2u
#define SSS_CORR_SPAD_SINK_PART_R(i)     (768u + 2u * (i))

#endif /* SSS_CORR_LAYOUT_H */
