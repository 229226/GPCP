/*
 * 描述符环基址：由链接器在 .bss 之后给出（link.ld 的 `_ring_base`）。
 *
 * 以前每个 *_stages.inc 都硬编码 0x80020000 —— 程序镜像（.rodata/.bss）一长过
 * 这个地址，描述符环就会盖住静态变量：pdsch 的 `errs`、LTE 的 `e_bits/e_sc`
 * 都这样被写坏过。现在环基址跟着 .bss 走，link.ld 还带一条 ASSERT 保证环窗口
 * 不会顶到 0x80040000 起的结果区。
 */
#ifndef GPCP_RING_H
#define GPCP_RING_H

#include <stdint.h>

extern char _ring_base[];      /* link.ld: .bss 之后（>= 0x80020000） */

/* 供 MMIO 写入 / 整数运算使用（链接期确定的地址常量）。 */
#define GPCP_DESC_BASE ((uint32_t)(uintptr_t)_ring_base)

#endif /* GPCP_RING_H */
