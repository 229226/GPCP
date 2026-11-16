/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: polar_node；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef POLAR_NODE_LAYOUT_H
#define POLAR_NODE_LAYOUT_H

#define POLAR_NODE_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define POLAR_NODE_LAYOUT_NPES        16u
#define POLAR_NODE_LAYOUT_WORDS       32u
#define POLAR_NODE_LAYOUT_BANKS       32u

#define POLAR_NODE_SPAD_LEAF_A_BASE   0u
#define POLAR_NODE_SPAD_LEAF_A_STRIDE 1u
#define POLAR_NODE_SPAD_LEAF_A(i)     (0u + 1u * (i))
#define POLAR_NODE_SPAD_LEAF_B_BASE   32u
#define POLAR_NODE_SPAD_LEAF_B_STRIDE 1u
#define POLAR_NODE_SPAD_LEAF_B(i)     (32u + 1u * (i))
#define POLAR_NODE_SPAD_LEAF_BETA_BASE   64u
#define POLAR_NODE_SPAD_LEAF_BETA_STRIDE 1u
#define POLAR_NODE_SPAD_LEAF_BETA(i)     (64u + 1u * (i))
#define POLAR_NODE_SPAD_SINK_F_BASE   512u
#define POLAR_NODE_SPAD_SINK_F_STRIDE 1u
#define POLAR_NODE_SPAD_SINK_F(i)     (512u + 1u * (i))
#define POLAR_NODE_SPAD_SINK_G_BASE   544u
#define POLAR_NODE_SPAD_SINK_G_STRIDE 1u
#define POLAR_NODE_SPAD_SINK_G(i)     (544u + 1u * (i))

#endif /* POLAR_NODE_LAYOUT_H */
