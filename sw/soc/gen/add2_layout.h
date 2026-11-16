/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: add2；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef ADD2_LAYOUT_H
#define ADD2_LAYOUT_H

#define ADD2_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define ADD2_LAYOUT_NPES        16u
#define ADD2_LAYOUT_WORDS       32u
#define ADD2_LAYOUT_BANKS       32u

#define ADD2_SPAD_LEAF_A0_BASE   832u
#define ADD2_SPAD_LEAF_A0_STRIDE 1u
#define ADD2_SPAD_LEAF_A0(i)     (832u + 1u * (i))
#define ADD2_SPAD_LEAF_A1_BASE   864u
#define ADD2_SPAD_LEAF_A1_STRIDE 1u
#define ADD2_SPAD_LEAF_A1(i)     (864u + 1u * (i))
#define ADD2_SPAD_SINK_SUM_BASE   896u
#define ADD2_SPAD_SINK_SUM_STRIDE 1u
#define ADD2_SPAD_SINK_SUM(i)     (896u + 1u * (i))

#endif /* ADD2_LAYOUT_H */
