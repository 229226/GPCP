/*
 * GPCP SoC - bare-metal I/O helpers
 *
 * These two accessors were copy-pasted into 26 benchmark files, byte for
 * byte identical in every one of them.  Every register access in the project
 * goes through them, so the ABI belongs in exactly one place: change it here
 * and every benchmark picks it up on the next build.
 *
 * It has already drifted once (sw/soc/cgra_bm.c carries a u64/u32 variant),
 * which is exactly what duplication does over time.
 *
 * No libc: safe with -nostdlib, same rule as gpcp_cgra.h.
 */
#ifndef GPCP_SOC_IO_H
#define GPCP_SOC_IO_H

#include <stdint.h>

static inline uint32_t mmio_r(uintptr_t a) { return *(volatile uint32_t *)a; }
static inline void mmio_w(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

#endif /* GPCP_SOC_IO_H */
