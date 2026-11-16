/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: cplx_mul；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef CPLX_MUL_LAYOUT_H
#define CPLX_MUL_LAYOUT_H

#define CPLX_MUL_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define CPLX_MUL_LAYOUT_NPES        16u
#define CPLX_MUL_LAYOUT_WORDS       32u
#define CPLX_MUL_LAYOUT_BANKS       32u

#define CPLX_MUL_SPAD_LEAF_XI_BASE   3u
#define CPLX_MUL_SPAD_LEAF_XI_STRIDE 32u
#define CPLX_MUL_SPAD_LEAF_XI(i)     (3u + 32u * (i))
#define CPLX_MUL_SPAD_LEAF_XR_BASE   2u
#define CPLX_MUL_SPAD_LEAF_XR_STRIDE 32u
#define CPLX_MUL_SPAD_LEAF_XR(i)     (2u + 32u * (i))
#define CPLX_MUL_SPAD_LEAF_YI_BASE   1u
#define CPLX_MUL_SPAD_LEAF_YI_STRIDE 32u
#define CPLX_MUL_SPAD_LEAF_YI(i)     (1u + 32u * (i))
#define CPLX_MUL_SPAD_LEAF_YR_BASE   0u
#define CPLX_MUL_SPAD_LEAF_YR_STRIDE 32u
#define CPLX_MUL_SPAD_LEAF_YR(i)     (0u + 32u * (i))
#define CPLX_MUL_SPAD_SINK_HI_BASE   513u
#define CPLX_MUL_SPAD_SINK_HI_STRIDE 32u
#define CPLX_MUL_SPAD_SINK_HI(i)     (513u + 32u * (i))
#define CPLX_MUL_SPAD_SINK_HR_BASE   512u
#define CPLX_MUL_SPAD_SINK_HR_STRIDE 32u
#define CPLX_MUL_SPAD_SINK_HR(i)     (512u + 32u * (i))

#endif /* CPLX_MUL_LAYOUT_H */
