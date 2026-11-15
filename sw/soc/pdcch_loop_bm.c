/* -----------------------------------------------------------------------------
 * G2 / B4: PDCCH self-loopback in ONE SoC program (no host round trip).
 *
 * Purpose: close the first half of docs/19 G2's acceptance criterion -- "the
 * DCI we transmit is blind-detected by our own receiver" -- on the same chip,
 * with no file written in between and no golden symbols loaded from the host.
 *
 *   params (0x8060_0000, nr_pdcch_tx.dump_td: magic, nvar, n_id, dci, n_reg,
 *           L, n_shift, -, rnti..)
 *     -> B3 transmit chain (polar transform on the array, 9 kernels)
 *     -> frequency grid: 216 QPSK REs at bins BIN0..BIN0+215, conjugate + int16
 *     -> 4096-point IFFT (fft4096, conjugate trick) + TD_SCALE + CP
 *     -> TD block at 0x8040_0000 (the layout run-pdcch expects)
 *     -> A4 receive chain: FFT -> 216 RE -> 432 LLR -> descramble
 *        -> inverse rate matching -> polar SC on the array -> RNTI blind detect
 *
 * The only thing the two halves share is the TD layout -- exactly the interface
 * that run-pdcch-tx / run-pdcch already validate separately.  One extra step on
 * the receive side: B3's transmitter applies the 38.211 7.3.2.3 REG/CCE
 * interleave, so the loopback receiver undoes it before descrambling (run-pdcch's
 * stimulus was never interleaved, which is why that chain has no such step).
 *
 * D$ coherence (AGENTS 4.2): the CPU writes the frequency grid and the TD, then
 * the engine transforms both in place, so *both* buffers need rt_dcache_flush()
 * before the CPU reads them back.  (run-pdcch does not flush because its TD was
 * written by the testbench, not by the CPU.)
 *
 * Two engine gotchas this benchmark found the hard way:
 *   1. 写完一大片 RAM 交给引擎读，要先 fence + 读回最后一个字（drain_stores）；
 *   2. 之前若用 gpcp_cgra_imem_load（DMA 写 IMEM），RTL 的 dma_tgt 曾是粘滞的，
 *      会把之后所有 DMA 静默改道去写 IMEM —— 已在 cgra_top.sv 的 D_DESC_SET 清零。
 *
 * Result area (0x8007_0000, 32-bit words; .bss 到 0x8001_0740，故主动上移):
 *   [0] 0xCAFEB4B1  [1] errors  [2] cycles  [3] array runs
 *   [4] dci_tx      [5] dci_rx  [6] rnti_tx [7] rnti_matched  [8] crc_ok
 *   [9] llr_bad (sign mismatches vs the transmitted bits)
 *   [10] llr_min |abs|  [11] llr_max |abs|  [12] n_id  [13] 0x600D
 *   [16..47]  transmitted DCI bits      [48..79]  decoded DCI bits
 *   [80..103] CRC24C bits (unmasked)    [108] max|symbol|
 *   [112..543] e_sc bits (432, the scrambled codeword the TX actually sent)
 *
 * 阶段实现（transport_block .. decode）在 pdcch_stages.inc，与协议运行器
 * sw/soc/proto_runner.c 共用同一份代码（docs/20 §8 规则 4：不许复制 benchmark）。
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"      /* rt_array_run / rt_dcache_flush */

#include "polar_inword_s0_prog.h"
#include "polar_inword_s1_prog.h"
#include "polar_inword_s2_prog.h"
#include "polar_inword_s3_prog.h"
#include "polar_inword_s4_prog.h"
#include "polar_word_d1_prog.h"
#include "polar_word_d2_prog.h"
#include "polar_word_d4_prog.h"
#include "polar_word_d8_prog.h"
#include "polar_node_prog.h"              /* polar SC node (receiver) */
#include "pdcch_luts.h"                   /* E=432 inverse rate matching */

#include "pdcch_stages.inc"               /* 10 个阶段 + 阵列/描述符辅助 */

int main(void)
{
    uint32_t *out = RESULT_BASE;
    const uint32_t *par = (const uint32_t *)PARAM_BASE;
    uint32_t t0, i;

    if (ps_setup(par) != 0) {
        out[0] = 0x0BADC4B1u;
        out[13] = 0x600Du;
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        mmio_w(EXIT_ADDR, 0x600Du);
        return 0;
    }

    t0 = mtime();

    ps_transport_block();      /* 0. DCI + CRC24C + RNTI 掩码 */
    ps_encode();               /* 1. polar 编码（阵列 9 级）+ 速率匹配 E=432 */
    ps_modulate();             /* 2. 加扰 + QPSK + REG/CCE 交织 */
    ps_dmrs();                 /* 3. 参考信号（自环里没有） */
    ps_map_to_grid();          /* 4. 频域映射（216 RE，共轭） */
    ps_ofdm_modulate();        /* 5. IFFT（阵列）+ CP -> 时域块 */
    ps_channel();              /* 6. 信道（自环 = 理想） */
    ps_transform();            /* 7a. 接收 FFT（阵列，节点拆分后独立成阶段） */
    ps_estimate();             /* 7b. 216 RE 抽取 */
    ps_equalise();             /* 8a. 逆 REG + 符号锚点 */
    ps_demap();                /* 8b. 解扰 */
    ps_decode();               /* 9. 逆速率匹配 + polar SC（阵列）+ 盲检 */

    out[0] = 0xCAFEB4B1u;
    out[1] = errs;
    out[2] = mtime() - t0;
    out[3] = runs;
    out[4] = ps_dci_word;
    out[5] = ps_dci_rx_word;
    out[6] = ps_rnti;
    out[7] = ps_rnti_match;
    out[8] = ps_crc_ok;
    out[9] = ps_llr_bad;
    out[10] = ps_llr_amin;
    out[11] = ps_llr_amax;
    out[12] = ps_n_id;
    out[13] = 0x600Du;
    for (i = 0; i < A_DCI; i++)
        out[16 + i] = dci_bits[i];
    for (i = 0; i < A_DCI; i++)
        out[48 + i] = rx_info[i];
    for (i = 0; i < CRC_LEN; i++)
        out[80 + i] = rx_crc[i];
    out[108] = ps_max_sym;
    for (i = 0; i < E_BITS; i++)
        out[112 + i] = e_sc[i];

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    mmio_w(EXIT_ADDR, 0x600Du);
    return 0;
}
