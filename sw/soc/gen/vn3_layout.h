/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: vn3；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef VN3_LAYOUT_H
#define VN3_LAYOUT_H

#define VN3_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define VN3_LAYOUT_NPES        16u
#define VN3_LAYOUT_WORDS       32u
#define VN3_LAYOUT_BANKS       32u

#define VN3_SPAD_LEAF_CH_BASE   256u
#define VN3_SPAD_LEAF_CH_STRIDE 32u
#define VN3_SPAD_LEAF_CH(i)     (256u + 32u * (i))
#define VN3_SPAD_LEAF_M0_BASE   257u
#define VN3_SPAD_LEAF_M0_STRIDE 32u
#define VN3_SPAD_LEAF_M0(i)     (257u + 32u * (i))
#define VN3_SPAD_LEAF_M1_BASE   258u
#define VN3_SPAD_LEAF_M1_STRIDE 32u
#define VN3_SPAD_LEAF_M1(i)     (258u + 32u * (i))
#define VN3_SPAD_SINK_OM0_BASE   260u
#define VN3_SPAD_SINK_OM0_STRIDE 32u
#define VN3_SPAD_SINK_OM0(i)     (260u + 32u * (i))
#define VN3_SPAD_SINK_OM1_BASE   261u
#define VN3_SPAD_SINK_OM1_STRIDE 32u
#define VN3_SPAD_SINK_OM1(i)     (261u + 32u * (i))
#define VN3_SPAD_SINK_SG_BASE   262u
#define VN3_SPAD_SINK_SG_STRIDE 32u
#define VN3_SPAD_SINK_SG(i)     (262u + 32u * (i))
#define VN3_SPAD_SINK_SUM_OUT_BASE   259u
#define VN3_SPAD_SINK_SUM_OUT_STRIDE 32u
#define VN3_SPAD_SINK_SUM_OUT(i)     (259u + 32u * (i))

#endif /* VN3_LAYOUT_H */
