/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: bpsk_corr；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef BPSK_CORR_LAYOUT_H
#define BPSK_CORR_LAYOUT_H

#define BPSK_CORR_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define BPSK_CORR_LAYOUT_NPES        16u
#define BPSK_CORR_LAYOUT_WORDS       32u
#define BPSK_CORR_LAYOUT_BANKS       32u

#define BPSK_CORR_SPAD_LEAF_R0_BASE   256u
#define BPSK_CORR_SPAD_LEAF_R0_STRIDE 8u
#define BPSK_CORR_SPAD_LEAF_R0(i)     (256u + 8u * (i))
#define BPSK_CORR_SPAD_LEAF_R1_BASE   257u
#define BPSK_CORR_SPAD_LEAF_R1_STRIDE 8u
#define BPSK_CORR_SPAD_LEAF_R1(i)     (257u + 8u * (i))
#define BPSK_CORR_SPAD_LEAF_R2_BASE   258u
#define BPSK_CORR_SPAD_LEAF_R2_STRIDE 8u
#define BPSK_CORR_SPAD_LEAF_R2(i)     (258u + 8u * (i))
#define BPSK_CORR_SPAD_LEAF_R3_BASE   259u
#define BPSK_CORR_SPAD_LEAF_R3_STRIDE 8u
#define BPSK_CORR_SPAD_LEAF_R3(i)     (259u + 8u * (i))
#define BPSK_CORR_SPAD_LEAF_R4_BASE   260u
#define BPSK_CORR_SPAD_LEAF_R4_STRIDE 8u
#define BPSK_CORR_SPAD_LEAF_R4(i)     (260u + 8u * (i))
#define BPSK_CORR_SPAD_LEAF_R5_BASE   261u
#define BPSK_CORR_SPAD_LEAF_R5_STRIDE 8u
#define BPSK_CORR_SPAD_LEAF_R5(i)     (261u + 8u * (i))
#define BPSK_CORR_SPAD_LEAF_R6_BASE   262u
#define BPSK_CORR_SPAD_LEAF_R6_STRIDE 8u
#define BPSK_CORR_SPAD_LEAF_R6(i)     (262u + 8u * (i))
#define BPSK_CORR_SPAD_LEAF_R7_BASE   263u
#define BPSK_CORR_SPAD_LEAF_R7_STRIDE 8u
#define BPSK_CORR_SPAD_LEAF_R7(i)     (263u + 8u * (i))
#define BPSK_CORR_SPAD_LEAF_XI0_BASE   1u
#define BPSK_CORR_SPAD_LEAF_XI0_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XI0(i)     (1u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XI1_BASE   3u
#define BPSK_CORR_SPAD_LEAF_XI1_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XI1(i)     (3u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XI2_BASE   5u
#define BPSK_CORR_SPAD_LEAF_XI2_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XI2(i)     (5u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XI3_BASE   7u
#define BPSK_CORR_SPAD_LEAF_XI3_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XI3(i)     (7u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XI4_BASE   9u
#define BPSK_CORR_SPAD_LEAF_XI4_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XI4(i)     (9u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XI5_BASE   11u
#define BPSK_CORR_SPAD_LEAF_XI5_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XI5(i)     (11u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XI6_BASE   13u
#define BPSK_CORR_SPAD_LEAF_XI6_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XI6(i)     (13u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XI7_BASE   15u
#define BPSK_CORR_SPAD_LEAF_XI7_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XI7(i)     (15u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XR0_BASE   0u
#define BPSK_CORR_SPAD_LEAF_XR0_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XR0(i)     (0u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XR1_BASE   2u
#define BPSK_CORR_SPAD_LEAF_XR1_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XR1(i)     (2u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XR2_BASE   4u
#define BPSK_CORR_SPAD_LEAF_XR2_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XR2(i)     (4u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XR3_BASE   6u
#define BPSK_CORR_SPAD_LEAF_XR3_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XR3(i)     (6u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XR4_BASE   8u
#define BPSK_CORR_SPAD_LEAF_XR4_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XR4(i)     (8u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XR5_BASE   10u
#define BPSK_CORR_SPAD_LEAF_XR5_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XR5(i)     (10u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XR6_BASE   12u
#define BPSK_CORR_SPAD_LEAF_XR6_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XR6(i)     (12u + 16u * (i))
#define BPSK_CORR_SPAD_LEAF_XR7_BASE   14u
#define BPSK_CORR_SPAD_LEAF_XR7_STRIDE 16u
#define BPSK_CORR_SPAD_LEAF_XR7(i)     (14u + 16u * (i))
#define BPSK_CORR_SPAD_SINK_PART_R_BASE   512u
#define BPSK_CORR_SPAD_SINK_PART_R_STRIDE 2u
#define BPSK_CORR_SPAD_SINK_PART_R(i)     (512u + 2u * (i))

#endif /* BPSK_CORR_LAYOUT_H */
