/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * kernel: polar_inword_s3；target: gpcp-cgra-4x4-v1。
 * 每个叶子/汇的 flat spad 下标 = BASE + STRIDE*实例号；
 * 字位/区域由 cgra/target.yaml 决定，C 侧不要写死坐标。
 * 编译后的最终 spad 布局（含自动 bank 分配）
 */
#ifndef POLAR_INWORD_S3_LAYOUT_H
#define POLAR_INWORD_S3_LAYOUT_H

#define POLAR_INWORD_S3_LAYOUT_TARGET      "gpcp-cgra-4x4-v1"
#define POLAR_INWORD_S3_LAYOUT_NPES        16u
#define POLAR_INWORD_S3_LAYOUT_WORDS       32u
#define POLAR_INWORD_S3_LAYOUT_BANKS       32u

#define POLAR_INWORD_S3_SPAD_LEAF_MASK_BASE   32u
#define POLAR_INWORD_S3_SPAD_LEAF_MASK_STRIDE 0u
#define POLAR_INWORD_S3_SPAD_LEAF_MASK(i)     (32u + 0u * (i))
#define POLAR_INWORD_S3_SPAD_LEAF_W_BASE   0u
#define POLAR_INWORD_S3_SPAD_LEAF_W_STRIDE 1u
#define POLAR_INWORD_S3_SPAD_LEAF_W(i)     (0u + 1u * (i))
#define POLAR_INWORD_S3_SPAD_SINK_W2_BASE   0u
#define POLAR_INWORD_S3_SPAD_SINK_W2_STRIDE 1u
#define POLAR_INWORD_S3_SPAD_SINK_W2(i)     (0u + 1u * (i))

#endif /* POLAR_INWORD_S3_LAYOUT_H */
