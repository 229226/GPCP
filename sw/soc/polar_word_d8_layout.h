/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: polar_word_d8；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef POLAR_WORD_D8_LAYOUT_H
#define POLAR_WORD_D8_LAYOUT_H

#define POLAR_WORD_D8_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define POLAR_WORD_D8_LAYOUT_NPES        16u
#define POLAR_WORD_D8_LAYOUT_WORDS       32u
#define POLAR_WORD_D8_LAYOUT_BANKS       32u

#define POLAR_WORD_D8_SPAD_LEAF_M_BASE   64u
#define POLAR_WORD_D8_SPAD_LEAF_M_STRIDE 1u
#define POLAR_WORD_D8_SPAD_LEAF_M(i)     (64u + 1u * (i))
#define POLAR_WORD_D8_SPAD_LEAF_WA_BASE   0u
#define POLAR_WORD_D8_SPAD_LEAF_WA_STRIDE 1u
#define POLAR_WORD_D8_SPAD_LEAF_WA(i)     (0u + 1u * (i))
#define POLAR_WORD_D8_SPAD_LEAF_WB_BASE   8u
#define POLAR_WORD_D8_SPAD_LEAF_WB_STRIDE 1u
#define POLAR_WORD_D8_SPAD_LEAF_WB(i)     (8u + 1u * (i))
#define POLAR_WORD_D8_SPAD_SINK_X_BASE   0u
#define POLAR_WORD_D8_SPAD_SINK_X_STRIDE 1u
#define POLAR_WORD_D8_SPAD_SINK_X(i)     (0u + 1u * (i))

#endif /* POLAR_WORD_D8_LAYOUT_H */
