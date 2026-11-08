/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: fft_butterfly；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef FFT_BUTTERFLY_LAYOUT_H
#define FFT_BUTTERFLY_LAYOUT_H

#define FFT_BUTTERFLY_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define FFT_BUTTERFLY_LAYOUT_NPES        16u
#define FFT_BUTTERFLY_LAYOUT_WORDS       32u
#define FFT_BUTTERFLY_LAYOUT_BANKS       32u

#define FFT_BUTTERFLY_SPAD_LEAF_AI_BASE   32u
#define FFT_BUTTERFLY_SPAD_LEAF_AI_STRIDE 1u
#define FFT_BUTTERFLY_SPAD_LEAF_AI(i)     (32u + 1u * (i))
#define FFT_BUTTERFLY_SPAD_LEAF_AR_BASE   0u
#define FFT_BUTTERFLY_SPAD_LEAF_AR_STRIDE 1u
#define FFT_BUTTERFLY_SPAD_LEAF_AR(i)     (0u + 1u * (i))
#define FFT_BUTTERFLY_SPAD_LEAF_BI_BASE   96u
#define FFT_BUTTERFLY_SPAD_LEAF_BI_STRIDE 1u
#define FFT_BUTTERFLY_SPAD_LEAF_BI(i)     (96u + 1u * (i))
#define FFT_BUTTERFLY_SPAD_LEAF_BR_BASE   64u
#define FFT_BUTTERFLY_SPAD_LEAF_BR_STRIDE 1u
#define FFT_BUTTERFLY_SPAD_LEAF_BR(i)     (64u + 1u * (i))
#define FFT_BUTTERFLY_SPAD_LEAF_WI_BASE   160u
#define FFT_BUTTERFLY_SPAD_LEAF_WI_STRIDE 1u
#define FFT_BUTTERFLY_SPAD_LEAF_WI(i)     (160u + 1u * (i))
#define FFT_BUTTERFLY_SPAD_LEAF_WR_BASE   128u
#define FFT_BUTTERFLY_SPAD_LEAF_WR_STRIDE 1u
#define FFT_BUTTERFLY_SPAD_LEAF_WR(i)     (128u + 1u * (i))
#define FFT_BUTTERFLY_SPAD_SINK_AOI_BASE   224u
#define FFT_BUTTERFLY_SPAD_SINK_AOI_STRIDE 1u
#define FFT_BUTTERFLY_SPAD_SINK_AOI(i)     (224u + 1u * (i))
#define FFT_BUTTERFLY_SPAD_SINK_AOR_BASE   192u
#define FFT_BUTTERFLY_SPAD_SINK_AOR_STRIDE 1u
#define FFT_BUTTERFLY_SPAD_SINK_AOR(i)     (192u + 1u * (i))
#define FFT_BUTTERFLY_SPAD_SINK_BOI_BASE   288u
#define FFT_BUTTERFLY_SPAD_SINK_BOI_STRIDE 1u
#define FFT_BUTTERFLY_SPAD_SINK_BOI(i)     (288u + 1u * (i))
#define FFT_BUTTERFLY_SPAD_SINK_BOR_BASE   256u
#define FFT_BUTTERFLY_SPAD_SINK_BOR_STRIDE 1u
#define FFT_BUTTERFLY_SPAD_SINK_BOR(i)     (256u + 1u * (i))

#endif /* FFT_BUTTERFLY_LAYOUT_H */
