/* -----------------------------------------------------------------------------
 * SS/PBCH block (SSB) receive chain on the CGRA (SoC level)
 *
 *   1. PSS detection      : 3 candidates, bpsk_corr microcode, 52 cycles
 *   2. SSS search         : 336 candidates, same microcode
 *   3. DMRS channel estimate per symbol (28 pilots, 38.211 Gold sequence)
 *   4. QPSK demod + PBCH descrambling -> 512 soft LLRs
 *   5. Polar SC decode (N=512, K=32) : polar_node microcode over the SC tree
 *   6. compare the decoded MIB payload with the transmitted one
 *
 * The grid is frequency domain (240 RE x 4 symbols); the 4096-point FFT is
 * verified separately by `make -C sim/soc run-fft`.  Golden model:
 * tools/ssb_plan.py (same Gold sequences, channel gain (600,300), noise +-512,
 * LLR scaling by 2^13, frozen set bitrev9(i) < N-K).
 *
 * Results at 0x8001_0000 (words):
 *   [0] N_ID2 detected  [1] N_ID1 detected  [2] true N_ID2  [3] true N_ID1
 *   [4..5] PSS correlation (re,im) of candidate 0, [6..7] cand 1, [8..9] cand 2
 *   [10..11] SSS correlation (re,im) of the winner
 *   [12..19] channel estimates (re,im) per symbol
 *   [20..51] decoded payload   [52..83] transmitted payload
 *   [84] errors  [85] cycles  [86] array runs  [87] 0x600D
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "pss_corr_prog.h"      /* 编译器生成的 PSS/SSS 复数相关 */
#include "pss_corr_layout.h"
#include "polar_node_prog.h"      /* 编译器生成的 polar SC 节点微码 */

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#define NPES             16
#define CORR_CYCLES   PSS_CORR_PROG_CYCLES
#define NODE_CYCLES   POLAR_NODE_PROG_CYCLES
#define RING_DEPTH       16
#define NREP             127
#define NSYM             4
#define NRE              240
#define NPSS_RE0         56
#define NPILOT           28   /* pilots outside the PSS/SSS band (RE 56..182) */
#define QPSK             2000
#define KINFO            32
#define NPOL             512
#define SDEPTH           9
#define LLR_SHIFT        2
#define H_SCALE          16          /* keep a fractional part in H_hat */
#define CH_RE            2
#define CH_IM            1

#include "../include/gpcp_ring.h"   /* 描述符环基址跟着 .bss 走（不再硬编码） */
#include "../include/gpcp_desc64.h"  /* 字段契约生成物：setter 是唯一的位定义 */
#define DESC_BASE     GPCP_DESC_BASE
#define Y_BASE           0x80030000ul          /* 4 x 240 complex = 1920 words */
#define SSS_REF_BASE     0x80100000ul          /* 336 x 128 words */
#define OUT_BASE         0x80200000ul          /* rotating result blocks (never revisited:
                                                  * the engine's RAM writes do not invalidate
                                                  * the CPU D$) */
#define OUT_BLOCKS       2048
#define LC_IN_BASE       0x80040000ul          /* a/b/beta staging (3 x 16) */

static uint32_t (*desc)[8] = (uint32_t (*)[8])_ring_base;
static int32_t *yw = (int32_t *)Y_BASE;
static int32_t *sssref = (int32_t *)SSS_REF_BASE;
static int32_t *oblk = (int32_t *)OUT_BASE;
static int32_t *ain = (int32_t *)LC_IN_BASE;
static int32_t *bin = (int32_t *)(LC_IN_BASE + 64);
static int32_t *bet = (int32_t *)(LC_IN_BASE + 128);

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, n_desc, out_idx, done;
static int32_t  lbuf[SDEPTH + 1][NPOL];
static uint8_t  bbuf[SDEPTH + 1][NPOL];

/* --------------------------------------------------------------- sequences */
static uint32_t gold_x1[NPOL + 1600 + 31];
static uint32_t gold_x2[NPOL + 1600 + 31];
/* The PBCH DMRS has its own Gold sequence (38.211 7.4.1.4.1 c_init); the data
 * scrambling keeps c_init = N_ID_cell (38.212 7.1.2).  It lives at a fixed
 * address above the 512 KiB linker RAM: .bss must stay below the result
 * window at 0x8001_0000. */
#define GOLD2_BASE       0x80060000ul
static uint32_t *gold2_x1 = (uint32_t *)GOLD2_BASE;
static uint32_t *gold2_x2 = (uint32_t *)(GOLD2_BASE + 4 * (NPOL + 1600 + 31));

static void gold_fill(uint32_t *x1, uint32_t *x2, uint32_t c_init)
{
    uint32_t n = NPOL + 1600;
    for (uint32_t i = 0; i < n + 31; i++) { x1[i] = 0; x2[i] = 0; }
    x1[0] = 1;
    for (uint32_t i = 0; i < 31; i++) x2[i] = (c_init >> i) & 1u;
    for (uint32_t i = 0; i < n; i++) {
        x1[i + 31] = x1[i + 3] ^ x1[i];
        x2[i + 31] = x2[i + 3] ^ x2[i + 2] ^ x2[i + 1] ^ x2[i];
    }
}

static void gold_init(uint32_t c_init) { gold_fill(gold_x1, gold_x2, c_init); }

static uint32_t gold_bit(uint32_t n) { return gold_x1[1600 + n] ^ gold_x2[1600 + n]; }

static void gold_init2(uint32_t c_init) { gold_fill(gold2_x1, gold2_x2, c_init); }

static uint32_t gold_bit2(uint32_t n) { return gold2_x1[1600 + n] ^ gold2_x2[1600 + n]; }

/* 38.211 7.4.1.4.1: c_init = 2^11 (i_SSB+1)(N_ID_cell/4+1) + 2^6 (i_SSB+1)
 * + (N_ID_cell mod 4); the SSB here is i_SSB = 2. */
#define I_SSB 2
static uint32_t dmrs_c_init(int nid1, int nid2)
{
    uint32_t nid = (uint32_t)(3 * nid1 + nid2);
    return ((uint32_t)(I_SSB + 1) << 11) * (nid / 4u + 1u)
         + ((uint32_t)(I_SSB + 1) << 6) + (nid % 4u);
}

/* PSS/SSS m-sequences (38.211), same as tools/nr_seq.py */
static uint8_t x0seq[2 * NREP], x1seq[2 * NREP];

static void build_mseq(uint8_t *x, int tap)
{
    for (int i = 0; i < 7; i++) x[i] = (uint8_t)(i == 6);
    for (int i = 0; i < 2 * NREP - 7; i++) x[i + 7] = (uint8_t)(x[i + tap] ^ x[i]);
}

static int pss_sign(int nid2, int n)
{
    static uint8_t m[2 * NREP];
    static int init;
    if (!init) { build_mseq(m, 4); init = 1; }
    return m[(n + 43 * nid2) % NREP] ? -1 : 1;
}

static int sss_sign(int nid1, int nid2, int n)
{
    int m0 = 15 * (nid1 / 112) + 5 * nid2;
    int m1 = nid1 % 112;
    int a = x0seq[(n + m0) % NREP] ? -1 : 1;
    int b = x1seq[(n + m1) % NREP] ? -1 : 1;
    return a * b;
}

/* ------------------------------------------------------------- bit utilities */
static uint32_t bitrev(uint32_t v, uint32_t bits)
{
    uint32_t r = 0;
    for (uint32_t b = 0; b < bits; b++) r |= ((v >> b) & 1u) << (bits - 1 - b);
    return r;
}

static int is_frozen(uint32_t i) { return bitrev(i % NPOL, SDEPTH) < (uint32_t)(NPOL - KINFO); }

/* --------------------------------------------------------------- microcode */

static void build_corr_uc(void)
{
    for (uint32_t i = 0; i < CORR_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, pss_corr_prog[i]);
    gpcp_cgra_write(CGRA_BASE, GPCP_SPAD_FLAT_ADDR(PSS_CORR_SPAD_LEAF_R7(GPCP_LAYOUT_NPES - 1u)), 0);   /* ref[127] 幻影 tap */
}

static void build_node_uc(void)
{
    for (uint32_t i = 0; i < NODE_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, polar_node_prog[i]);
}

/* ---------------------------------------------------------------- ring glue */
static void dsc(uint32_t ram, uint32_t spad, uint32_t len, uint32_t page,
                uint32_t out, uint32_t stride, uint32_t row)
{
    /* 32B 描述符 = 64B 描述符的前 4 个字；字段用生成的 setter 组包，
     * 位定义只在 rtl/cgra/cgra_desc64.yml，这里不再手写掩码/移位。 */
    uint32_t b[4] = {0, 0, 0, 0};
    gpcp_d64_set_src(b, (uint32_t)ram);
    gpcp_d64_set_spad(b, spad);
    gpcp_d64_set_page(b, page);
    gpcp_d64_set_dir(b, out);
    gpcp_d64_set_in_len(b, len);
    gpcp_d64_set_stride(b, stride);
    gpcp_d64_set_row(b, row);
    uint32_t *d = desc[n_desc];
    d[0] = b[0]; d[1] = b[1]; d[2] = b[2]; d[3] = b[3];
    d[4] = 0; d[5] = 0; d[6] = 0; d[7] = 0;
    n_desc++;
}

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

static void ring_on(uint32_t head, uint32_t tail)
{
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_BASE, DESC_BASE);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_SIZE, RING_DEPTH);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_HEAD, head);
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_TAIL, tail);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_CTRL, GPCP_CGRA_RING_EN);
}

static void array_run(uint32_t cycles)
{
    gpcp_cgra_array_start(CGRA_BASE, 0, cycles);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY) {
        if (++spins > 4000000u) { errs++; break; }
    }
    runs++;
}

/* one 127-point correlation: y slice + 127 reference words -> (re, im) sums */
static void corr_run(uint32_t y_word_off, uint32_t ref_addr, int64_t *re, int64_t *im)
{
    int32_t *fb = oblk + out_idx * 32;
    out_idx = (out_idx + 1) % OUT_BLOCKS;

    n_desc = 0;
    dsc(Y_BASE + 4u * y_word_off, 0, 2 * NREP, 0, 0, 0, 0);
    dsc(ref_addr, (GPCP_LAYOUT_REGION_REF * GPCP_LAYOUT_WORDS), NREP, 0, 0, GPCP_LAYOUT_WORDS, 8);
    dsc((uint32_t)(uintptr_t)fb, (GPCP_LAYOUT_REGION_LLR * GPCP_LAYOUT_WORDS), GPCP_LAYOUT_WORDS, 0, 1, 0, 0);

    ring_on(0, 2);
    done += 2;
    wait_desc(done);
    array_run(CORR_CYCLES);
    gpcp_cgra_ring_push(CGRA_BASE, 3);
    done += 1;
    wait_desc(done);
    mmio_w(CGRA_BASE + GPCP_CGRA_RING_CTRL, 0);

    int64_t sr = 0, si = 0;
    for (int p = 0; p < NPES; p++) { sr += fb[2 * p]; si += fb[2 * p + 1]; }
    *re = sr;            /* 127 x 32767 x |y| does not fit in int32 */
    *im = si;
}

/* --------------------------------------------------------------- polar */
static uint32_t polar_enc_words[NPOL];

static void polar_encode(uint8_t *ufull)
{
    uint8_t *x = (uint8_t *)polar_enc_words;         /* one byte per bit, temp */
    for (int i = 0; i < NPOL; i++) x[i] = ufull[i];
    for (int step = 1; step < NPOL; step *= 2)
        for (int i = 0; i < NPOL; i += 2 * step)
            for (int k = 0; k < step; k++)
                x[i + k] ^= x[i + k + step];
}

static void node_run(uint32_t d, uint32_t o, uint32_t half, const uint8_t *bp, uint32_t cnt)
{
    for (uint32_t chunk = 0; chunk < cnt; chunk += NPES) {
        uint32_t n = cnt - chunk < NPES ? cnt - chunk : NPES;
        for (uint32_t k = 0; k < n; k++) {
            ain[k] = lbuf[d][o + chunk + k];
            bin[k] = lbuf[d][o + half + chunk + k];
            bet[k] = bp ? bp[chunk + k] : 0;
        }
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        (void)ain[n - 1];

        int32_t *fb = oblk + out_idx * 32;
        int32_t *gb = fb + 16;
        out_idx = (out_idx + 1) % OUT_BLOCKS;

        n_desc = 0;
        dsc((uint32_t)(uintptr_t)ain, 0, n, 0, 0, 0, 0);
        dsc((uint32_t)(uintptr_t)bin, 32, n, 0, 0, 0, 0);
        dsc((uint32_t)(uintptr_t)bet, 64, n, 0, 0, 0, 0);
        dsc((uint32_t)(uintptr_t)fb, (GPCP_LAYOUT_REGION_OUT * GPCP_LAYOUT_WORDS), n, 0, 1, 0, 0);
        dsc((uint32_t)(uintptr_t)gb, ((GPCP_LAYOUT_REGION_OUT + 1) * GPCP_LAYOUT_WORDS), n, 0, 1, 0, 0);

        ring_on(0, 3);
        done += 3;
        wait_desc(done);
        array_run(NODE_CYCLES);
        gpcp_cgra_ring_push(CGRA_BASE, 5);
        done += 2;
        wait_desc(done);
        mmio_w(CGRA_BASE + GPCP_CGRA_RING_CTRL, 0);

        for (uint32_t k = 0; k < n; k++) {
            lbuf[d + 1][o + chunk + k] = fb[k];
            lbuf[d + 1][o + half + chunk + k] = gb[k];
        }
    }
}

static void sc(uint32_t d, uint32_t o, uint32_t size)
{
    if (size == 1) {
        bbuf[d][o] = (uint8_t)(is_frozen(o) ? 0 : (lbuf[d][o] < 0));
        return;
    }
    uint32_t half = size >> 1;
    node_run(d, o, half, 0, half);
    sc(d + 1, o, half);
    node_run(d, o, half, &bbuf[d + 1][o], half);
    sc(d + 1, o + half, half);
    for (uint32_t i = 0; i < half; i++) {
        bbuf[d][o + i] = bbuf[d + 1][o + i] ^ bbuf[d + 1][o + half + i];
        bbuf[d][o + half + i] = bbuf[d + 1][o + half + i];
    }
}

/* --------------------------------------------------------------- grid build */
static uint32_t lcg, lcg_noise;

static uint32_t lcg_step(void)
{
    lcg = lcg * 1103515245u + 12345u;
    return lcg;
}

static int32_t noise(void)
{
    lcg_noise = lcg_noise * 1103515245u + 12345u;
    return (int32_t)((lcg_noise >> 16) & 0x3FFu) - 512;
}

static int pilot_re(int p) { return (p < 14) ? p * 4 : 184 + (p - 14) * 4; }
static int is_mid(int r) { return r >= NPSS_RE0 && r < NPSS_RE0 + NREP; }

/* data RE scan order, identical to tools/ssb_plan.py */
static void data_re_list(uint32_t s, int *out, int *cnt)
{
    int n = 0;
    for (int r = 0; r < NRE; r++) {
        if ((r % 4) == 0 && !is_mid(r)) continue;   /* DMRS (pilots outside PSS/SSS) */
        if ((s == 1 || s == 3) && is_mid(r)) continue; /* PSS / SSS */
        out[n++] = r;
    }
    *cnt = n;
}

static void put(int s, int r, int32_t re, int32_t im)
{
    yw[(s * NRE + r) * 2 + 0] = re;
    yw[(s * NRE + r) * 2 + 1] = im;
}

/* ------------------------------------------------------------------ epilogue */
static void finish(void)
{
    RESULT_BASE[87] = 0x600D;
    *(volatile uint32_t *)EXIT_ADDR = 0;
    while (1) { }
}

int main(void)
{
    static uint8_t payload[KINFO], ufull[NPOL];

    build_mseq(x0seq, 4);
    build_mseq(x1seq, 1);
    build_corr_uc();

    /* ---- MIB payload (24 bits + 8 zero pad), polar encode, scramble ---- */
    lcg = 0x1234u;
    lcg_noise = 0x50B0A5u;
    for (int i = 0; i < 24; i++) payload[i] = (uint8_t)((lcg_step() >> 31) & 1u);
    for (int i = 24; i < KINFO; i++) payload[i] = 0;
    int k = 0;
    for (int i = 0; i < NPOL; i++) {
        ufull[i] = 0;
        if (!is_frozen((uint32_t)i) && k < KINFO) ufull[i] = payload[k++];
    }
    polar_encode(ufull);

    /* ---- transmit: PSS/SSS/DMRS/QPSK, channel (2,1), noise +-512 ---- */
    gold_init(3u * 200u + 1u);                       /* data scrambling c_init */
    gold_init2(dmrs_c_init(200, 1));                 /* PBCH DMRS c_init       */
    for (int s = 0; s < NSYM; s++)
        for (int r = 0; r < NRE; r++) put(s, r, 0, 0);

    for (int kk = 0; kk < NREP; kk++) {
        int sgn = pss_sign(1, kk);
        put(1, NPSS_RE0 + kk, sgn * QPSK, 0);
        sgn = sss_sign(200, 1, kk);
        put(3, NPSS_RE0 + kk, sgn * QPSK, 0);
    }
    {
        int n = 0;
        for (int s = 0; s < NSYM; s++)
            for (int p = 0; p < NPILOT; p++) {
                int32_t dr = (int32_t)(1 - 2 * (int)gold_bit2(2u * (uint32_t)n)) * QPSK;
                int32_t di = (int32_t)(1 - 2 * (int)gold_bit2(2u * (uint32_t)n + 1u)) * QPSK;
                put(s, pilot_re(p), dr, di);
                n++;
            }
    }
    {
        uint8_t *xbits = (uint8_t *)polar_enc_words;
        int n = 0;
        for (int s = 0; s < NSYM; s++) {
            int res[NRE], cnt = 0;
            data_re_list((uint32_t)s, res, &cnt);
            for (int j = 0; j < cnt; j++) {
                if (n + 1 >= NPOL) break;
                int b0 = xbits[n] ^ (uint8_t)gold_bit((uint32_t)n);
                int b1 = xbits[n + 1] ^ (uint8_t)gold_bit((uint32_t)n + 1);
                put(s, res[j], (1 - 2 * b0) * QPSK, (1 - 2 * b1) * QPSK);
                n += 2;
            }
        }
    }

    /* channel + noise (identical order to tools/ssb_plan.py transmit) */
    for (int s = 0; s < NSYM; s++)
        for (int r = 0; r < NRE; r++) {
            int32_t re = yw[(s * NRE + r) * 2 + 0], im = yw[(s * NRE + r) * 2 + 1];
            int64_t cr = (int64_t)re * CH_RE - (int64_t)im * CH_IM;
            int64_t ci = (int64_t)re * CH_IM + (int64_t)im * CH_RE;
            put(s, r, (int32_t)cr + noise(), (int32_t)ci + noise());
        }

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    (void)yw[0];

    uint32_t t0 = mtime();
    done = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE);

    /* ---- 1) PSS: 3 candidates ---- */
    /* PSS reference blocks: one 128-word block per candidate at 0x801C0000 */
    {
        uint32_t *pref = (uint32_t *)0x801C0000ul;
        for (int c = 0; c < 3; c++)
            for (int t = 0; t < NREP; t++)
                pref[c * 128 + t] = (uint32_t)(pss_sign(c, t) * 32767);
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        (void)pref[3 * 128 - 1];

        int64_t best = -1;
        uint32_t nid2 = 0;
        for (int c = 0; c < 3; c++) {
            int64_t re, im;
            corr_run((NRE + NPSS_RE0) * 2, 0x801C0000ul + 4u * (uint32_t)(c * 128), &re, &im);
            RESULT_BASE[4 + 2 * c] = (uint32_t)(re >> 16);
            RESULT_BASE[5 + 2 * c] = (uint32_t)(im >> 16);
            int64_t mag = (re < 0 ? -re : re) + (im < 0 ? -im : im);
            if (mag > best) { best = mag; nid2 = (uint32_t)c; }
        }

        /* ---- 2) SSS: 336 hypotheses (references generated once) ---- */
        for (uint32_t nid1 = 0; nid1 < 336; nid1++)
            for (int t = 0; t < NREP; t++)
                sssref[nid1 * 128 + t] = (uint32_t)(sss_sign((int)nid1, 1, t) * 32767);
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        (void)sssref[336 * 128 - 1];

        /* debug: DMRS bits and y of two symbol-1 pilots */
        int64_t sbest = -1;
        uint32_t nid1 = 0;
        int64_t fre = 0, fim = 0;
        for (uint32_t h = 0; h < 336; h++) {
            int64_t re, im;
            corr_run((3 * NRE + NPSS_RE0) * 2,
                     SSS_REF_BASE + 4u * h * 128u, &re, &im);
            int64_t mag = (re < 0 ? -re : re) + (im < 0 ? -im : im);
            if (mag > sbest) { sbest = mag; nid1 = h; fre = re; fim = im; }
        }
        RESULT_BASE[10] = (uint32_t)(fre >> 16);
        RESULT_BASE[11] = (uint32_t)(fim >> 16);

        /* ---- 3) DMRS channel estimate per symbol ---- */
        int32_t hre[NSYM], him[NSYM];
        {
            int n = 0;
            int64_t den = (int64_t)NPILOT * 2 * QPSK * QPSK / H_SCALE;
            for (int s = 0; s < NSYM; s++) {
                int64_t accr = 0, acci = 0;
                for (int p = 0; p < NPILOT; p++) {
                    int32_t dr = (int32_t)(1 - 2 * (int)gold_bit2(2u * (uint32_t)n)) * QPSK;
                    int32_t di = (int32_t)(1 - 2 * (int)gold_bit2(2u * (uint32_t)n + 1u)) * QPSK;
                    int32_t yr = yw[(s * NRE + pilot_re(p)) * 2 + 0];
                    int32_t yi = yw[(s * NRE + pilot_re(p)) * 2 + 1];
                    accr += (int64_t)yr * dr + (int64_t)yi * di;      /* y*d* */
                    acci += (int64_t)yi * dr - (int64_t)yr * di;
                    n++;
                }
                hre[s] = (int32_t)(accr / den);
                him[s] = (int32_t)(acci / den);
                RESULT_BASE[12 + 2 * s] = (uint32_t)hre[s];
                RESULT_BASE[13 + 2 * s] = (uint32_t)him[s];
            }
        }

        /* ---- 4) demod + descramble -> LLRs ---- */
        for (int i = 0; i < NPOL; i++) lbuf[0][i] = 0;
        {
            int n = 0;
            for (int s = 0; s < NSYM; s++) {
                int res[NRE], cnt = 0;
                data_re_list((uint32_t)s, res, &cnt);
                for (int j = 0; j < cnt; j++) {
                    if (n + 1 >= NPOL) break;
                    int32_t yr = yw[(s * NRE + res[j]) * 2 + 0];
                    int32_t yi = yw[(s * NRE + res[j]) * 2 + 1];
                    int64_t eqr = (int64_t)yr * hre[s] + (int64_t)yi * him[s];
                    int64_t eqi = (int64_t)yi * hre[s] - (int64_t)yr * him[s];
                    int32_t v0 = (int32_t)(eqr >> LLR_SHIFT);
                    int32_t v1 = (int32_t)(eqi >> LLR_SHIFT);
                    if (v0 > 2000000) v0 = 2000000;
                    if (v0 < -2000000) v0 = -2000000;
                    if (v1 > 2000000) v1 = 2000000;
                    if (v1 < -2000000) v1 = -2000000;
                    lbuf[0][n] = gold_bit((uint32_t)n) ? -v0 : v0;
                    lbuf[0][n + 1] = gold_bit((uint32_t)n + 1) ? -v1 : v1;
                    n += 2;
                }
            }
        }

        /* ---- 5) polar SC decode ---- */
        build_node_uc();
        sc(0, 0, NPOL);

        /* ---- 6) compare ---- */
        k = 0;
        int mism = 0;
        for (int i = 0; i < NPOL; i++)
            if (!is_frozen((uint32_t)i) && k < KINFO) {
                uint8_t b = bbuf[SDEPTH][i];
                RESULT_BASE[20 + k] = b;
                if (b != payload[k]) mism++;
                k++;
            }
        for (int i = 0; i < KINFO; i++) RESULT_BASE[52 + i] = payload[i];

        RESULT_BASE[0] = nid2;
        RESULT_BASE[1] = nid1;
        RESULT_BASE[2] = 1;
        RESULT_BASE[3] = 200;
        RESULT_BASE[84] = errs;
        RESULT_BASE[88] = (uint32_t)mism;
        RESULT_BASE[85] = mtime() - t0;
        RESULT_BASE[86] = runs;
    }
    finish();
    return (int)errs;
}
