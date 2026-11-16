// 由 tools/gen/gen_cgra_desc.py 从 rtl/cgra/cgra_desc64.yml 生成，不要手改。
#ifndef GPCP_DESC64_H
#define GPCP_DESC64_H

#include <stdint.h>

#define GPCP_DESC64_WORDS 16u

/* src: word 0 [31:0] */
static inline void gpcp_d64_set_src(uint32_t *d, uint32_t v)
{
    d[0] = (d[0] & ~0xffffffffu) | ((v & 0xffffffffu) << 0);
}
static inline uint32_t gpcp_d64_src(const uint32_t *d)
{
    return (d[0] >> 0) & 0xffffffffu;
}

/* spad: word 1 [15:0] */
static inline void gpcp_d64_set_spad(uint32_t *d, uint32_t v)
{
    d[1] = (d[1] & ~0x0000ffffu) | ((v & 0x0000ffffu) << 0);
}
static inline uint32_t gpcp_d64_spad(const uint32_t *d)
{
    return (d[1] >> 0) & 0x0000ffffu;
}

/* page: word 1 [16:16] */
static inline void gpcp_d64_set_page(uint32_t *d, uint32_t v)
{
    d[1] = (d[1] & ~0x00010000u) | ((v & 0x00000001u) << 16);
}
static inline uint32_t gpcp_d64_page(const uint32_t *d)
{
    return (d[1] >> 16) & 0x00000001u;
}

/* dir: word 1 [17:17] */
static inline void gpcp_d64_set_dir(uint32_t *d, uint32_t v)
{
    d[1] = (d[1] & ~0x00020000u) | ((v & 0x00000001u) << 17);
}
static inline uint32_t gpcp_d64_dir(const uint32_t *d)
{
    return (d[1] >> 17) & 0x00000001u;
}

/* irq: word 1 [18:18] */
static inline void gpcp_d64_set_irq(uint32_t *d, uint32_t v)
{
    d[1] = (d[1] & ~0x00040000u) | ((v & 0x00000001u) << 18);
}
static inline uint32_t gpcp_d64_irq(const uint32_t *d)
{
    return (d[1] >> 18) & 0x00000001u;
}

/* tgt_rot: word 1 [19:19] */
static inline void gpcp_d64_set_tgt_rot(uint32_t *d, uint32_t v)
{
    d[1] = (d[1] & ~0x00080000u) | ((v & 0x00000001u) << 19);
}
static inline uint32_t gpcp_d64_tgt_rot(const uint32_t *d)
{
    return (d[1] >> 19) & 0x00000001u;
}

/* in_len: word 2 [15:0] */
static inline void gpcp_d64_set_in_len(uint32_t *d, uint32_t v)
{
    d[2] = (d[2] & ~0x0000ffffu) | ((v & 0x0000ffffu) << 0);
}
static inline uint32_t gpcp_d64_in_len(const uint32_t *d)
{
    return (d[2] >> 0) & 0x0000ffffu;
}

/* stride: word 3 [15:0] */
static inline void gpcp_d64_set_stride(uint32_t *d, uint32_t v)
{
    d[3] = (d[3] & ~0x0000ffffu) | ((v & 0x0000ffffu) << 0);
}
static inline uint32_t gpcp_d64_stride(const uint32_t *d)
{
    return (d[3] >> 0) & 0x0000ffffu;
}

/* row: word 3 [31:16] */
static inline void gpcp_d64_set_row(uint32_t *d, uint32_t v)
{
    d[3] = (d[3] & ~0xffff0000u) | ((v & 0x0000ffffu) << 16);
}
static inline uint32_t gpcp_d64_row(const uint32_t *d)
{
    return (d[3] >> 16) & 0x0000ffffu;
}

/* cycles: word 6 [15:0] */
static inline void gpcp_d64_set_cycles(uint32_t *d, uint32_t v)
{
    d[6] = (d[6] & ~0x0000ffffu) | ((v & 0x0000ffffu) << 0);
}
static inline uint32_t gpcp_d64_cycles(const uint32_t *d)
{
    return (d[6] >> 0) & 0x0000ffffu;
}

/* arr_page: word 6 [16:16] */
static inline void gpcp_d64_set_arr_page(uint32_t *d, uint32_t v)
{
    d[6] = (d[6] & ~0x00010000u) | ((v & 0x00000001u) << 16);
}
static inline uint32_t gpcp_d64_arr_page(const uint32_t *d)
{
    return (d[6] >> 16) & 0x00000001u;
}

/* arr_en: word 6 [31:31] */
static inline void gpcp_d64_set_arr_en(uint32_t *d, uint32_t v)
{
    d[6] = (d[6] & ~0x80000000u) | ((v & 0x00000001u) << 31);
}
static inline uint32_t gpcp_d64_arr_en(const uint32_t *d)
{
    return (d[6] >> 31) & 0x00000001u;
}

/* out_len: word 8 [15:0] */
static inline void gpcp_d64_set_out_len(uint32_t *d, uint32_t v)
{
    d[8] = (d[8] & ~0x0000ffffu) | ((v & 0x0000ffffu) << 0);
}
static inline uint32_t gpcp_d64_out_len(const uint32_t *d)
{
    return (d[8] >> 0) & 0x0000ffffu;
}

/* out_spad: word 8 [31:16] */
static inline void gpcp_d64_set_out_spad(uint32_t *d, uint32_t v)
{
    d[8] = (d[8] & ~0xffff0000u) | ((v & 0x0000ffffu) << 16);
}
static inline uint32_t gpcp_d64_out_spad(const uint32_t *d)
{
    return (d[8] >> 16) & 0x0000ffffu;
}

/* out_en: word 9 [0:0] */
static inline void gpcp_d64_set_out_en(uint32_t *d, uint32_t v)
{
    d[9] = (d[9] & ~0x00000001u) | ((v & 0x00000001u) << 0);
}
static inline uint32_t gpcp_d64_out_en(const uint32_t *d)
{
    return (d[9] >> 0) & 0x00000001u;
}

/* out_dst: word 10 [31:0] */
static inline void gpcp_d64_set_out_dst(uint32_t *d, uint32_t v)
{
    d[10] = (d[10] & ~0xffffffffu) | ((v & 0xffffffffu) << 0);
}
static inline uint32_t gpcp_d64_out_dst(const uint32_t *d)
{
    return (d[10] >> 0) & 0xffffffffu;
}

/* rep: word 12 [15:0] */
static inline void gpcp_d64_set_rep(uint32_t *d, uint32_t v)
{
    d[12] = (d[12] & ~0x0000ffffu) | ((v & 0x0000ffffu) << 0);
}
static inline uint32_t gpcp_d64_rep(const uint32_t *d)
{
    return (d[12] >> 0) & 0x0000ffffu;
}

/* in_step: word 12 [31:16] */
static inline void gpcp_d64_set_in_step(uint32_t *d, uint32_t v)
{
    d[12] = (d[12] & ~0xffff0000u) | ((v & 0x0000ffffu) << 16);
}
static inline uint32_t gpcp_d64_in_step(const uint32_t *d)
{
    return (d[12] >> 16) & 0x0000ffffu;
}

/* out_step: word 13 [15:0] */
static inline void gpcp_d64_set_out_step(uint32_t *d, uint32_t v)
{
    d[13] = (d[13] & ~0x0000ffffu) | ((v & 0x0000ffffu) << 0);
}
static inline uint32_t gpcp_d64_out_step(const uint32_t *d)
{
    return (d[13] >> 0) & 0x0000ffffu;
}

#endif /* GPCP_DESC64_H */
