/* -----------------------------------------------------------------------------
 * L1 link end-to-end on the SoC -- DCI-driven PDSCH (38.211/38.212).
 *
 *   TD0 (1 OFDM symbol, CP + 4096 IFFT, at 0x8040_0000)  = PDCCH (DCI)
 *   TD1 (2 OFDM symbols, at 0x8040_0000 + PDCCH_SYM_BYTES) = PDSCH (TB)
 *
 *   step 1  PDCCH decode (same engine as sw/soc/pdcch_bm.c):
 *       FFT -> 216 RE LLR -> descramble (N_ID) -> inverse rate match (E=432)
 *       -> polar SC on CGRA -> RNTI CRC24C blind detect -> 32-bit DCI
 *   step 2  parse rv from the DCI (bits [5:7], MSB-first) -> k0 = rv*USABLE/4
 *   step 3  PDSCH decode (same engine as sw/soc/pdsch_full_bm.c) at that k0:
 *       FFT x2 -> Gold-DMRS LS -> equalise -> QPSK LLR -> de-interleave
 *       -> descramble (n_RNTI<<15) -> Chase-combine at k0 -> rate de-match
 *       -> 8 min-sum LDPC iterations on CGRA -> CRC24A -> 80-bit TB
 *
 * The whole point of this benchmark is that the rv used by the PDSCH circular
 * buffer is *read out of the decoded DCI*, not hard-coded -- the link is driven
 * by what the control channel actually said.  (At rv=2 the k0 offset is 492 and
 * a hard-coded k0=0 would fail, so this also exercises the coupling.)
 *
 * Acceptance: tools/check_link_e2e.py (golden from pdsch_full_plan + nr_pdcch).
 *
 * Result area (0x8001_0000, 32-bit words):
 *   [0] 0xCAFEBABE  [1] errors  [2] cycles  [3] array runs
 *   [4] PDCCH matched RNTI (0 if none)  [5] PDCCH CRC ok  [6] 0x600D  [7] rv
 *   [8..39]   32 DCI bits
 *   [40] mcs [41] ndi [42] harq_pid [43] rb_start [44] rb_len [45] tpc
 *   [46] PDSCH CRC24A ok
 *   [48..1087]  1040 PDSCH LDPC hard decisions (0/1, block-major)
 *   [1088] PDCCH first raw LLR  [1089] PDCCH first de-matched polar LLR
 *   [1090] PDSCH first raw LLR  [1091] PDSCH first descrambled LLR
 *   [1092] PDSCH first de-matched LLR>>8  [1093] DMRS c_init sym0
 *   [1094] DMRS c_init sym1  [1095] k0 used by the PDSCH
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "polar_node_prog.h"      /* 编译器生成的 polar SC 节点微码 */
#include "../include/gpcp_cgra_rt.h"
#include "../include/ldpc_bg2.h"
#include "pdcch_luts.h"
#include "pdsch_cn_prog.h"
#include "pdsch_bn_prog.h"

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
/* Result region must sit ABOVE .bss (ends ~0x8001422C in this program, which
 * carries the full LDPC working sets) and below the top-of-RAM stack
 * (0x80080000).  0x80010000 collides with .bss globals and corrupts the
 * DCI bytes written early; 0x80070000 leaves a 64 KiB stack margin. */
#define RESULT_BASE   ((uint32_t *)0x80070000ul)

/* ---- ssb_fft.inc prerequisites (shared by both chains) ---- */
#define NPES          16
#define RING_DEPTH    1024
#define DESC_BASE     0x80020000ul
#define TD_RAM        0x80400000ul
#define NFFT          4096
#define CP_LEN        288
#define BIN0          (NFFT / 2 + 1)
#define SYM_WORDS     (2 * (CP_LEN + NFFT))          /* int32 words per symbol */
#define PDCCH_SYM_BYTES (SYM_WORDS * 4u)

/* ---- Gold scrambling (one shared buffer, re-init between uses) ---- */
#define GOLD_NC       1600
#define GOLD_LEN      (720 + GOLD_NC + 31)            /* >= max(PDCCH,PDSCH E) */
#define GOLD_BASE     0x80380000ul

/* ---- PDCCH (control) chain constants ---- */
#define PDCCH_E       432
#define PDCCH_NRE     216
#define N_ID          200u
#define NPOL          512
#define KINFO         56
#define SDEPTH        9
#define NODE_CYCLES   POLAR_NODE_PROG_CYCLES
#define LC_IN_BASE    0x80310000ul
#define LB_BASE       0x80320000ul
#define NODE_OUT_BASE 0x80340000ul
#define NODE_OUT_BLOCKS 2048
#define NODE_OUT_STRIDE 32
#ifdef SIB1
/* SIB1: the SI-RNTI is fixed (0xFFFF) and known to the UE (Type0-PDCCH
 * common search space), so there is no blind search -- one candidate. */
#define NCAND         1
static const uint32_t candidates[NCAND] = { 0xFFFFu };
#else
#define NCAND         5
static const uint32_t candidates[NCAND] = {
    0x1111u, 0x5678u, 0x1234u, 0x9ABCu, 0xDEF0u
};
#endif

/* ---- PDSCH (data) chain constants ---- */
#define PDSCH_E       720
#define PDSCH_NRE     240
#define NSYM          2
#define NPILOT        60
#define QPSK          2000
#define H_SCALE       16
#define LLR_SHIFT     8
#ifdef SIB1
#define N_RNTI        0xFFFFu
#else
#define N_RNTI        0x1234u
#endif
#define USABLE        984
#define FILLER0       64
#define FILLER_N      16
#define PUNCT         40
#define NCODED        1040
#define A_TB          80
#define CRC_LEN       24
#define DMRS_NID      1
#define DMRS_NSCID    0
#define DMRS_NSLOT    0
#define DMRS_L0       2
#define DMRS_NSYMB    14
#define DMRS_NCP      0
#define QM            2

/* ---- LDPC engine (same addresses as sw/soc/pdsch_iter_bm.c) ---- */
#define ZC            20
#define MAXD          10
#define SENTINEL      32767
#define SENTINEL_BANK 30
#define ITERS         8
#define MSG_BASE      0x80030000ul
#define RES_BASE      0x80039000ul
#define LLR_BASE      0x8003C000ul
#define GATH_BASE     0x8003E000ul
#define BOUT_BASE     0x80059000ul
#define GSPAD         (26 * ZC)
#define YW_BASE       0x80390000ul
#define LLRE_BASE     0x80391000ul
#define DEM_BASE      0x80392000ul
#define BUF_BASE      0x80393000ul
#define DEINT_BASE    0x80394000ul

static uint32_t (*desc)[8] = (uint32_t (*)[8])DESC_BASE;
static uint32_t *gold_x1 = (uint32_t *)GOLD_BASE;
static uint32_t *gold_x2 = (uint32_t *)(GOLD_BASE + 4 * GOLD_LEN);
static int32_t *ain = (int32_t *)LC_IN_BASE;
static int32_t *bin = (int32_t *)(LC_IN_BASE + 64);
static int32_t *bet = (int32_t *)(LC_IN_BASE + 128);
static int32_t (*lbuf)[NPOL] = (int32_t (*)[NPOL])LB_BASE;
static uint8_t (*bbuf)[NPOL] =
    (uint8_t (*)[NPOL])(LB_BASE + 4 * (SDEPTH + 1) * NPOL);
static int32_t *noblk = (int32_t *)NODE_OUT_BASE;
static int32_t llr_pd[PDCCH_E];
static int32_t pre[POLAR_N];

static int32_t *yw = (int32_t *)YW_BASE;
static int32_t *llr_e = (int32_t *)LLRE_BASE;
static int32_t *dem = (int32_t *)DEM_BASE;
static int32_t *rbuf = (int32_t *)BUF_BASE;
static int32_t *llr_i = (int32_t *)DEINT_BASE;
static int32_t dmrs_re[NSYM][NPILOT], dmrs_im[NSYM][NPILOT];

static int32_t (*msg)[MAXD][ZC] = (int32_t (*)[MAXD][ZC])MSG_BASE;
static int32_t (*res)[3][ZC] = (int32_t (*)[3][ZC])RES_BASE;
static int32_t (*llr)[ZC] = (int32_t (*)[ZC])LLR_BASE;
static int32_t *g_all = (int32_t *)GATH_BASE;
static int32_t *b_all = (int32_t *)BOUT_BASE;

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t errs, runs, dmas;
static uint32_t n_desc, done, nblk;

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; break; }
    }
}

#include "ssb_fft.inc"          /* fft4096() + rev4() */

/* ------------------------------------------------------------ 38.211 Gold */
static void gold_init(uint32_t c_init)
{
    uint32_t n = 720 + GOLD_NC;
    for (uint32_t i = 0; i < n + 31u; i++) { gold_x1[i] = 0; gold_x2[i] = 0; }
    gold_x1[0] = 1;
    for (uint32_t i = 0; i < 31u; i++) gold_x2[i] = (c_init >> i) & 1u;
    for (uint32_t i = 0; i < n; i++) {
        gold_x1[i + 31] = gold_x1[i + 3] ^ gold_x1[i];
        gold_x2[i + 31] = gold_x2[i + 3] ^ gold_x2[i + 2] ^ gold_x2[i + 1] ^ gold_x2[i];
    }
}

static uint32_t gold_bit(uint32_t n)
{
    return gold_x1[GOLD_NC + n] ^ gold_x2[GOLD_NC + n];
}

/* ------------------------------------------------- 38.211 7.4.1.1 PDSCH DMRS */
static uint32_t dmrs_cinit(int s)
{
    uint64_t l = (uint64_t)(DMRS_L0 + s);
    uint64_t v = ((uint64_t)1 << 17)
                 * (uint64_t)(DMRS_NSYMB * DMRS_NSLOT + l + 1)
                 * (uint64_t)(2 * DMRS_NID + DMRS_NSCID + 1)
                 + (uint64_t)(2 * DMRS_NID + DMRS_NCP);
    return (uint32_t)(v & 0x7FFFFFFFu);
}

static void build_dmrs(void)
{
    for (int s = 0; s < NSYM; s++) {
        gold_init(dmrs_cinit(s));
        for (int p = 0; p < NPILOT; p++) {
            uint32_t b0 = gold_bit((uint32_t)(2 * p));
            uint32_t b1 = gold_bit((uint32_t)(2 * p + 1));
            dmrs_re[s][p] = (int32_t)(1 - 2 * (int)b0) * QPSK;
            dmrs_im[s][p] = (int32_t)(1 - 2 * (int)b1) * QPSK;
        }
    }
}

/* ------------------------------------------------------------ CRC24A (38.212 7.1) */
static void crc24a(const uint8_t *bits, int n, uint8_t *out)
{
    const uint32_t poly = 0x864CFBu;
    uint32_t reg = 0;
    for (int i = 0; i < n; i++) {
        reg ^= (uint32_t)(bits[i] & 1u) << 23;
        for (int j = 0; j < 24; j++) {
            if (reg & 0x800000u) reg = ((reg << 1) ^ poly) & 0xFFFFFFu;
            else                 reg = (reg << 1) & 0xFFFFFFu;
        }
    }
    for (int i = 0; i < 24; i++) out[i] = (uint8_t)((reg >> (23 - i)) & 1u);
}

/* ---------------------------------------------- CRC24C (38.212 7.2, PDCCH) */
static void crc24c(const uint8_t *bits, int n, uint8_t *out)
{
    const uint32_t poly = 0xB2B117u;
    uint32_t reg = 0;
    for (int i = 0; i < n; i++) {
        reg ^= (uint32_t)(bits[i] & 1u) << 23;
        for (int j = 0; j < 24; j++) {
            if (reg & 0x800000u) reg = ((reg << 1) ^ poly) & 0xFFFFFFu;
            else                 reg = (reg << 1) & 0xFFFFFFu;
        }
    }
    for (int i = 0; i < 24; i++) out[i] = (uint8_t)((reg >> (23 - i)) & 1u);
}

/* ============================================================ POLAR (PDCCH) */
static uint32_t dbg_wait, dbg_array_spin, dbg_nblk;

/* scratch-pad index: bank * 32 words + word (matches pdcch_bm.c) */

static void build_node_uc(void)
{
    for (uint32_t i = 0; i < NODE_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, polar_node_prog[i]);
}

static void array_run(uint32_t cycles)
{
    gpcp_cgra_array_start(CGRA_BASE, 0, cycles);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY) {
        if (++spins > 4000000u) { errs++; dbg_array_spin++; break; }
    }
    runs++;
}

static void dsc(uint32_t ram, uint32_t spad, uint32_t len, uint32_t page,
                uint32_t out, uint32_t stride, uint32_t row)
{
    uint32_t *d = desc[n_desc];
    d[0] = ram;
    d[1] = (spad & 0xFFFFu) | (page ? GPCP_DMA_DESC_PAGE1 : 0u) |
           (out ? GPCP_DMA_DESC_OUT : 0u);
    d[2] = len & 0xFFFFu;
    d[3] = (stride & 0xFFFFu) | ((row & 0xFFFFu) << 16);
    d[4] = 0; d[5] = 0; d[6] = 0; d[7] = 0;
    n_desc++;
}

static void node_run(uint32_t d, uint32_t o, uint32_t half,
                     const uint8_t *bp, uint32_t cnt)
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

        int32_t *fb = noblk + (nblk % NODE_OUT_BLOCKS) * NODE_OUT_STRIDE;
        int32_t *gb = fb + 16;
        nblk++;
        if (nblk > NODE_OUT_BLOCKS) { errs++; dbg_nblk++; nblk = 0; }

        n_desc = 0;
        dsc((uint32_t)(uintptr_t)ain, 0, n, 0, 0, 0, 0);
        dsc((uint32_t)(uintptr_t)bin, 32, n, 0, 0, 0, 0);
        dsc((uint32_t)(uintptr_t)bet, 64, n, 0, 0, 0, 0);
        uint32_t n_in = n_desc;

        done = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE);
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_BASE, DESC_BASE);
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_SIZE, RING_DEPTH);
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_HEAD, 0);
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_TAIL, n_in);
        __asm__ __volatile__("fence iorw, iorw" ::: "memory");
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_CTRL, GPCP_CGRA_RING_EN);
        done += n_in;
        wait_desc(done);
        array_run(NODE_CYCLES);
        dsc((uint32_t)(uintptr_t)fb, 16 * 32, n, 0, 1, 0, 0);
        dsc((uint32_t)(uintptr_t)gb, 17 * 32, n, 0, 1, 0, 0);
        gpcp_cgra_ring_push(CGRA_BASE, n_in + 2u);
        done += 2;
        wait_desc(done);
        mmio_w(CGRA_BASE + GPCP_CGRA_RING_CTRL, 0);
        for (uint32_t k = 0; k < n; k++) {
            lbuf[d + 1][o + chunk + k] = fb[k];
            lbuf[d + 1][o + half + chunk + k] = gb[k];
        }
    }
}

static uint32_t bitrev(uint32_t v, uint32_t bits)
{
    uint32_t r = 0;
    for (uint32_t b = 0; b < bits; b++)
        r |= ((v >> b) & 1u) << (bits - 1 - b);
    return r;
}

static int is_frozen(uint32_t i)
{
    return bitrev(i % NPOL, SDEPTH) < (uint32_t)(NPOL - KINFO);
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

/* ============================================================ LDPC (PDSCH) */
static uint8_t blk_n[BG2_COLS];
static uint8_t blk_r[BG2_COLS][24], blk_k[BG2_COLS][24], blk_sh[BG2_COLS][24];
static uint8_t edge_b[BG2_ROWS][MAXD], edge_e[BG2_ROWS][MAXD];

static void build_tables(void)
{
    for (uint32_t b = 0; b < BG2_COLS; b++) {
        uint8_t n = 0;
        for (uint32_t r = 0; r < BG2_ROWS; r++)
            for (uint32_t k = 0; k < bg2_deg[r]; k++)
                if (bg2_col[r][k] == b) {
                    blk_r[b][n] = (uint8_t)r;
                    blk_k[b][n] = (uint8_t)k;
                    blk_sh[b][n] = (uint8_t)(bg2_sh[r][k] % ZC);
                    edge_b[r][k] = (uint8_t)b;
                    edge_e[r][k] = n;
                    n++;
                }
        blk_n[b] = n;
    }
}

static void cn_row_in(uint32_t r, uint32_t page)
{
    if (rt_dma_begin(CGRA_BASE, (uintptr_t)&msg[r][0][0], 0, MAXD * ZC, page, 0, 32, ZC))
        errs++;
    dmas++;
}

static void cn_row_out(uint32_t r, uint32_t page)
{
    if (rt_dma_begin(CGRA_BASE, (uintptr_t)&res[r][0][0], 10 * 32, 3 * ZC, page, 1, 32, ZC))
        errs++;
    dmas++;
}

static void reconstruct_gather(void)
{
    for (uint32_t r = 0; r < BG2_ROWS; r++) {
        uint32_t d = bg2_deg[r];
        const int32_t *m1p = res[r][0];
        const int32_t *m2p = res[r][1];
        const int32_t *sgp = res[r][2];
        for (uint32_t k = 0; k < d; k++) {
            uint32_t b = edge_b[r][k], e = edge_e[r][k];
            uint32_t sh = bg2_sh[r][k] % ZC;
            const int32_t *src = msg[r][k];
            int32_t *dst = (int32_t *)(g_all + b * GSPAD) + e * ZC;
            for (uint32_t j = 0; j < ZC - sh; j++) {
                int32_t a = src[j];
                int32_t ab = a < 0 ? -a : a;
                int32_t mag = (ab == m1p[j]) ? m2p[j] : m1p[j];
                dst[sh + j] = ((a < 0) ^ (sgp[j] != 0)) ? -mag : mag;
            }
            for (uint32_t j = 0; j < sh; j++) {
                uint32_t i = ZC - sh + j;
                int32_t a = src[i];
                int32_t ab = a < 0 ? -a : a;
                int32_t mag = (ab == m1p[i]) ? m2p[i] : m1p[i];
                dst[j] = ((a < 0) ^ (sgp[i] != 0)) ? -mag : mag;
            }
        }
    }
}

static void bn_dma_in(uint32_t b)
{
    int32_t *gath = g_all + b * GSPAD;
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    if (rt_dma_begin(CGRA_BASE, (uintptr_t)gath, 0, 25 * ZC, 0, 0, 32, ZC))
        errs++;
    dmas++;
}

static void bn_dma_out(uint32_t b, int want_hard)
{
    uint32_t n = blk_n[b];
    int32_t *bout = b_all + b * GSPAD;
    if (n) {
        if (rt_dma_begin(CGRA_BASE, (uintptr_t)bout, 0, n * ZC, 0, 1, 32, ZC))
            errs++;
        dmas++;
    }
    if (want_hard) {
        if (rt_dma_begin(CGRA_BASE, (uintptr_t)(bout + n * ZC),
                         GPCP_SPAD_IDX(25, 0), ZC, 0, 1, 0, 0))
            errs++;
        dmas++;
    }
}

static void bn_scatter(uint32_t b, int want_hard, int32_t *hard)
{
    uint32_t n = blk_n[b];
    int32_t *bout = b_all + b * GSPAD;
    for (uint32_t e = 0; e < n; e++) {
        uint32_t r = blk_r[b][e], k = blk_k[b][e], sh = blk_sh[b][e];
        const int32_t *src = bout + e * ZC;
        int32_t *dst = msg[r][k];
        for (uint32_t i = 0; i < ZC - sh; i++) dst[i] = src[sh + i];
        for (uint32_t i = 0; i < sh; i++)      dst[ZC - sh + i] = src[i];
    }
    if (want_hard) {
        const int32_t *hb = bout + n * ZC;
        for (uint32_t i = 0; i < ZC; i++) hard[i] = hb[i];
    }
}

/* ============================================================ main */
int main(void)
{
    uint32_t *out = RESULT_BASE;
    static int32_t hard[BG2_COLS][ZC] __attribute__((aligned(8)));
    uint32_t t0 = mtime();

    /* ----------------------------------------------------- 1) PDCCH decode */
    int32_t *pdcch_td = (int32_t *)TD_RAM;
    int32_t *pspec = pdcch_td + 2 * CP_LEN;
    fft4096(pspec);
    for (int r = 0; r < PDCCH_NRE; r++) {
        uint32_t idx = rev4((uint32_t)(BIN0 + r));
        llr_pd[2 * r + 0] = pspec[2 * idx] << 4;
        llr_pd[2 * r + 1] = pspec[2 * idx + 1] << 4;
    }
    gold_init(N_ID);
    for (int k = 0; k < PDCCH_E; k++)
        if (gold_bit((uint32_t)k)) llr_pd[k] = -llr_pd[k];

    for (int i = 0; i < POLAR_N; i++) pre[i] = 0;
    for (int m = 0; m < RM_LEN; m++) pre[m] += llr_pd[tri_perm[m]];
    for (int i = 0; i < POLAR_N; i++) lbuf[0][i] = pre[sb_perm[i]];

    build_node_uc();
    sc(0, 0, NPOL);
    uint8_t info[KINFO];
    {
        int k = 0;
        for (uint32_t i = 0; i < NPOL && k < KINFO; i++)
            if (!is_frozen(i)) info[k++] = bbuf[SDEPTH][i];
        if (k != KINFO) errs++;
    }

    uint8_t expect[24];
    crc24c(info, 32, expect);
    uint32_t matched = 0;
    int pdcch_crc_ok = 0;
    for (int c = 0; c < NCAND; c++) {
        uint8_t got[24];
        for (int j = 0; j < 24; j++) got[j] = info[32 + j];
        for (int j = 0; j < 16; j++) got[j] ^= (uint8_t)((candidates[c] >> j) & 1u);
        int ok = 1;
        for (int j = 0; j < 24; j++)
            if (got[j] != expect[j]) { ok = 0; break; }
        if (ok && !matched) {
            matched = candidates[c];
            pdcch_crc_ok = 1;
            for (int i = 0; i < 32; i++) out[8 + i] = info[i];
        }
    }

    /* ----------------------------------------------------- 2) parse DCI rv */
    uint32_t mcs   = ((uint32_t)info[0] << 4) | ((uint32_t)info[1] << 3) |
                     ((uint32_t)info[2] << 2) | ((uint32_t)info[3] << 1) | (uint32_t)info[4];
    uint32_t rv    = ((uint32_t)info[5] << 1) | (uint32_t)info[6];
    uint32_t ndi   = (uint32_t)info[7];
    uint32_t harq_pid = ((uint32_t)info[8] << 3) | ((uint32_t)info[9] << 2) |
                        ((uint32_t)info[10] << 1) | (uint32_t)info[11];
    uint32_t rb_start = ((uint32_t)info[12] << 3) | ((uint32_t)info[13] << 2) |
                        ((uint32_t)info[14] << 1) | (uint32_t)info[15];
    uint32_t rb_len   = ((uint32_t)info[16] << 3) | ((uint32_t)info[17] << 2) |
                        ((uint32_t)info[18] << 1) | (uint32_t)info[19];
    uint32_t tpc   = ((uint32_t)info[20] << 1) | (uint32_t)info[21];

    /* ----------------------------------------------------- 3) PDSCH at k0(rv) */
    uint32_t k0 = rv * (USABLE / 4u);
    int32_t *pdsch_td = (int32_t *)(TD_RAM + PDCCH_SYM_BYTES);

    build_tables();
    build_dmrs();

    for (int i = 0; i < USABLE; i++) rbuf[i] = 0;

    for (int s = 0; s < NSYM; s++) {
        int32_t *sp = pdsch_td + (size_t)s * SYM_WORDS + 2 * CP_LEN;
        fft4096(sp);
        for (int r = 0; r < PDSCH_NRE; r++) {
            uint32_t idx = rev4((uint32_t)(BIN0 + r));
            yw[(s * PDSCH_NRE + r) * 2 + 0] = sp[2 * idx] << 4;
            yw[(s * PDSCH_NRE + r) * 2 + 1] = sp[2 * idx + 1] << 4;
        }
    }
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");

    int64_t den = (int64_t)NPILOT * 2 * QPSK * QPSK / H_SCALE;
    int32_t hre[NSYM], him[NSYM];
    for (int s = 0; s < NSYM; s++) {
        int64_t accr = 0, acci = 0;
        int p = 0;
        for (int r = 0; r < PDSCH_NRE; r += 4) {
            int32_t yr = yw[(s * PDSCH_NRE + r) * 2 + 0];
            int32_t yi = yw[(s * PDSCH_NRE + r) * 2 + 1];
            int32_t dr = dmrs_re[s][p], di = dmrs_im[s][p];
            accr += (int64_t)yr * dr + (int64_t)yi * di;
            acci += (int64_t)yi * dr - (int64_t)yr * di;
            p++;
        }
        hre[s] = (int32_t)(accr / den);
        him[s] = (int32_t)(acci / den);
    }

    {
        int m = 0;
        for (int s = 0; s < NSYM; s++)
            for (int r = 0; r < PDSCH_NRE; r++) {
                if (r % 4 == 0) continue;
                int32_t yr = yw[(s * PDSCH_NRE + r) * 2 + 0];
                int32_t yi = yw[(s * PDSCH_NRE + r) * 2 + 1];
                int64_t er = (int64_t)yr * hre[s] + (int64_t)yi * him[s];
                int64_t ei = (int64_t)yi * hre[s] - (int64_t)yr * him[s];
                llr_e[2 * m + 0] = (int32_t)er;
                llr_e[2 * m + 1] = (int32_t)ei;
                m++;
            }
    }

    {
        int rows = PDSCH_E / QM;
        for (int i = 0; i < rows; i++)
            for (int j = 0; j < QM; j++)
                llr_i[j * rows + i] = llr_e[QM * i + j];
    }

    gold_init(N_RNTI << 15);
    for (int k = 0; k < PDSCH_E; k++)
        if (gold_bit((uint32_t)k)) llr_i[k] = -llr_i[k];

    for (int k = 0; k < PDSCH_E; k++)
        rbuf[(k0 + (uint32_t)k) % USABLE] += llr_i[k];

    for (int j = 0; j < PUNCT; j++) dem[j] = 0;
    {
        int u = 0;
        for (int i = 0; i < NCODED - PUNCT; i++) {
            if (i >= FILLER0 && i < FILLER0 + FILLER_N) dem[PUNCT + i] = 0;
            else                                        dem[PUNCT + i] = rbuf[u++];
        }
    }

    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t i = 0; i < ZC; i++)
            llr[b][i] = dem[b * ZC + i] >> LLR_SHIFT;

    for (uint32_t r = 0; r < BG2_ROWS; r++)
        for (uint32_t k = 0; k < MAXD; k++)
            for (uint32_t i = 0; i < ZC; i++)
                msg[r][k][i] = (k < bg2_deg[r])
                               ? llr[bg2_col[r][k]][(i + bg2_sh[r][k]) % ZC]
                               : SENTINEL;

    for (uint32_t b = 0; b < BG2_COLS; b++) {
        int32_t (*gath)[ZC] = (int32_t (*)[ZC])(g_all + b * GSPAD);
        for (uint32_t k = 0; k < 24; k++)
            for (uint32_t i = 0; i < ZC; i++) gath[k][i] = 0;
        for (uint32_t i = 0; i < ZC; i++) gath[24][i] = llr[b][i];
    }

    for (uint32_t i = 0; i < ZC; i++) {
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK, i), (uint32_t)SENTINEL);
        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(SENTINEL_BANK + 32, i), (uint32_t)SENTINEL);
    }
    if (gpcp_cgra_imem_load(CGRA_BASE, cn_prog, CN_PROG_CYCLES * NPES))
        errs++;

    for (uint32_t it = 0; it < ITERS; it++) {
        if (gpcp_cgra_imem_load(CGRA_BASE, cn_prog, CN_PROG_CYCLES * NPES))
            errs++;
        cn_row_in(0, 0);
        if (rt_dma_wait(CGRA_BASE)) errs++;
        for (uint32_t r = 0; r < BG2_ROWS; r++) {
            if (r + 1 < BG2_ROWS) cn_row_in(r + 1, (r + 1) & 1u);
            if (rt_array_run(CGRA_BASE, r & 1u, CN_PROG_CYCLES)) errs++;
            runs++;
            if (r + 1 < BG2_ROWS) { if (rt_dma_wait(CGRA_BASE)) errs++; }
            cn_row_out(r, r & 1u);
            if (rt_dma_wait(CGRA_BASE)) errs++;
        }
        rt_dcache_flush();
        reconstruct_gather();

        if (gpcp_cgra_imem_load(CGRA_BASE, bn_prog, BN_PROG_CYCLES * NPES))
            errs++;
        int want_hard = (it + 1u == ITERS);
        bn_dma_in(0);
        if (rt_dma_wait(CGRA_BASE)) errs++;
        for (uint32_t b = 0; b < BG2_COLS; b++) {
            if (rt_array_run(CGRA_BASE, 0, BN_PROG_CYCLES)) errs++;
            runs++;
            bn_dma_out(b, want_hard);
            if (rt_dma_wait(CGRA_BASE)) errs++;
            if (b + 1 < BG2_COLS) bn_dma_in(b + 1);
            bn_scatter(b, want_hard, hard[b]);
            if (b + 1 < BG2_COLS) { if (rt_dma_wait(CGRA_BASE)) errs++; }
        }
    }
    uint32_t cyc = mtime() - t0;

    uint8_t bits[A_TB + CRC_LEN], crc[CRC_LEN];
    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t i = 0; i < ZC; i++)
            out[48 + b * ZC + i] = (hard[b][i] < 0) ? 1u : 0u;
    for (int i = 0; i < A_TB + CRC_LEN; i++)
        bits[i] = (uint8_t)(hard[i / ZC][i % ZC] < 0 ? 1 : 0);
    crc24a(bits, A_TB, crc);
    int pdsch_crc_ok = 1;
    for (int i = 0; i < CRC_LEN; i++)
        if (crc[i] != bits[A_TB + i]) pdsch_crc_ok = 0;

    /* ----------------------------------------------------- publish */
    out[0] = 0xCAFEBABEu;
    out[1] = errs;
    out[2] = cyc;
    out[3] = runs;
    out[4] = matched;
    out[5] = (uint32_t)pdcch_crc_ok;
    out[6] = 0x600Du;
    out[7] = rv;
    out[40] = mcs; out[41] = ndi; out[42] = harq_pid;
    out[43] = rb_start; out[44] = rb_len; out[45] = tpc;
    out[46] = (uint32_t)pdsch_crc_ok;
    out[1088] = (uint32_t)llr_pd[0];
    out[1089] = (uint32_t)pre[sb_perm[0]];
    out[1090] = (uint32_t)llr_e[0];
    out[1091] = (uint32_t)llr_i[0];
    out[1092] = (uint32_t)(dem[PUNCT + 0] >> LLR_SHIFT);
    out[1093] = dmrs_cinit(0);
    out[1094] = dmrs_cinit(1);
    out[1095] = k0;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
