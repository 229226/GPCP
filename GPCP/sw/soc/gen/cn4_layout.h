/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: cn4；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef CN4_LAYOUT_H
#define CN4_LAYOUT_H

#define CN4_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define CN4_LAYOUT_NPES        16u
#define CN4_LAYOUT_WORDS       32u
#define CN4_LAYOUT_BANKS       32u

#define CN4_SPAD_LEAF_M0_BASE   0u
#define CN4_SPAD_LEAF_M0_STRIDE 32u
#define CN4_SPAD_LEAF_M0(i)     (0u + 32u * (i))
#define CN4_SPAD_LEAF_M1_BASE   1u
#define CN4_SPAD_LEAF_M1_STRIDE 32u
#define CN4_SPAD_LEAF_M1(i)     (1u + 32u * (i))
#define CN4_SPAD_LEAF_M2_BASE   2u
#define CN4_SPAD_LEAF_M2_STRIDE 32u
#define CN4_SPAD_LEAF_M2(i)     (2u + 32u * (i))
#define CN4_SPAD_LEAF_M3_BASE   3u
#define CN4_SPAD_LEAF_M3_STRIDE 32u
#define CN4_SPAD_LEAF_M3(i)     (3u + 32u * (i))
#define CN4_SPAD_SINK_MIN1_BASE   8u
#define CN4_SPAD_SINK_MIN1_STRIDE 32u
#define CN4_SPAD_SINK_MIN1(i)     (8u + 32u * (i))
#define CN4_SPAD_SINK_MIN2_BASE   9u
#define CN4_SPAD_SINK_MIN2_STRIDE 32u
#define CN4_SPAD_SINK_MIN2(i)     (9u + 32u * (i))
#define CN4_SPAD_SINK_SIGN_BASE   10u
#define CN4_SPAD_SINK_SIGN_STRIDE 32u
#define CN4_SPAD_SINK_SIGN(i)     (10u + 32u * (i))

#endif /* CN4_LAYOUT_H */
