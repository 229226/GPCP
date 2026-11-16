/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: matmul；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef MATMUL_LAYOUT_H
#define MATMUL_LAYOUT_H

#define MATMUL_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define MATMUL_LAYOUT_NPES        16u
#define MATMUL_LAYOUT_WORDS       32u
#define MATMUL_LAYOUT_BANKS       32u

#define MATMUL_SPAD_LEAF_A0_BASE   0u
#define MATMUL_SPAD_LEAF_A0_STRIDE 8u
#define MATMUL_SPAD_LEAF_A0(i)     (0u + 8u * (i))
#define MATMUL_SPAD_LEAF_A1_BASE   1u
#define MATMUL_SPAD_LEAF_A1_STRIDE 8u
#define MATMUL_SPAD_LEAF_A1(i)     (1u + 8u * (i))
#define MATMUL_SPAD_LEAF_A2_BASE   2u
#define MATMUL_SPAD_LEAF_A2_STRIDE 8u
#define MATMUL_SPAD_LEAF_A2(i)     (2u + 8u * (i))
#define MATMUL_SPAD_LEAF_A3_BASE   3u
#define MATMUL_SPAD_LEAF_A3_STRIDE 8u
#define MATMUL_SPAD_LEAF_A3(i)     (3u + 8u * (i))
#define MATMUL_SPAD_LEAF_B0_BASE   4u
#define MATMUL_SPAD_LEAF_B0_STRIDE 8u
#define MATMUL_SPAD_LEAF_B0(i)     (4u + 8u * (i))
#define MATMUL_SPAD_LEAF_B1_BASE   5u
#define MATMUL_SPAD_LEAF_B1_STRIDE 8u
#define MATMUL_SPAD_LEAF_B1(i)     (5u + 8u * (i))
#define MATMUL_SPAD_LEAF_B2_BASE   6u
#define MATMUL_SPAD_LEAF_B2_STRIDE 8u
#define MATMUL_SPAD_LEAF_B2(i)     (6u + 8u * (i))
#define MATMUL_SPAD_LEAF_B3_BASE   7u
#define MATMUL_SPAD_LEAF_B3_STRIDE 8u
#define MATMUL_SPAD_LEAF_B3(i)     (7u + 8u * (i))
#define MATMUL_SPAD_SINK_SUM_BASE   256u
#define MATMUL_SPAD_SINK_SUM_STRIDE 1u
#define MATMUL_SPAD_SINK_SUM(i)     (256u + 1u * (i))

#endif /* MATMUL_LAYOUT_H */
