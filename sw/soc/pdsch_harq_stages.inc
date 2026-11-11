/* -----------------------------------------------------------------------------
 * PDSCH HARQ（两次传输 Chase 合并）的接收阶段实现。
 *
 * 复用 pdsch_stages.inc 的 LDPC 引擎、DMRS、Gold、解速率匹配与 qs_decode()，
 * 只新增"两次传输"的接收编排：
 *
 *   received -> dmrs -> ofdm_demodulate -> estimate -> equalise_demap   (tx0)
 *   received ->          ofdm_demodulate -> estimate -> equalise_demap   (tx1)
 *   -> decode
 *
 * 激励 = tools/pdsch_full_plan.py dump_td_bin_harq 的输出：
 *   transmission-major，每次 2 符号 × (CP+NFFT) 复数，样本已乘 TD_SCALE。
 *   rv 序列固定为 (0, 2)，k0 = 0 / 492，合并坐标 (k0+k) % USABLE。
 * -------------------------------------------------------------------------- */
#ifndef PDSCH_HARQ_STAGES_INC
#define PDSCH_HARQ_STAGES_INC

#include "pdsch_stages.inc"

#define HARQ_NTX 2
static const uint32_t harq_k0[HARQ_NTX] = { 0u, (uint32_t)(USABLE / 2) };

static uint32_t harq_q;                         /* 当前传输序号 */
static int32_t harq_hre[HARQ_NTX][NSYM], harq_him[HARQ_NTX][NSYM];
static uint32_t harq_llr_bad[HARQ_NTX], harq_llr_first[HARQ_NTX];

static int harq_setup(const uint32_t *par)
{
    (void)par;
    errs = runs = dmas = busy = 0;
    harq_q = 0;
    qs_n_rnti = 0x1234u;             /* 与 pdsch_full_plan 的 N_RNTI 一致 */
    qs_crc_ok = 0;
    qs_tb_bad = 0;
    for (uint32_t q = 0; q < HARQ_NTX; q++) {
        harq_llr_bad[q] = 0;
        harq_llr_first[q] = 0xFFFFFFFFu;
        for (int s = 0; s < NSYM; s++) {
            harq_hre[q][s] = 0;
            harq_him[q][s] = 0;
        }
    }
    for (uint32_t i = 0; i < USABLE; i++)
        rbuf[i] = 0;
    return 0;
}

static int harq_received(void) { return 0; }

static int harq_dmrs(void)
{
    build_dmrs();
    return 0;
}

static int harq_ofdm_demodulate(void)
{
    int32_t *td = (int32_t *)TD_RAM;
    for (int s = 0; s < NSYM; s++) {
        int32_t *sp = td + (harq_q * NSYM + (uint32_t)s) * SYM_WORDS
                      + 2 * CP_LEN;
#ifdef PROTO_CPU_ONLY
        fft4096_cpu(sp);
#else
        fft4096(sp);
        rt_dcache_flush();
#endif
        for (uint32_t r = 0; r < NRE; r++) {
            uint32_t idx = rev4(re_bin(r));
            yw[(s * NRE + r) * 2 + 0] = sp[2 * idx] << 4;
            yw[(s * NRE + r) * 2 + 1] = sp[2 * idx + 1] << 4;
        }
    }
    return 0;
}

static int harq_estimate(void)
{
    const int64_t den = (int64_t)NPILOT * 2 * QPSK * QPSK / H_SCALE;
    for (int s = 0; s < NSYM; s++) {
        int64_t accr = 0, acci = 0;
        int p = 0;
        for (uint32_t r = 0; r < NRE; r += 4) {
            int32_t yr = yw[(s * NRE + r) * 2 + 0];
            int32_t yi = yw[(s * NRE + r) * 2 + 1];
            int32_t dr = dmrs_re[s][p], di = dmrs_im[s][p];
            accr += (int64_t)yr * dr + (int64_t)yi * di;
            acci += (int64_t)yi * dr - (int64_t)yr * di;
            p++;
        }
        qs_hre[s] = floor_div(accr, den);
        qs_him[s] = floor_div(acci, den);
        harq_hre[harq_q][s] = qs_hre[s];
        harq_him[harq_q][s] = qs_him[s];
    }
    return 0;
}

static int harq_equalise_demap(void)
{
    uint32_t s, r, i, k, rows = E_BITS / 2u;

    qs_mm = 0;
    for (s = 0; s < NSYM; s++)
        for (r = 0; r < NRE; r++) {
            int32_t yr, yi;
            int64_t er, ei;
            if (r % 4u == 0)
                continue;
            if (qs_mm >= E_BITS / 2u)
                break;
            yr = yw[(s * NRE + r) * 2 + 0];
            yi = yw[(s * NRE + r) * 2 + 1];
            er = (int64_t)yr * qs_hre[s] + (int64_t)yi * qs_him[s];
            ei = (int64_t)yi * qs_hre[s] - (int64_t)yr * qs_him[s];
            llr_e[2 * qs_mm + 0] = (int32_t)er;
            llr_e[2 * qs_mm + 1] = (int32_t)ei;
            qs_mm++;
        }

    for (i = 0; i < rows; i++)
        for (k = 0; k < 2u; k++)
            llr_i[k * rows + i] = llr_e[2u * i + k];

    {   /* 与发射的加扰序列逐位同号（解扰前） */
        uint32_t bad = 0, first = 0xFFFFFFFFu;
        for (k = 0; k < E_BITS; k++)
            if ((uint32_t)(llr_i[k] < 0) != (uint32_t)e_sc[k]) {
                if (first == 0xFFFFFFFFu) first = k;
                bad++;
            }
        harq_llr_bad[harq_q] = bad;
        harq_llr_first[harq_q] = first;
    }

    gold_init(qs_n_rnti << 15);
    for (k = 0; k < E_BITS; k++)
        if (gold_bit(k))
            llr_i[k] = -llr_i[k];

    for (k = 0; k < E_BITS; k++)
        rbuf[(harq_k0[harq_q] + k) % USABLE] += llr_i[k];

    harq_q++;
    return 0;
}

static int harq_decode(void)
{
    uint32_t i, u = 0;

    for (i = 0; i < PUNCT; i++)
        dem[i] = 0;
    for (i = 0; i < NCODED - PUNCT; i++) {
        if (i >= FILLER0 && i < FILLER0 + FILLER_N)
            dem[PUNCT + i] = 0;
        else
            dem[PUNCT + i] = rbuf[u++];
    }
    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (i = 0; i < ZC; i++)
            llr[b][i] = dem[b * ZC + i] >> LLR_SHIFT;

    build_tables();
    return qs_decode();
}

#endif /* PDSCH_HARQ_STAGES_INC */
