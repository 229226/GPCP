/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: turbo_acs；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef TURBO_ACS_LAYOUT_H
#define TURBO_ACS_LAYOUT_H

#define TURBO_ACS_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define TURBO_ACS_LAYOUT_NPES        16u
#define TURBO_ACS_LAYOUT_WORDS       32u
#define TURBO_ACS_LAYOUT_BANKS       32u

#define TURBO_ACS_SPAD_LEAF_A0_BASE   0u
#define TURBO_ACS_SPAD_LEAF_A0_STRIDE 32u
#define TURBO_ACS_SPAD_LEAF_A0(i)     (0u + 32u * (i))
#define TURBO_ACS_SPAD_LEAF_A1_BASE   2u
#define TURBO_ACS_SPAD_LEAF_A1_STRIDE 32u
#define TURBO_ACS_SPAD_LEAF_A1(i)     (2u + 32u * (i))
#define TURBO_ACS_SPAD_LEAF_G0_BASE   1u
#define TURBO_ACS_SPAD_LEAF_G0_STRIDE 32u
#define TURBO_ACS_SPAD_LEAF_G0(i)     (1u + 32u * (i))
#define TURBO_ACS_SPAD_LEAF_G1_BASE   3u
#define TURBO_ACS_SPAD_LEAF_G1_STRIDE 32u
#define TURBO_ACS_SPAD_LEAF_G1(i)     (3u + 32u * (i))
#define TURBO_ACS_SPAD_SINK_M_BASE   512u
#define TURBO_ACS_SPAD_SINK_M_STRIDE 32u
#define TURBO_ACS_SPAD_SINK_M(i)     (512u + 32u * (i))

#endif /* TURBO_ACS_LAYOUT_H */
