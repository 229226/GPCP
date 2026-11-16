/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: ldpc_enc_acc；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef LDPC_ENC_ACC_LAYOUT_H
#define LDPC_ENC_ACC_LAYOUT_H

#define LDPC_ENC_ACC_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define LDPC_ENC_ACC_LAYOUT_NPES        16u
#define LDPC_ENC_ACC_LAYOUT_WORDS       32u
#define LDPC_ENC_ACC_LAYOUT_BANKS       32u

#define LDPC_ENC_ACC_SPAD_LEAF_E0_BASE   0u
#define LDPC_ENC_ACC_SPAD_LEAF_E0_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E0(i)     (0u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E1_BASE   1u
#define LDPC_ENC_ACC_SPAD_LEAF_E1_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E1(i)     (1u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E10_BASE   10u
#define LDPC_ENC_ACC_SPAD_LEAF_E10_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E10(i)     (10u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E11_BASE   11u
#define LDPC_ENC_ACC_SPAD_LEAF_E11_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E11(i)     (11u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E12_BASE   12u
#define LDPC_ENC_ACC_SPAD_LEAF_E12_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E12(i)     (12u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E13_BASE   13u
#define LDPC_ENC_ACC_SPAD_LEAF_E13_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E13(i)     (13u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E14_BASE   14u
#define LDPC_ENC_ACC_SPAD_LEAF_E14_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E14(i)     (14u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E15_BASE   15u
#define LDPC_ENC_ACC_SPAD_LEAF_E15_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E15(i)     (15u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E16_BASE   16u
#define LDPC_ENC_ACC_SPAD_LEAF_E16_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E16(i)     (16u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E17_BASE   17u
#define LDPC_ENC_ACC_SPAD_LEAF_E17_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E17(i)     (17u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E18_BASE   18u
#define LDPC_ENC_ACC_SPAD_LEAF_E18_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E18(i)     (18u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E19_BASE   19u
#define LDPC_ENC_ACC_SPAD_LEAF_E19_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E19(i)     (19u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E2_BASE   2u
#define LDPC_ENC_ACC_SPAD_LEAF_E2_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E2(i)     (2u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E3_BASE   3u
#define LDPC_ENC_ACC_SPAD_LEAF_E3_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E3(i)     (3u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E4_BASE   4u
#define LDPC_ENC_ACC_SPAD_LEAF_E4_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E4(i)     (4u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E5_BASE   5u
#define LDPC_ENC_ACC_SPAD_LEAF_E5_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E5(i)     (5u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E6_BASE   6u
#define LDPC_ENC_ACC_SPAD_LEAF_E6_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E6(i)     (6u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E7_BASE   7u
#define LDPC_ENC_ACC_SPAD_LEAF_E7_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E7(i)     (7u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E8_BASE   8u
#define LDPC_ENC_ACC_SPAD_LEAF_E8_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E8(i)     (8u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_E9_BASE   9u
#define LDPC_ENC_ACC_SPAD_LEAF_E9_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_E9(i)     (9u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_LEAF_ZERO_BASE   20u
#define LDPC_ENC_ACC_SPAD_LEAF_ZERO_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_LEAF_ZERO(i)     (20u + 32u * (i))
#define LDPC_ENC_ACC_SPAD_SINK_ACC_BASE   512u
#define LDPC_ENC_ACC_SPAD_SINK_ACC_STRIDE 32u
#define LDPC_ENC_ACC_SPAD_SINK_ACC(i)     (512u + 32u * (i))

#endif /* LDPC_ENC_ACC_LAYOUT_H */
