/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: chnest16；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef CHNEST16_LAYOUT_H
#define CHNEST16_LAYOUT_H

#define CHNEST16_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define CHNEST16_LAYOUT_NPES        16u
#define CHNEST16_LAYOUT_WORDS       32u
#define CHNEST16_LAYOUT_BANKS       32u

#define CHNEST16_SPAD_LEAF_XI_BASE   96u
#define CHNEST16_SPAD_LEAF_XI_STRIDE 1u
#define CHNEST16_SPAD_LEAF_XI(i)     (96u + 1u * (i))
#define CHNEST16_SPAD_LEAF_XR_BASE   64u
#define CHNEST16_SPAD_LEAF_XR_STRIDE 1u
#define CHNEST16_SPAD_LEAF_XR(i)     (64u + 1u * (i))
#define CHNEST16_SPAD_LEAF_YI_BASE   32u
#define CHNEST16_SPAD_LEAF_YI_STRIDE 1u
#define CHNEST16_SPAD_LEAF_YI(i)     (32u + 1u * (i))
#define CHNEST16_SPAD_LEAF_YR_BASE   0u
#define CHNEST16_SPAD_LEAF_YR_STRIDE 1u
#define CHNEST16_SPAD_LEAF_YR(i)     (0u + 1u * (i))
#define CHNEST16_SPAD_SINK_HI_BASE   160u
#define CHNEST16_SPAD_SINK_HI_STRIDE 1u
#define CHNEST16_SPAD_SINK_HI(i)     (160u + 1u * (i))
#define CHNEST16_SPAD_SINK_HR_BASE   128u
#define CHNEST16_SPAD_SINK_HR_STRIDE 1u
#define CHNEST16_SPAD_SINK_HR(i)     (128u + 1u * (i))

#endif /* CHNEST16_LAYOUT_H */
