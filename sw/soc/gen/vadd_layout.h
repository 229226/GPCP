/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: vadd；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef VADD_LAYOUT_H
#define VADD_LAYOUT_H

#define VADD_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define VADD_LAYOUT_NPES        16u
#define VADD_LAYOUT_WORDS       32u
#define VADD_LAYOUT_BANKS       32u

#define VADD_SPAD_LEAF_A_BASE   832u
#define VADD_SPAD_LEAF_A_STRIDE 1u
#define VADD_SPAD_LEAF_A(i)     (832u + 1u * (i))
#define VADD_SPAD_LEAF_B_BASE   864u
#define VADD_SPAD_LEAF_B_STRIDE 1u
#define VADD_SPAD_LEAF_B(i)     (864u + 1u * (i))
#define VADD_SPAD_SINK_SUM_BASE   896u
#define VADD_SPAD_SINK_SUM_STRIDE 1u
#define VADD_SPAD_SINK_SUM(i)     (896u + 1u * (i))

#endif /* VADD_LAYOUT_H */
