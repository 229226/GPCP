/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: pss_corr；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef PSS_CORR_LAYOUT_H
#define PSS_CORR_LAYOUT_H

#define PSS_CORR_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define PSS_CORR_LAYOUT_NPES        16u
#define PSS_CORR_LAYOUT_WORDS       32u
#define PSS_CORR_LAYOUT_BANKS       32u

#define PSS_CORR_SPAD_LEAF_R0_BASE   256u
#define PSS_CORR_SPAD_LEAF_R0_STRIDE 32u
#define PSS_CORR_SPAD_LEAF_R0(i)     (256u + 32u * (i))
#define PSS_CORR_SPAD_LEAF_R1_BASE   257u
#define PSS_CORR_SPAD_LEAF_R1_STRIDE 32u
#define PSS_CORR_SPAD_LEAF_R1(i)     (257u + 32u * (i))
#define PSS_CORR_SPAD_LEAF_R2_BASE   258u
#define PSS_CORR_SPAD_LEAF_R2_STRIDE 32u
#define PSS_CORR_SPAD_LEAF_R2(i)     (258u + 32u * (i))
#define PSS_CORR_SPAD_LEAF_R3_BASE   259u
#define PSS_CORR_SPAD_LEAF_R3_STRIDE 32u
#define PSS_CORR_SPAD_LEAF_R3(i)     (259u + 32u * (i))
#define PSS_CORR_SPAD_LEAF_R4_BASE   260u
#define PSS_CORR_SPAD_LEAF_R4_STRIDE 32u
#define PSS_CORR_SPAD_LEAF_R4(i)     (260u + 32u * (i))
#define PSS_CORR_SPAD_LEAF_R5_BASE   261u
#define PSS_CORR_SPAD_LEAF_R5_STRIDE 32u
#define PSS_CORR_SPAD_LEAF_R5(i)     (261u + 32u * (i))
#define PSS_CORR_SPAD_LEAF_R6_BASE   262u
#define PSS_CORR_SPAD_LEAF_R6_STRIDE 32u
#define PSS_CORR_SPAD_LEAF_R6(i)     (262u + 32u * (i))
#define PSS_CORR_SPAD_LEAF_R7_BASE   263u
#define PSS_CORR_SPAD_LEAF_R7_STRIDE 32u
#define PSS_CORR_SPAD_LEAF_R7(i)     (263u + 32u * (i))
#define PSS_CORR_SPAD_LEAF_YI0_BASE   1u
#define PSS_CORR_SPAD_LEAF_YI0_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YI0(i)     (1u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YI1_BASE   3u
#define PSS_CORR_SPAD_LEAF_YI1_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YI1(i)     (3u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YI2_BASE   5u
#define PSS_CORR_SPAD_LEAF_YI2_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YI2(i)     (5u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YI3_BASE   7u
#define PSS_CORR_SPAD_LEAF_YI3_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YI3(i)     (7u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YI4_BASE   9u
#define PSS_CORR_SPAD_LEAF_YI4_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YI4(i)     (9u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YI5_BASE   11u
#define PSS_CORR_SPAD_LEAF_YI5_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YI5(i)     (11u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YI6_BASE   13u
#define PSS_CORR_SPAD_LEAF_YI6_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YI6(i)     (13u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YI7_BASE   15u
#define PSS_CORR_SPAD_LEAF_YI7_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YI7(i)     (15u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YR0_BASE   0u
#define PSS_CORR_SPAD_LEAF_YR0_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YR0(i)     (0u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YR1_BASE   2u
#define PSS_CORR_SPAD_LEAF_YR1_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YR1(i)     (2u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YR2_BASE   4u
#define PSS_CORR_SPAD_LEAF_YR2_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YR2(i)     (4u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YR3_BASE   6u
#define PSS_CORR_SPAD_LEAF_YR3_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YR3(i)     (6u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YR4_BASE   8u
#define PSS_CORR_SPAD_LEAF_YR4_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YR4(i)     (8u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YR5_BASE   10u
#define PSS_CORR_SPAD_LEAF_YR5_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YR5(i)     (10u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YR6_BASE   12u
#define PSS_CORR_SPAD_LEAF_YR6_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YR6(i)     (12u + 16u * (i))
#define PSS_CORR_SPAD_LEAF_YR7_BASE   14u
#define PSS_CORR_SPAD_LEAF_YR7_STRIDE 16u
#define PSS_CORR_SPAD_LEAF_YR7(i)     (14u + 16u * (i))
#define PSS_CORR_SPAD_SINK_ACC_IM_BASE   769u
#define PSS_CORR_SPAD_SINK_ACC_IM_STRIDE 2u
#define PSS_CORR_SPAD_SINK_ACC_IM(i)     (769u + 2u * (i))
#define PSS_CORR_SPAD_SINK_ACC_RE_BASE   768u
#define PSS_CORR_SPAD_SINK_ACC_RE_STRIDE 2u
#define PSS_CORR_SPAD_SINK_ACC_RE(i)     (768u + 2u * (i))

#endif /* PSS_CORR_LAYOUT_H */
