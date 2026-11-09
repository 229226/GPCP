/* -----------------------------------------------------------------------------
 * 2x2 MIMO PDSCH receiver: FDM pilots -> channel estimate -> MMSE -> hard bits.
 *
 *   two TD blocks (Rx 0 / Rx 1), one symbol each
 *     -> one 4096-point FFT per antenna
 *     -> 4 H_SCALE-scaled coefficients from the 60 FDM pilots (30/layer)
 *     -> per data RE: G = H^H H + lambda I, b = H^H y, solve G s = b
 *     -> hard bits (2 per layer symbol, layer 0 then layer 1) -> 720 bits
 *
 * The 2x2 solve is written out and uses one complex division:
 *     det  = g00*g11 - g01*g10            (g10 = conj(g01))
 *     s0   = (b0*g11 - b1*g01) * conj(det) / |det|^2
 *     s1   = (b1*g00 - b0*g10) * conj(det) / |det|^2
 * so everything stays in int64 (num*conj(det) ~ 3e16 at the worst case).
 *
 * Result area (0x8007_0000):
 *   [0] 0xCAFEBABE  [1] errors  [2] cycles  [6] 0x600D
 *   [7] CGRA MMSE runs
 *   [8..15]  four channel coefficients (re,im)
 *   [16] 180 (data REs)  [17] lambda  [18] bit errors vs the local re-encode
 *   [19] bit count (720)
 *   [20 .. 20+179]  four hard bits packed per data RE (low nibble)
 *
 * 阶段实现（received .. mimo_equalise）在 mimo_stages.inc，与协议运行器
 * sw/soc/proto_runner.c 共用同一份代码（docs/20 §8 规则 4）。
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"
#include "mmse_2x1_prog.h"

#include "mimo_stages.inc"

int main(void)
{
    uint32_t *out = RESULT_BASE;
    uint32_t t0;

    if (ms_setup(0) != 0) {
        out[0] = 0x0BADC0DEu;
        out[6] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 0;
    }

    t0 = mtime();
    ms_received();          /* 0. 两块时域样本已由宿主放进 0x8040_0000 */
    ms_dmrs();              /* 1. 两层 FDM 导频序列 */
    ms_ofdm_demodulate();   /* 2. 两个天线的 4096 点 FFT [array] */
    ms_estimate();          /* 3. LS + G/lambda/det */
    ms_mimo_equalise();     /* 4. b = H^H y [array] + 2x2 求解 + 硬判 */

    out[0] = 0xCAFEBABEu;
    out[1] = errs;
    out[2] = mtime() - t0;
    out[6] = 0x600Du;
    out[7] = mmse_runs;          /* >0 证明 b = H^H y 真的跑在 CGRA 上 */
    for (int rx = 0; rx < 2; rx++)
        for (int l = 0; l < 2; l++) {
            out[8 + 4 * rx + 2 * l] = (uint32_t)(int32_t)ms_hre[rx][l];
            out[9 + 4 * rx + 2 * l] = (uint32_t)(int32_t)ms_him[rx][l];
        }
    out[16] = NDATA;
    out[17] = (uint32_t)ms_lam;
    out[18] = ms_bit_err;
    out[19] = NBITS;
    for (int i = 0; i < NDATA; i++)
        out[20 + i] = ms_bits[i];

    *(volatile uint32_t *)EXIT_ADDR = 1u;
    return 0;
}
