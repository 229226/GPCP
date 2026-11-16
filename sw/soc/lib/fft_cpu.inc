/* -----------------------------------------------------------------------------
 * CPU 版 radix-4 DIF FFT（逐位复刻 tools/fft_plan.py 的 reference_fft）。
 *
 * 用途：协议基准的 "CPU 执行" 对照路径（-DPROTO_CPU_ONLY）。旋转因子直接
 * 从同一份 twiddle ROM（0x8010_0000）读，所以输出与阵列镜像逐位一致。
 *
 * 依赖：先 include "ssb_fft.inc"（提供 FFT_LUT_RAM / FFT_TW_WORDS /
 *       FFT_TAIL_BLOCK / FFT_LUT_TAIL / FFT_LUT_4096 等常量）。
 * -------------------------------------------------------------------------- */
#ifndef GPCP_FFT_CPU_INC
#define GPCP_FFT_CPU_INC

static inline int32_t fftcpu_sx16(int32_t v)
{
    v &= 0xFFFF;
    return (v & 0x8000) ? v - 0x10000 : v;
}

static inline int32_t fftcpu_wrap32(int64_t v)
{
    return (int32_t)(uint32_t)v;
}

static void fftcpu_dft4(const int32_t *vr, const int32_t *vi, uint32_t m,
                        int32_t *or_, int32_t *oi)
{
    int64_t ar = 0, ai = 0;
    for (uint32_t j = 0; j < 4; j++) {
        uint32_t f = (j * m) & 3u;
        if (f == 0)      { ar += vr[j]; ai += vi[j]; }
        else if (f == 1) { ar += vi[j]; ai -= vr[j]; }
        else if (f == 2) { ar -= vr[j]; ai -= vi[j]; }
        else             { ar -= vi[j]; ai += vr[j]; }
    }
    *or_ = fftcpu_wrap32(ar);
    *oi = fftcpu_wrap32(ai);
}

/* twiddle ROM 是"每个 32-bit 字放一个 int16（低半字）"，不是打包对。 */
static void fftcpu_twiddle(int32_t ar, int32_t ai, int32_t wr, int32_t wi,
                           int32_t *or_, int32_t *oi)
{
    int32_t tr = fftcpu_sx16(ar) * fftcpu_sx16(wr)
               - fftcpu_sx16(ai) * fftcpu_sx16(wi);
    int32_t ti = fftcpu_sx16(ar) * fftcpu_sx16(wi)
               + fftcpu_sx16(ai) * fftcpu_sx16(wr);
    *or_ = fftcpu_wrap32(tr) >> 17;
    *oi = fftcpu_wrap32(ti) >> 17;
}

/* n = 256 / 1024 / 4096；数据在 RAM 中按 [re,im] 交错，原地变换，
 * 输出仍在 digit-reversed 位置（与阵列引擎同一约定）。 */
static void fft_cpu_n(int32_t *d, uint32_t n, uint32_t lut_ofs)
{
    const uint32_t *lut = (const uint32_t *)FFT_LUT_RAM;
    uint32_t tiles = n / 256u, stages = 0;
    while ((n >> (2u * stages)) >= 256u)
        stages++;

    for (uint32_t s = 0; s < stages; s++) {
        uint32_t L = n >> (2u * s), M = L / 4u;
        for (uint32_t q = 0; q < tiles; q++) {
            uint32_t base = q * FFT_TILE_BFLIES;
            uint32_t blk = base / M, t0 = base % M;
            for (uint32_t bi = 0; bi < FFT_TILE_BFLIES; bi++) {
                uint32_t t = t0 + bi;
                uint32_t site = blk * L + t;
                int32_t vr[4], vi[4], ur, ui;
                for (uint32_t j = 0; j < 4; j++) {
                    vr[j] = d[2u * (site + j * M)];
                    vi[j] = d[2u * (site + j * M) + 1u];
                }
                for (uint32_t m = 0; m < 4; m++) {
                    fftcpu_dft4(vr, vi, m, &ur, &ui);
                    if (m == 0) {
                        ur >>= 2; ui >>= 2;
                    } else {
                        uint32_t wi = lut_ofs + (s * tiles + q) * FFT_TW_WORDS
                                    + (m - 1u) * 128u + bi * 2u;
                        fftcpu_twiddle(ur, ui,
                                       (int16_t)(lut[wi] & 0xFFFFu),
                                       (int16_t)(lut[wi + 1u] & 0xFFFFu),
                                       &ur, &ui);
                    }
                    d[2u * (site + m * M)] = ur;
                    d[2u * (site + m * M) + 1u] = ui;
                }
            }
        }
    }

    for (uint32_t b = 0; b < n / FFT_TAIL_BLOCK; b++) {
        uint32_t off = b * FFT_TAIL_BLOCK;
        static const uint32_t pass_L[3]    = { 64u, 16u, 4u };
        static const uint32_t pass_nsub[3] = { 1u, 4u, 16u };
        static const int32_t  pass_tw[3]   = { 0, 96, -1 };
        static const uint32_t pass_str[3]  = { 32u, 8u, 0u };
        for (uint32_t pass = 0; pass < 3; pass++) {
            uint32_t L = pass_L[pass], M = L / 4u;
            for (uint32_t sb = 0; sb < pass_nsub[pass]; sb++) {
                for (uint32_t t = 0; t < M; t++) {
                    uint32_t site = off + sb * L + t;
                    int32_t vr[4], vi[4], ur, ui;
                    for (uint32_t j = 0; j < 4; j++) {
                        vr[j] = d[2u * (site + j * M)];
                        vi[j] = d[2u * (site + j * M) + 1u];
                    }
                    for (uint32_t m = 0; m < 4; m++) {
                        fftcpu_dft4(vr, vi, m, &ur, &ui);
                        if (m == 0 || pass_tw[pass] < 0) {
                            ur >>= 2; ui >>= 2;
                        } else {
                            uint32_t wi = FFT_LUT_TAIL + (uint32_t)pass_tw[pass]
                                        + (m - 1u) * pass_str[pass] + t * 2u;
                            fftcpu_twiddle(ur, ui,
                                           (int16_t)(lut[wi] & 0xFFFFu),
                                           (int16_t)(lut[wi + 1u] & 0xFFFFu),
                                           &ur, &ui);
                        }
                        d[2u * (site + m * M)] = ur;
                        d[2u * (site + m * M) + 1u] = ui;
                    }
                }
            }
        }
    }
}

static void fft4096_cpu(int32_t *data)
{
    fft_cpu_n(data, 4096u, FFT_LUT_4096);
}

#endif /* GPCP_FFT_CPU_INC */
