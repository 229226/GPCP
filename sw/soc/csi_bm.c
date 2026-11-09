/* -----------------------------------------------------------------------------
 * CSI-RS (38.211 7.4.1.5) + wideband CQI report on the SoC.
 *
 *   1 OFDM symbol -> 4096-point FFT (ssb_fft.inc) -> 240-RE window
 *     -> 80 CSI-RS REs (density 3) -> LS estimate A[i] = Y[r_i]*conj(r_i)/|s|^2
 *     -> Ps = |mean(A)|^2, Pn = sum|A[i]-A[i-1]|^2 / (2(N-1))
 *     -> snr_q8 = Ps*256/Pn -> CQI by 38.214 threshold table (no log needed)
 *
 * The adjacent-difference noise estimate is blind to a uniform front-end gain,
 * so this gate stays tight even though the absolute FFT scale is not.
 *
 * Result area (0x8007_0000, 32-bit words):
 *   [0] 0xCAFEBABE  [1] errors  [2] cycles  [3] array runs  [4] DMAs
 *   [5] CQI         [6] 0x600D  [7] snr_q8   [8] Ps>>40      [9] Pn>>10
 *   [10..17] A[0..3] (re,im)    [18] NCSI     [19] c_init
 *
 * 阶段实现（received -> dmrs -> ofdm_demodulate -> measure）在 csi_stages.inc，
 * 与协议运行器 sw/soc/proto_runner.c 共用同一份代码（docs/20 §8 规则 4）。
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"

#include "csi_cqi_tab.h"

#include "csi_stages.inc"

int main(void)
{
    uint32_t *out = RESULT_BASE;
    const uint32_t *par = (const uint32_t *)0x80600000ul;
    uint32_t t0;

    if (cs_setup(par) != 0) {
        out[0] = 0x0BADCAFEu;
        out[6] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
        return 0;
    }

    t0 = mtime();
    cs_received();          /* 0. 时域样本已由宿主放进 0x8040_0000 */
    cs_dmrs();              /* 1. CSI-RS 参考序列（Gold，c_init 见 7.4.1.5） */
    cs_ofdm_demodulate();   /* 2. 4096 点 FFT [array] */
    cs_measure();           /* 3. LS + 差值噪声 + SNR + CQI */

    out[0] = 0xCAFEBABEu;
    out[1] = errs;
    out[2] = mtime() - t0;
    out[3] = runs;          /* fft4096 的阵列运行数（共享阶段代码统计） */
    out[4] = 0;
    out[5] = cs_cqi;
    out[6] = 0x600Du;
    out[7] = cs_snr_q8;
    out[8] = cs_ps40;
    out[9] = cs_pn10;
    for (int i = 0; i < 4; i++) {
        out[10 + 2 * i] = (uint32_t)(int32_t)cs_a_re[i];
        out[11 + 2 * i] = (uint32_t)(int32_t)cs_a_im[i];
    }
    out[18] = (uint32_t)NCSI;
    out[19] = cs_cinit_v;

    *(volatile uint32_t *)EXIT_ADDR = 1u;
    return 0;
}
