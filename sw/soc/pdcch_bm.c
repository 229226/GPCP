/* -----------------------------------------------------------------------------
 * PDCCH / DCI blind detection on the GPCP SoC (38.211 7.3 + 38.212 7.3).
 *
 *   TD samples (1 OFDM symbol, CP + 4096 IFFT, loaded at 0x8040_0000)
 *     -> 4096-point FFT (ssb_fft.inc, the run-fft verified engine)
 *     -> extract 216 QPSK REs -> 432 soft LLRs
 *     -> Gold descrambling (38.211 7.3.2.3, c_init = N_ID)
 *     -> inverse rate matching (E=432 -> N=512, pdcch_luts.h)
 *     -> polar SC decode on the CGRA (22-cycle f/g node microcode, K=56)
 *     -> RNTI blind detection: CRC24C with the RNTI mask, 5 candidates
 *     -> 32-bit DCI payload
 *
 * The scrambling seed is the cell ID, NOT the RNTI (common search space), so
 * one Polar decode serves every candidate -- the RNTI only unmasks the CRC.
 * Acceptance: tools/check_pdcch.py (anchors from tools/nr_pdcch.py).
 *
 * Result area (0x8001_0000, 32-bit words):
 *   [0] 0xCAFEBABE  [1] errors  [2] cycles  [3] array runs
 *   [4] matched RNTI (0 if none)  [5] CRC ok  [6] 0x600D  [7] candidates
 *   [8..39]   32 DCI bits
 *   [40..44]  per-candidate CRC result
 *   [45..60]  first 16 raw LLRs
 *   [61..76]  first 16 de-matched polar LLRs
 *   [80..135] 56 polar information bits
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "pdcch_luts.h"

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
#define RESULT_BASE   ((uint32_t *)0x80010000ul)

#define NPES          16
#define RING_DEPTH    1024
#define DESC_BASE     0x80020000ul
#define TD_RAM        0x80400000ul

#define NFFT          4096
#define CP_LEN        288
#define BIN0          (NFFT / 2 + 1)
#define NRE           216
#define E_BITS        432
#define SYM_WORDS     (2 * (CP_LEN + NFFT))
#define N_ID          200u
#define GOLD_NC       1600
#define GOLD_LEN      (E_BITS + GOLD_NC + 31)
#define GOLD_BASE     0x80300000ul

#define NPOL          512
#define KINFO         56
#define SDEPTH        9
#define NODE_CYCLES   22
#define LC_IN_BASE    0x80310000ul
#define LB_BASE       0x80320000ul
#define NODE_OUT_BASE 0x80340000ul
#define NODE_OUT_BLOCKS 2048
#define NODE_OUT_STRIDE 32

#define NCAND         5
static const uint32_t candidates[NCAND] = {
    0x1111u, 0x5678u, 0x1234u, 0x9ABCu, 0xDEF0u
};

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
static int32_t llr_e[E_BITS];
static int32_t pre[POLAR_N];

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

static uint32_t prog[NODE_CYCLES * NPES];
static uint32_t errs, runs, n_desc, done, nblk;
static uint32_t dbg_wait, dbg_array_spin, dbg_nblk;

static void wait_desc(uint32_t want)
{
    uint32_t spins = 0, got;
    while ((got = mmio_r(CGRA_BASE + GPCP_CGRA_RING_DONE)) != want) {
        if (++spins > 4000000u) { (void)got; errs++; dbg_wait++; break; }
    }
}

#include "ssb_fft.inc"          /* fft4096() + rev4() */

static uint32_t sidx(uint32_t bank, uint32_t word) { return bank * 32u + word; }

static void build_node_uc(void)
{
    for (uint32_t i = 0; i < NODE_CYCLES * NPES; i++)
        prog[i] = gpcp_cgra_inst(GPCP_OP_NOP, 0, 0, 0, 0);
    for (uint32_t pe = 0; pe < NPES; pe++) {
#define E(cyc, op, a, b, d, imm) prog[(cyc) * NPES + pe] = gpcp_cgra_inst(op, a, b, d, imm)
        E(0,  GPCP_OP_LD, 0, 0, 0, sidx(0, pe));
        E(1,  GPCP_OP_LD, 0, 0, 1, sidx(1, pe));
        E(2,  GPCP_OP_LD, 0, 0, 2, sidx(2, pe));
        E(3,  GPCP_OP_SHR, 0, 0, 3, 31);
        E(4,  GPCP_OP_XOR, 0, 3, 4, 0);
        E(5,  GPCP_OP_SUB, 4, 3, 4, 0);
        E(6,  GPCP_OP_SHR, 1, 0, 5, 31);
        E(7,  GPCP_OP_XOR, 1, 5, 6, 0);
        E(8,  GPCP_OP_SUB, 6, 5, 6, 0);
        E(9,  GPCP_OP_MIN, 4, 6, 7, 0);
        E(10, GPCP_OP_XOR, 3, 5, 3, 0);
        E(11, GPCP_OP_XOR, 7, 3, 4, 0);
        E(12, GPCP_OP_SUB, 4, 3, 4, 0);
        E(13, GPCP_OP_SUB, GPCP_SRC_ZERO, 2, 6, 0);
        E(14, GPCP_OP_AND, 0, 6, 7, 0);
        E(15, GPCP_OP_SHL, 7, 0, 7, 1);
        E(16, GPCP_OP_ADD, 1, 0, 6, 0);
        E(17, GPCP_OP_SUB, 6, 7, 6, 0);
        E(18, GPCP_OP_ST, 4, 0, 0, sidx(16, pe));
        E(19, GPCP_OP_ST, 6, 0, 0, sidx(17, pe));
#undef E
    }
    for (uint32_t i = 0; i < NODE_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, prog[i]);
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

/* ------------------------------------------------------------ 38.211 Gold */
static void gold_init(uint32_t c_init)
{
    uint32_t n = E_BITS + GOLD_NC;
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

/* ------------------------------------------------------------ CRC24C (38.212 7.2) */
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

/* ------------------------------------------------------------ main */
int main(void)
{
    uint32_t *out = RESULT_BASE;
    uint32_t t0 = mtime();
    int32_t *td = (int32_t *)TD_RAM;
    int32_t *spec = td + 2 * CP_LEN;

    /* ---- OFDM demodulation ---- */
    fft4096(spec);
    for (int r = 0; r < NRE; r++) {
        uint32_t idx = rev4((uint32_t)(BIN0 + r));
        llr_e[2 * r + 0] = spec[2 * idx] << 4;
        llr_e[2 * r + 1] = spec[2 * idx + 1] << 4;
    }
    for (int i = 0; i < 16; i++) out[45 + i] = (uint32_t)llr_e[i];

    /* ---- descramble (c_init = N_ID, RNTI-independent) ---- */
    gold_init(N_ID);
    for (int k = 0; k < E_BITS; k++)
        if (gold_bit((uint32_t)k)) llr_e[k] = -llr_e[k];

    /* ---- inverse rate matching: E=432 -> N=512 ---- */
    for (int i = 0; i < POLAR_N; i++) pre[i] = 0;
    for (int m = 0; m < RM_LEN; m++) pre[m] += llr_e[tri_perm[m]];
    for (int i = 0; i < POLAR_N; i++) lbuf[0][i] = pre[sb_perm[i]];
    for (int i = 0; i < 16; i++) out[61 + i] = (uint32_t)lbuf[0][i];

    /* ---- polar SC decode on the CGRA ---- */
    build_node_uc();
    sc(0, 0, NPOL);
    uint8_t info[KINFO];
    {
        int k = 0;
        for (uint32_t i = 0; i < NPOL && k < KINFO; i++)
            if (!is_frozen(i)) info[k++] = bbuf[SDEPTH][i];
        if (k != KINFO) errs++;
    }
    for (int i = 0; i < KINFO; i++) out[80 + i] = info[i];

    /* ---- RNTI blind detection ---- */
    uint8_t expect[24];
    crc24c(info, 32, expect);
    uint32_t matched = 0;
    int crc_ok = 0;
    for (int c = 0; c < NCAND; c++) {
        uint8_t got[24];
        for (int j = 0; j < 24; j++) got[j] = info[32 + j];
        for (int j = 0; j < 16; j++) got[j] ^= (uint8_t)((candidates[c] >> j) & 1u);
        int ok = 1;
        for (int j = 0; j < 24; j++)
            if (got[j] != expect[j]) { ok = 0; break; }
        out[40 + c] = (uint32_t)ok;
        if (ok && !matched) {
            matched = candidates[c];
            crc_ok = 1;
            for (int i = 0; i < 32; i++) out[8 + i] = info[i];
        }
    }

    uint32_t cyc = mtime() - t0;
    out[0] = 0xCAFEBABEu;
    out[1] = errs;
    out[2] = cyc;
    out[3] = runs;
    out[4] = matched;
    out[5] = (uint32_t)crc_ok;
    out[6] = 0x600Du;
    out[7] = (uint32_t)NCAND;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
