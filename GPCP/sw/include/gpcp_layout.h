/* 由 cgra/tools/layout_contract.py 生成，勿手改。
 * 硬件目标：gpcp-cgra-4x4-v1；改几何只改 cgra/target.yaml 后重跑 `make kernels`。
 * C 侧的 spad 算术与命名区域只允许引用本文件。 */
#ifndef GPCP_LAYOUT_H
#define GPCP_LAYOUT_H

#define GPCP_LAYOUT_TARGET       "gpcp-cgra-4x4-v1"
#define GPCP_LAYOUT_NPES         16u
#define GPCP_LAYOUT_BANKS        32u
#define GPCP_LAYOUT_WORDS        32u
#define GPCP_LAYOUT_WORD_BITS    5u
#define GPCP_LAYOUT_WORD_MASK    ((1u << GPCP_LAYOUT_WORD_BITS) - 1u)
#define GPCP_LAYOUT_BANK_MASK    ((1u << 5u) - 1u)
#define GPCP_LAYOUT_IDX_BANK_MASK ((GPCP_LAYOUT_BANKS * 2u) - 1u)
#define GPCP_LAYOUT_PAGE_BANKS   32u
#define GPCP_LAYOUT_FLAT_MAX     1023u
#define GPCP_LAYOUT_REGS         8u
#define GPCP_LAYOUT_IMEM_CYCLES  512u

#define GPCP_LAYOUT_REGION_IN           0u
#define GPCP_LAYOUT_REGION_CONST        1u
#define GPCP_LAYOUT_REGION_DATA         2u
#define GPCP_LAYOUT_REGION_REF          8u
#define GPCP_LAYOUT_REGION_MIN1         10u
#define GPCP_LAYOUT_REGION_MIN2         11u
#define GPCP_LAYOUT_REGION_SIGN         12u
#define GPCP_LAYOUT_REGION_OUT          16u
#define GPCP_LAYOUT_REGION_LLR          24u
#define GPCP_LAYOUT_REGION_SCRATCH      24u
#define GPCP_LAYOUT_REGION_HARD         25u
#define GPCP_LAYOUT_REGION_SENTINEL     30u

#endif /* GPCP_LAYOUT_H */
