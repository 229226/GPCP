/* -----------------------------------------------------------------------------
 * run-rach: the whole 4-step random-access process on one SoC run.
 *
 *   Msg1  PRACH preamble   -> CGRA cyclic-shift search -> preamble index + TA
 *   Msg2  RA-RNTI DCI      -> Polar SC blind detect (one candidate, the RA-RNTI
 *         the hardware computes itself from the PRACH occasion) -> DCI 1_0 -> rv
 *         -> PDSCH (RNTI = RA-RNTI) -> CRC24A -> RAR MAC PDU -> BI / RAPID /
 *         TA command / 27-bit UL grant / TC-RNTI
 *   Msg3  PUSCH            -> FFT window placed with the TA *measured from Msg1*,
 *         DMRS 6.4.1.1, data descrambled with the TC-RNTI *read out of Msg2*
 *         -> CRC24A -> CCCH SDU
 *   Msg4  contention res.  -> DCI blind-detected with the decoded TC-RNTI ->
 *         PDSCH descrambled with it -> CRC24A -> UE Contention Resolution
 *         Identity CE compared against the SDU this program put into Msg3
 *         (38.321 5.1.5.3: match => the TC-RNTI becomes the C-RNTI)
 *
 * Every RNTI and every timing value below is *derived at run time*, never
 * hard-coded: the RA-RNTI from the occasion, k0 from the decoded DCI, the Msg3
 * window from the Msg1 peak position, and both Msg3 scrambling and Msg4's CRC
 * mask from the TC-RNTI inside the decoded RAR.  Get any of them wrong and the
 * leg downstream fails to decode.
 *
 * Stimulus (tools/nr_rach.py dump, offsets from msg_offsets()):
 *   0x8040_0000  rach.td   ms2 PDCCH 1 sym | ms2 PDSCH 2 sym | ms3 PUSCH 2 timed
 *                          rows | ms4 PDCCH 1 sym | ms4 PDSCH 2 sym
 *   0x8048_0000  prach.td  139 rx chips | 139 rotated 127-tap references
 *   0x8010_0000  FFT twiddle LUT (build/fft/twiddle.bin)
 *
 * OCC= selects the PRACH occasion (the UE knows where it transmitted) and
 * MSG4_RV= the redundancy version of Msg4's first transmission, so the
 * DCI -> rv -> k0 coupling is exercised with a non-zero k0.
 *
 * Results at 0x8018_0000 (32-bit words, mirrored by tools/check_rach.py):
 *   [0] 0xCAFEBABE [1] errors [2] cycles [3] array runs [4] DMA ops [5] 0x600D
 *   Msg1  [8]delay [9]preamble [10]TA [11]peak re [12]peak im [13]2nd delay
 *         [14]N_CS [15]L_RA [16]taps [17]partial checks [18]prog cycles
 *         [19]prach runs   [20+2d]/[21+2d] = C[d].re/.im, d = 0..138
 *   Msg2  [300]RA-RNTI [301]dci ok [302]matched RNTI [303]rv [304]k0 [305]CRC ok
 *         [306..337]32 DCI bits [338]mcs [339]ndi [340]harq_pid [341]rb_start
 *         [342]rb_len [343]tpc [344]reserved [345]BI [346]RAPID [347]TA cmd
 *         [348]TC-RNTI [349]UL grant(27) [350..359]RAR PDU bytes
 *         [360..363]DMRS c_init s0/s1, scr c_init, first dematched LLR>>8
 *         [364..367]channel estimate hre/him per symbol
 *         [370..1409]1040 LDPC hard decisions
 *   Msg3  [1420]TA window [1421]CRC ok [1422]k0 [1423]scr c_init
 *         [1424..1425]DMRS c_init [1426..1429]h [1430..1437]first 8 raw LLRs
 *         [1438..1445]first 8 descrambled LLRs [1446]LCID [1447]SDU len
 *         [1448..1455]CCCH SDU [1456]grant rb_start [1457]rb_len [1458]tda
 *         [1459]mcs   [1460..2499]1040 hard decisions
 *   Msg4  [2510]dci ok [2511]matched RNTI (= TC-RNTI) [2512]rv [2513]k0
 *         [2514]CRC ok [2515..2546]32 DCI bits [2547]mcs [2548]ndi
 *         [2549]harq_pid [2550]rb_start [2551]rb_len [2552]tpc [2553]reserved
 *         [2554]CE matches own SDU [2555]contention resolved [2556]C-RNTI
 *         [2557..2562]CE bytes [2563..2564]DMRS c_init [2565]scr c_init
 *         [2566]first dematched LLR>>8 [2567..2570]h
 *         [2580..3619]1040 hard decisions
 * ----------------------------------------------------------------------------- */
#include "../include/gpcp_cgra.h"
#include "../include/gpcp_cgra_rt.h"
#include "../include/ldpc_bg2.h"
#include "pdcch_luts.h"
#include "pdsch_cn_prog.h"
#include "pdsch_bn_prog.h"
#include "prach_prog.h"

#define CGRA_BASE     0x50000000ul
#define EXIT_ADDR     0x40010000ul
/* Result area: above every scratch buffer (LDPC working set ends ~0x8007_4000,
 * twiddle LUT at 0x8010_0000) and below the message image at 0x8040_0000, so
 * intermediate values can be published as soon as a leg finishes. */
#define RESULT_BASE   ((uint32_t *)0x80180000ul)
#define RESULT_WORDS  3620u

/* ---- ssb_fft.inc prerequisites ---- */
#define NPES          16
#define RING_DEPTH    1024
#define DESC_BASE     0x80020000ul
#define TD_MSG        0x80400000ul
#define TD_PRACH      0x80480000ul

/* ---- OFDM geometry (38.211 numerology 0, as run-pusch / run-pdsch-full) ---- */
#define NFFT          4096
#define CP_LEN        288
#define SYM_LEN       (CP_LEN + NFFT)
#define SYM_WORDS     (2u * SYM_LEN)
#define BIN0          (NFFT / 2 + 1)
#define NSYM          2
#define NRE           240
#define NPILOT        60
#define QPSK          2000
#define H_SCALE       16
#define LLR_SHIFT     8
#define TD_SCALE      256            /* image is pre-scaled; FFT output gets <<4 */

/* ---- PDCCH (control) chain ---- */
#define PDCCH_E       432
#define PDCCH_NRE     216
#define N_ID          200u
#define NPOL          512
#define KINFO         56
#define SDEPTH        9
#define NODE_CYCLES   22
#define LC_IN_BASE    0x80310000ul
#define LB_BASE       0x80320000ul
#define NODE_OUT_BASE 0x80340000ul
#define NODE_OUT_BLOCKS 2048
#define NODE_OUT_STRIDE 32

/* ---- data chain (PDSCH 7.4.1.1 / PUSCH 6.4.1.1, both SISO QPSK) ---- */
#define E_BITS        720
#define USABLE        984
#define FILLER0       64
#define FILLER_N      16
#define PUNCT         40
#define NCODED        1040
#define A_TB          80
#define CRC_LEN       24
#define QM            2
#define DMRS_NID      1              /* PDSCH: n_ID = N_ID^cell >> 1 here */
#define DMRS_NSCID    0
#define DMRS_NSLOT    0
#define DMRS_L0       2
#define DMRS_NSYMB    14
#define DMRS_NCP      0
#define PUSCH_NID     0x1234u        /* 6.4.1.1 keeps a module-level n_ID */
#define PUSCH_NSCID   0
#define PUSCH_NSLOT   0
#define PUSCH_L0      0
#define PUSCH_N_DMRS_SYMB NSYM

/* ---- Msg1 / PRACH (38.211 6.3.3, short sequence L_RA = 139) ---- */
#define L_RA          139u
#define TAPS          127u
#define PAIR_WORDS    (2u * TAPS)
#define Y_SPAD        0u
#define REF_SPAD      256u
#define PART_SPAD     512u
#define OUT_WORDS     64u
#define FINAL_W       60u
#ifndef N_CS
#define N_CS          15u            /* zero-correlation zone from SIB1 zczc 0 */
#endif
#ifndef OCC
#define OCC           1u             /* PRACH occasion the UE transmitted on */
#endif
#define MSG1_FDM      1u             /* SIB1 msg1-FDM = n2 -> 2 occasions/freq */
#define MAX_TA        16u            /* Msg3 timed-row guard band (> N_CS) */
#define ROW3_WORDS    (2u * (2u * SYM_LEN + 2u * MAX_TA))

/* ---- message image word offsets (must equal nr_rach.msg_offsets()) ---- */
#define OFF_MS2_PDCCH  0u
#define OFF_MS2_PDSCH  (OFF_MS2_PDCCH + SYM_WORDS)
#define OFF_MS3_PUSCH  (OFF_MS2_PDSCH + SYM_WORDS * NSYM)
#define OFF_MS4_PDCCH  (OFF_MS3_PUSCH + ROW3_WORDS * NSYM)
#define OFF_MS4_PDSCH  (OFF_MS4_PDCCH + SYM_WORDS)

/* ---- result offsets ---- */
#define R_MSG1   8u
#define R_C0     20u
#define R_MS2    300u
#define R_MS2_H  370u
#define R_MS3    1420u
#define R_MS3_H  1460u
#define R_MS4    2510u
#define R_MS4_H  2580u

/* ---- Gold scrambling (one buffer, re-initialised per use) ---- */
#define GOLD_NC       1600
#define GOLD_LEN      (720 + GOLD_NC + 31)
#define GOLD_BASE     0x80380000ul

/* ---- LDPC engine (same addresses as run-pdsch-full / run-pusch) ---- */
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
#define PART_BASE     0x80300000ul

#define LCID_CCCH     24u
#define CR_ID_BYTES   6u             /* 38.321 6.2.1: 48-bit identity */
#define CAUSE_MO_SIGNALLING 4u

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
static int32_t ds_re[NSYM][NPILOT], ds_im[NSYM][NPILOT];
static int32_t us_re[NSYM][NPILOT], us_im[NSYM][NPILOT];

static int32_t (*msg)[MAXD][ZC] = (int32_t (*)[MAXD][ZC])MSG_BASE;
static int32_t (*res)[3][ZC] = (int32_t (*)[3][ZC])RES_BASE;
static int32_t (*llr)[ZC] = (int32_t (*)[ZC])LLR_BASE;
static int32_t *g_all = (int32_t *)GATH_BASE;
static int32_t *b_all = (int32_t *)BOUT_BASE;
static int32_t hard[BG2_COLS][ZC];

static inline uint32_t mmio_r(uintptr_t a) { return *(volatile uint32_t *)a; }
static inline void mmio_w(uintptr_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
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

/* ------------------------- 38.211 7.4.1.1 PDSCH and 6.4.1.1 PUSCH DMRS ---- */
static uint32_t pdsch_cinit(int s)
{
    uint64_t l = (uint64_t)(DMRS_L0 + s);
    uint64_t v = ((uint64_t)1 << 17)
                 * (uint64_t)(DMRS_NSYMB * DMRS_NSLOT + l + 1)
                 * (uint64_t)(2 * DMRS_NID + DMRS_NSCID + 1)
                 + (uint64_t)(2 * DMRS_NID + DMRS_NCP);
    return (uint32_t)(v & 0x7FFFFFFFu);
}

static uint32_t pusch_cinit(int s)
{
    uint64_t l = (uint64_t)(PUSCH_L0 + s);
    uint64_t v = ((uint64_t)1 << 17)
                 * (uint64_t)(PUSCH_N_DMRS_SYMB * PUSCH_NSLOT + l + 1)
                 * (uint64_t)(2 * PUSCH_NID + 1)
                 + (uint64_t)(2 * PUSCH_NID + PUSCH_NSCID);
    return (uint32_t)(v & 0x7FFFFFFFu);
}

static void qpsk_gold(uint32_t c_init, int32_t *re, int32_t *im)
{
    gold_init(c_init);
    for (int p = 0; p < NPILOT; p++) {
        uint32_t b0 = gold_bit((uint32_t)(2 * p));
        uint32_t b1 = gold_bit((uint32_t)(2 * p + 1));
        re[p] = (int32_t)(1 - 2 * (int)b0) * QPSK;
        im[p] = (int32_t)(1 - 2 * (int)b1) * QPSK;
    }
}

static void build_dmrs(void)
{
    for (int s = 0; s < NSYM; s++) {
        qpsk_gold(pdsch_cinit(s), ds_re[s], ds_im[s]);
        qpsk_gold(pusch_cinit(s), us_re[s], us_im[s]);
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

static int32_t floor_div(int64_t a, int64_t b)
{
    int64_t q = a / b;
    if (a % b != 0 && ((a < 0) != (b < 0))) q--;
    return (int32_t)q;
}

static uint32_t re_bin(uint32_t r) { return (uint32_t)BIN0 + r; }

/* ============================================================ POLAR (PDCCH) */
static uint32_t prog[NODE_CYCLES * NPES];

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
        if (++spins > 4000000u) { errs++; break; }
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
        if (nblk > NODE_OUT_BLOCKS) { errs++; nblk = 0; }

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

/* ------------------------------------------------- one PDCCH leg, one RNTI */
/* Random access is scheduled by RNTIs the UE computes itself (RA-RNTI from the
 * PRACH occasion, TC-RNTI from the RAR), so the search space is a single
 * candidate -- the same shape run-sib1 uses for the SI-RNTI.  Returns the
 * matched RNTI (0 = no CRC24C hit) and the 56 Polar information bits. */
static uint32_t pdcch_leg(uint32_t td_words, uint32_t rnti, uint8_t *info)
{
    int32_t *td = (int32_t *)(TD_MSG + 4u * td_words);
    int32_t *pspec = td + 2 * CP_LEN;

    /* the polar output pool is about to be re-filled by the copy engine; drop
     * every cached line from the previous leg (AGENTS rule 2) */
    rt_dcache_flush();
    nblk = 0;

    fft4096(pspec);
    for (int r = 0; r < PDCCH_NRE; r++) {
        uint32_t idx = rev4(re_bin((uint32_t)r));
        llr_pd[2 * r + 0] = pspec[2 * idx] << 4;
        llr_pd[2 * r + 1] = pspec[2 * idx + 1] << 4;
    }
    gold_init(N_ID);
    for (int k = 0; k < PDCCH_E; k++)
        if (gold_bit((uint32_t)k)) llr_pd[k] = -llr_pd[k];

    for (int i = 0; i < POLAR_N; i++) pre[i] = 0;
    for (int m = 0; m < RM_LEN; m++) pre[m] += llr_pd[tri_perm[m]];
    for (int i = 0; i < POLAR_N; i++) lbuf[0][i] = pre[sb_perm[i]];

    build_node_uc();                 /* the FFT overwrote IMEM (rule 9) */
    sc(0, 0, NPOL);
    int k = 0;
    for (uint32_t i = 0; i < NPOL && k < KINFO; i++)
        if (!is_frozen(i)) info[k++] = bbuf[SDEPTH][i];
    if (k != KINFO) errs++;

    uint8_t expect[24];
    crc24c(info, 32, expect);
    uint8_t got[24];
    for (int j = 0; j < 24; j++) got[j] = info[32 + j];
    for (int j = 0; j < 16; j++) got[j] ^= (uint8_t)((rnti >> j) & 1u);
    for (int j = 0; j < 24; j++)
        if (got[j] != expect[j]) return 0u;
    return rnti;
}

/* ============================================================ LDPC engine */
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

static void bn_scatter(uint32_t b, int want_hard, int32_t *hardb)
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
        for (uint32_t i = 0; i < ZC; i++) hardb[i] = hb[i];
    }
}

/* 8 pure min-sum iterations over the channel LLRs already in llr[][] */
static void ldpc_run(void)
{
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
}

/* ------------------------------------------------------------------- one leg
 * FFT -> per-symbol LS against the given DMRS set -> equalise -> QPSK soft LLR
 * -> 38.212 5.4.2.2 de-interleave -> Gold descramble -> Chase accumulation at
 * k0 -> rate de-match -> LDPC.  `row_words` is the stride between OFDM symbols
 * and `win_words` the FFT window inside the first one, which is all that
 * differs between a plain PDSCH symbol and a TA-timed Msg3 row.
 */
static void data_leg(uint32_t td_words, uint32_t row_words, uint32_t win_words,
                     const int32_t *dm_re, const int32_t *dm_im,
                     uint32_t k0, uint32_t scr_cinit, int32_t *h)
{
    int32_t *td = (int32_t *)(TD_MSG + 4u * td_words);
    int32_t *sp;

    for (int i = 0; i < USABLE; i++) rbuf[i] = 0;

    for (int s = 0; s < NSYM; s++) {
        sp = (int32_t *)(td + (size_t)s * row_words + win_words);
        fft4096(sp);
        for (int r = 0; r < NRE; r++) {
            uint32_t idx = rev4(re_bin((uint32_t)r));
            yw[(s * NRE + r) * 2 + 0] = sp[2 * idx] << 4;
            yw[(s * NRE + r) * 2 + 1] = sp[2 * idx + 1] << 4;
        }
    }

    int64_t den = (int64_t)NPILOT * 2 * QPSK * QPSK / H_SCALE;
    for (int s = 0; s < NSYM; s++) {
        int64_t accr = 0, acci = 0;
        int p = 0;
        for (int r = 0; r < NRE; r += 4) {
            int32_t yr = yw[(s * NRE + r) * 2 + 0];
            int32_t yi = yw[(s * NRE + r) * 2 + 1];
            int32_t dr = dm_re[s * NPILOT + p], di = dm_im[s * NPILOT + p];
            accr += (int64_t)yr * dr + (int64_t)yi * di;   /* y * conj(DMRS) */
            acci += (int64_t)yi * dr - (int64_t)yr * di;
            p++;
        }
        h[2 * s] = floor_div(accr, den);
        h[2 * s + 1] = floor_div(acci, den);
    }

    {
        int m = 0;
        for (int s = 0; s < NSYM; s++)
            for (int r = 0; r < NRE; r++) {
                if (r % 4 == 0) continue;                 /* DMRS RE */
                int32_t yr = yw[(s * NRE + r) * 2 + 0];
                int32_t yi = yw[(s * NRE + r) * 2 + 1];
                int64_t er = (int64_t)yr * h[2 * s] + (int64_t)yi * h[2 * s + 1];
                int64_t ei = (int64_t)yi * h[2 * s] - (int64_t)yr * h[2 * s + 1];
                llr_e[2 * m + 0] = (int32_t)er;
                llr_e[2 * m + 1] = (int32_t)ei;
                m++;
            }
    }

    {   /* 38.212 5.4.2.2 bit de-interleaving (Qm = 2, 360 rows) */
        int rows = E_BITS / QM;
        for (int i = 0; i < rows; i++)
            for (int j = 0; j < QM; j++)
                llr_i[j * rows + i] = llr_e[QM * i + j];
    }

    gold_init(scr_cinit);
    for (int k = 0; k < E_BITS; k++)
        if (gold_bit((uint32_t)k)) llr_i[k] = -llr_i[k];

    for (int k = 0; k < E_BITS; k++)
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

    ldpc_run();
}

/* CRC24A over the decoded codeword; also returns the 80-bit TB as PDU bytes */
static int tb_crc(uint8_t *pdu)
{
    uint8_t bits[A_TB + CRC_LEN], crc[CRC_LEN];
    for (int i = 0; i < A_TB + CRC_LEN; i++)
        bits[i] = (uint8_t)(hard[i / ZC][i % ZC] < 0 ? 1 : 0);
    crc24a(bits, A_TB, crc);
    for (int i = 0; i < A_TB / 8; i++) pdu[i] = 0;
    for (int i = 0; i < A_TB; i++)
        pdu[i / 8] = (uint8_t)(pdu[i / 8] | (bits[i] << (7 - (i % 8))));
    for (int i = 0; i < CRC_LEN; i++)
        if (crc[i] != bits[A_TB + i]) return 0;
    return 1;
}

static void publish_hard(uint32_t base)
{
    uint32_t *out = RESULT_BASE;
    for (uint32_t b = 0; b < BG2_COLS; b++)
        for (uint32_t i = 0; i < ZC; i++)
            out[base + b * ZC + i] = (uint32_t)(hard[b][i] < 0 ? 1 : 0);
}

/* the 32 information bits, MSB-first field by field, exactly as nr_pdcch packs
 * them: mcs(5) rv(2) ndi(1) harq_pid(4) rb_start(4) rb_len(4) tpc(2) rsv(10) */
static void dci_fields(const uint8_t *info, uint32_t *f)
{
    uint32_t mcs = 0, rv = 0, ndi = 0, pid = 0, rbs = 0, rbl = 0, tpc = 0, rsv = 0;
    for (int i = 0; i < 5; i++) mcs = (mcs << 1) | info[i];
    for (int i = 5; i < 7; i++) rv  = (rv << 1) | info[i];
    ndi = info[7];
    for (int i = 8; i < 12; i++) pid = (pid << 1) | info[i];
    for (int i = 12; i < 16; i++) rbs = (rbs << 1) | info[i];
    for (int i = 16; i < 20; i++) rbl = (rbl << 1) | info[i];
    for (int i = 20; i < 22; i++) tpc = (tpc << 1) | info[i];
    for (int i = 22; i < 32; i++) rsv = (rsv << 1) | info[i];
    f[0] = mcs; f[1] = rv; f[2] = ndi; f[3] = pid;
    f[4] = rbs; f[5] = rbl; f[6] = tpc; f[7] = rsv;
}

/* ================================================================ Msg1 */
/* One 128-cycle array run per candidate cyclic shift; the CPU re-sums the 16
 * per-PE partials as an independent check on the array's own butterfly. */
static uint32_t msg1_prach(uint32_t *delay, uint32_t *preamble, uint32_t *ta,
                           uint32_t *checks)
{
    uint32_t *out = RESULT_BASE;
    uintptr_t refs = TD_PRACH + 4u * (2u * L_RA);

    if (gpcp_cgra_imem_load(CGRA_BASE, prach_prog, PRACH_PROG_CYCLES * NPES))
        errs++;
    /* the received preamble stays resident for the whole shift search */
    if (rt_dma_run(CGRA_BASE, TD_PRACH, Y_SPAD, PAIR_WORDS, 0, 0, 0, 0))
        errs++;
    dmas++;

    for (uint32_t d = 0; d < L_RA; d++) {
        if (rt_dma_run(CGRA_BASE, refs + 4u * d * PAIR_WORDS, REF_SPAD,
                       PAIR_WORDS, 0, 0, 0, 0)) { errs++; break; }
        dmas++;
        if (rt_array_run(CGRA_BASE, 0, PRACH_PROG_CYCLES)) errs++;
        runs++;
        if (rt_dma_run(CGRA_BASE, PART_BASE + d * OUT_WORDS * 4u, PART_SPAD,
                       OUT_WORDS, 0, 1, 0, 0)) { errs++; break; }
        dmas++;
    }

    int64_t best = -1, second = -1;
    uint32_t bd = 0, sd = 0;
    int32_t bre = 0, bim = 0;
    for (uint32_t d = 0; d < L_RA; d++) {
        const int32_t *s = (const int32_t *)(PART_BASE + d * OUT_WORDS * 4u);
        int64_t re = 0, im = 0;
        for (uint32_t p = 0; p < NPES; p++) {
            re += s[2 * p];
            im += s[2 * p + 1];
            (*checks) += 2;
        }
        if ((int64_t)s[FINAL_W] != re || (int64_t)s[FINAL_W + 1] != im)
            errs++;                       /* array butterfly disagrees with CPU */
        int64_t pw = re * re + im * im;
        if (pw > best) {
            second = best; sd = bd;
            best = pw; bre = (int32_t)re; bim = (int32_t)im; bd = d;
        } else if (pw > second) {
            second = pw; sd = d;
        }
        out[R_C0 + 2 * d] = (uint32_t)re;
        out[R_C0 + 2 * d + 1] = (uint32_t)im;
    }
    *delay = bd;
    *preamble = bd / N_CS;
    *ta = bd % N_CS;
    out[R_MSG1 + 3] = (uint32_t)bre;
    out[R_MSG1 + 4] = (uint32_t)bim;
    out[R_MSG1 + 5] = sd;
    out[R_MSG1 + 6] = N_CS;
    out[R_MSG1 + 7] = L_RA;
    out[R_MSG1 + 8] = TAPS;
    return runs;
}

/* ================================================================ main */
int main(void)
{
    uint32_t *out = RESULT_BASE;
    uint32_t t0 = mtime();
    uint8_t info[64], pdu[10], ce[CR_ID_BYTES];
    uint32_t f[8];
    int32_t h[4];

    out[0] = 0xCAFEBABEu;
    out[5] = 0x600Du;

    build_tables();
    build_dmrs();

    /* ------------------------------------------------------ Msg1: PRACH */
    uint32_t delay, preamble, ta, m1checks = 0;
    uint32_t m1runs = msg1_prach(&delay, &preamble, &ta, &m1checks);
    out[R_MSG1] = delay;
    out[R_MSG1 + 1] = preamble;
    out[R_MSG1 + 2] = ta;
    out[R_MSG1 + 9] = m1checks;
    out[R_MSG1 + 10] = PRACH_PROG_CYCLES;
    out[R_MSG1 + 11] = m1runs;

    /* The RA-RNTI is not signalled anywhere: the UE builds it from the PRACH
     * occasion it transmitted on (38.321 6.1.5). */
    uint32_t n_fdm = 1u << MSG1_FDM;
    uint32_t f_id = OCC % n_fdm;
    uint32_t rest = OCC / n_fdm;
    uint32_t t_id = rest % 10u;
    uint32_t s_id = (rest / 10u) % 14u;
    uint32_t ra_rnti = 1u + s_id + 6u * t_id + 60u * f_id;
    out[R_MS2] = ra_rnti;

    /* ------------------------------------------------- Msg2: RAR delivery */
    uint32_t matched2 = pdcch_leg(OFF_MS2_PDCCH, ra_rnti, info);
    dci_fields(info, f);
    out[R_MS2 + 1] = matched2 != 0u ? 1u : 0u;
    out[R_MS2 + 2] = matched2;
    out[R_MS2 + 3] = f[1];                          /* rv */
    for (int i = 0; i < 32; i++) out[R_MS2 + 6 + i] = info[i];
    out[R_MS2 + 38] = f[0];  out[R_MS2 + 39] = f[2]; out[R_MS2 + 40] = f[3];
    out[R_MS2 + 41] = f[4];  out[R_MS2 + 42] = f[5]; out[R_MS2 + 43] = f[6];
    out[R_MS2 + 44] = f[7];
    uint32_t k0_2 = f[1] * (USABLE / 4u);
    out[R_MS2 + 4] = k0_2;

    data_leg(OFF_MS2_PDSCH, SYM_WORDS, 2 * CP_LEN, &ds_re[0][0], &ds_im[0][0],
             k0_2, ra_rnti << 15, h);
    int crc2 = tb_crc(pdu);
    out[R_MS2 + 5] = (uint32_t)crc2;
    out[R_MS2 + 60] = pdsch_cinit(0);
    out[R_MS2 + 61] = pdsch_cinit(1);
    out[R_MS2 + 62] = ra_rnti << 15;
    out[R_MS2 + 63] = (uint32_t)(dem[PUNCT] >> LLR_SHIFT);
    for (int i = 0; i < 4; i++) out[R_MS2 + 64 + i] = (uint32_t)h[i];
    for (int i = 0; i < 10; i++) out[R_MS2 + 50 + i] = pdu[i];
    publish_hard(R_MS2_H);

    /* 38.321 6.2.3 RAR: BI subheader, RAPID, TA(12) | grant(27) | rsv(1) |
     * TC-RNTI(24).  The TC-RNTI steers everything from here on. */
    uint32_t bi = pdu[0] & 0x3Fu;
    uint32_t rapid = pdu[1];
    uint64_t body = 0;
    for (int i = 0; i < 8; i++) body = (body << 8) | pdu[2 + i];
    uint32_t tc_rnti = (uint32_t)(body & 0xFFFFFFu);
    uint32_t grant = (uint32_t)((body >> 25) & 0x7FFFFFFu);
    uint32_t ta_cmd = (uint32_t)(body >> 52);
    out[R_MS2 + 45] = bi;
    out[R_MS2 + 46] = rapid;
    out[R_MS2 + 47] = ta_cmd;
    out[R_MS2 + 48] = tc_rnti;
    out[R_MS2 + 49] = grant;

    /* RAR subheader field order, MSB first (38.321 Table 6.2.3-2): fh(1)
     * rb_start(6) rb_len(3) tda(4) mcs(4) harq_timing(3) tpc(3)
     * power_class(2) rsv(1) -- the 1-bit rsv sits at bit 0, so mcs starts at 9. */
    uint32_t g_rb_start = (grant >> 20) & 0x3Fu;
    uint32_t g_rb_len   = (grant >> 17) & 0x7u;
    uint32_t g_tda      = (grant >> 13) & 0xFu;
    uint32_t g_mcs      = (grant >> 9) & 0xFu;

    /* ---------------------------------------------------- Msg3: PUSCH */
    /* The receive window comes out of the Msg1 peak position -- nothing else
     * feeds it.  Msg3 is always rv = 0 (38.321 5.1.3), so k0 = 0. */
    uint32_t ta_window = ta;
    uint32_t win3 = 2u * (MAX_TA + ta_window + CP_LEN);
    out[R_MS3] = ta_window;
    data_leg(OFF_MS3_PUSCH, ROW3_WORDS, win3, &us_re[0][0], &us_im[0][0],
             0u, tc_rnti << 15, h);
    int crc3 = tb_crc(pdu);
    out[R_MS3 + 1] = (uint32_t)crc3;
    out[R_MS3 + 2] = 0u;
    out[R_MS3 + 3] = tc_rnti << 15;
    out[R_MS3 + 4] = pusch_cinit(0);
    out[R_MS3 + 5] = pusch_cinit(1);
    for (int i = 0; i < 4; i++) out[R_MS3 + 6 + i] = (uint32_t)h[i];
    for (int i = 0; i < 8; i++) out[R_MS3 + 10 + i] = llr_e[i];
    for (int i = 0; i < 8; i++) out[R_MS3 + 18 + i] = llr_i[i];
    for (int i = 0; i < 8; i++) out[R_MS3 + 28 + i] = pdu[2 + i];   /* CCCH SDU */
    out[R_MS3 + 26] = pdu[0] & 0x3Fu;                 /* LCID */
    out[R_MS3 + 27] = pdu[1];                         /* length */
    out[R_MS3 + 36] = g_rb_start;
    out[R_MS3 + 37] = g_rb_len;
    out[R_MS3 + 38] = g_tda;
    out[R_MS3 + 39] = g_mcs;
    publish_hard(R_MS3_H);

    /* The UE-side view of the same SDU: this program *is* the UE for Msg4, so
     * it builds the RRCSetupRequest it would have sent from the preamble index
     * it used (compact 64-bit profile: cause(4) identity(40) preamble(6) rsv(14)). */
    uint64_t ue_ident = 0x1234ABCD00ull + preamble;
    uint64_t v = 0;
    v = (v << 4) | CAUSE_MO_SIGNALLING;
    v = (v << 40) | (ue_ident & 0xFFFFFFFFFFull);
    v = (v << 6) | (preamble & 0x3Fu);
    v = (v << 14);
    uint8_t own[8];
    for (int i = 0; i < 8; i++)
        own[i] = (uint8_t)((v >> (8 * (7 - i))) & 0xFFu);

    /* ------------------------------------------------- Msg4: contention */
    uint32_t matched4 = pdcch_leg(OFF_MS4_PDCCH, tc_rnti & 0xFFFFu, info);
    dci_fields(info, f);
    out[R_MS4] = matched4 != 0u ? 1u : 0u;
    out[R_MS4 + 1] = matched4;
    out[R_MS4 + 2] = f[1];
    for (int i = 0; i < 32; i++) out[R_MS4 + 5 + i] = info[i];
    out[R_MS4 + 37] = f[0];  out[R_MS4 + 38] = f[2]; out[R_MS4 + 39] = f[3];
    out[R_MS4 + 40] = f[4];  out[R_MS4 + 41] = f[5]; out[R_MS4 + 42] = f[6];
    out[R_MS4 + 43] = f[7];
    uint32_t k0_4 = f[1] * (USABLE / 4u);
    out[R_MS4 + 3] = k0_4;

    data_leg(OFF_MS4_PDSCH, SYM_WORDS, 2 * CP_LEN, &ds_re[0][0], &ds_im[0][0],
             k0_4, tc_rnti << 15, h);
    int crc4 = tb_crc(pdu);
    out[R_MS4 + 4] = (uint32_t)crc4;
    for (uint32_t i = 0; i < CR_ID_BYTES; i++) ce[i] = pdu[2 + i];
    int ce_match = (pdu[0] & 0x3Fu) == LCID_CCCH && pdu[1] == CR_ID_BYTES;
    for (uint32_t i = 0; i < CR_ID_BYTES; i++) {
        out[R_MS4 + 47 + i] = ce[i];
        if (ce[i] != own[i]) ce_match = 0;
    }
    out[R_MS4 + 53] = pdsch_cinit(0);
    out[R_MS4 + 54] = pdsch_cinit(1);
    out[R_MS4 + 55] = tc_rnti << 15;
    out[R_MS4 + 56] = (uint32_t)(dem[PUNCT] >> LLR_SHIFT);
    for (int i = 0; i < 4; i++) out[R_MS4 + 57 + i] = (uint32_t)h[i];
    publish_hard(R_MS4_H);

    uint32_t resolved = crc2 && crc3 && crc4 && matched4 == tc_rnti &&
                        ta_cmd == ta && rapid == preamble && ce_match;
    out[R_MS4 + 44] = ce_match ? 1u : 0u;
    out[R_MS4 + 45] = resolved;
    out[R_MS4 + 46] = resolved ? tc_rnti : 0u;

    /* ------------------------------------------------------- epilogue */
    out[1] = errs;
    out[2] = mtime() - t0;
    out[3] = runs;
    out[4] = dmas;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
