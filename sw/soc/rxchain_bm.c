/* GPCP SoC - L1 receive chain over the descriptor ring with double buffering
 *
 * One slot runs a *fused* microcode image, i.e. the three existing L1 kernels
 * back to back in one array run (87 cycles):
 *
 *   cycles  0..14   chnest : bank p word 0..3 (Yr,Yi,Xr,Xi) -> word 4,5 (Hr,Hi)
 *   cycles 15..34   mmse   : reads word 4,5 (H) and 2,3 (Y)   -> word 20,21,22
 *                            (Nr,Ni,|H|^2; the division stays on the CPU)
 *   cycles 35..86   ldpc   : reads word 20,21,22 and word 4   -> word 4,5,6
 *                            (min1,min2,sign of the 4 messages)
 *
 * Slot scheduling: the array works on page p while the copy engine fills or
 * drains page p^1, driven by the descriptor ring (the CPU only pushes RING_TAIL):
 *
 *   ring: IN0(p0) IN1(p1) | OUT0(p0) IN2(p0) | OUT1(p1) IN3(p1) | OUT2(p0) OUT3(p1)
 *
 * Result area (RESULT_BASE, 32-bit words):
 *   [0..191]     4 slots x 16 PEs x {min1, min2, sign}
 *   [192]        marker 0xCAFEBABE
 *   [193]        interrupts taken
 *   [194]        unexpected traps
 *   [195]        on-chip error count (timeouts + software self-check)
 *   [196]        descriptors completed (must be 8)
 *   [197]        cycles for the whole chain (mtime)
 *   [198]        epilogue 0x600D
 *   [199]        cycles spent waiting for the array (4 runs)
 *   [200]        cycles spent waiting for descriptors
 */

#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"

#define CGRA_BASE    0x50000000ul
#define PLIC_BASE    0x0C000000ul
#define PLIC_PRIO(id)     (PLIC_BASE + 4u * (id))
#define PLIC_ENABLE(ctx)  (PLIC_BASE + 0x1000u + 4u * (ctx))
#define PLIC_THRESH(ctx)  (PLIC_BASE + 0x2000u + 4u * (ctx))
#define PLIC_CLAIM(ctx)   (PLIC_BASE + 0x200000u + 4u * (ctx))

#define CLINT_MTIME     0x0200BFF8ul
#define EXIT_ADDR       0x40010000ul
#define RESULT_BASE     ((uint32_t *)0x80010000ul)

#define SLOTS       4
#define NPES        16
#define WORDS_IN    64      /* 4 words per bank (Yr,Yi,Xr,Xi)        */
#define WORDS_OUT   48      /* 3 words per bank (min1,min2,sign)     */

#define CYC_A       15      /* LS channel estimate      */
#define CYC_B       20      /* MMSE numerator + |H|^2   */
#define CYC_D       40      /* 16-QAM max-log demapper  */
#define CYC_C       52      /* LDPC check node          */
#define CHAIN_CYCLES (CYC_A + CYC_B + CYC_D + CYC_C)     /* 127 */

/* per-bank scratchpad word map (page relative) */
#define W_YR   0
#define W_YI   1
#define W_XR   2
#define W_XI   3
#define W_HR   4
#define W_HI   5
#define W_NR   20
#define W_NI   21
#define W_MAG  22
#define W_SIGN 6
#define W_LLR_I1 24      /* 16-QAM bits: I MSB, I LSB, Q MSB, Q LSB */
#define W_LLR_I0 25
#define W_LLR_Q1 26
#define W_LLR_Q0 27
#define LLR_SHIFT 10     /* LLR normalisation (metric units -> soft bits) */

#define MSTATUS_MIE 0x8u
#define MIE_MEIE    0x800u
#define CGRA_SRC_ID 1u

static int32_t  in[SLOTS][WORDS_IN] __attribute__((aligned(8)));
static int32_t  out[SLOTS][WORDS_OUT] __attribute__((aligned(8)));
static uint32_t desc[16][8] __attribute__((aligned(64)));
static uint32_t chain_prog[CHAIN_CYCLES * NPES];

static uint32_t t_array;     /* cycles spent waiting for the array */
static uint32_t t_ring;      /* cycles spent waiting for descriptors  */

static volatile uint32_t irq_count;
static volatile uint32_t arr_count;      /* array completions (progress, not an edge) */
static volatile uint32_t bad_traps;
static uint32_t errs;


/* --------------------------------------------------------------- trap entry */
void trap_handler(void)
{
    uint64_t cause;
    __asm__ __volatile__("csrr %0, mcause" : "=r"(cause));

    if (cause == 0x800000000000000Bull) {
        uint32_t id = mmio_r(PLIC_CLAIM(0));
        if (id == CGRA_SRC_ID) {
            uint32_t st = gpcp_cgra_irq_status(CGRA_BASE);
            if (st & GPCP_CGRA_IRQ_ARR) arr_count++;
            gpcp_cgra_irq_clear(CGRA_BASE, st);    /* claim first, then clear */
            irq_count++;
        } else {
            bad_traps++;
        }
        mmio_w(PLIC_CLAIM(0), id);
    } else {
        bad_traps++;
    }
}

static void plic_setup(void)
{
    mmio_w(PLIC_PRIO(CGRA_SRC_ID), 1);
    mmio_w(PLIC_ENABLE(0), 1u << CGRA_SRC_ID);
    mmio_w(PLIC_THRESH(0), 0);
}

static void irq_unmask(void)
{
    uint64_t mie = MIE_MEIE;
    __asm__ __volatile__("csrw mie, %0" :: "r"(mie));
    __asm__ __volatile__("csrs mstatus, %0" :: "r"((uint64_t)MSTATUS_MIE));
}

/* ------------------------------------------------------------ wait helpers */
/* Wait for n completed descriptors.  Progress is a level (RING_DONE), not an
 * interrupt edge, so an interrupt that overlaps the handler cannot be lost. */
static void wait_ring(uint32_t n)
{
    uint32_t g = 0;
    uint32_t t0 = mmio_r(CLINT_MTIME);
    while (gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_RING_DONE) < n) {
#if RINGDBG
        /* poll only while debugging: a wfi would hide a dead engine */
#else
        __asm__ __volatile__("csrci mstatus, 8");
        if (gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_RING_DONE) < n)
            __asm__ __volatile__("wfi");
        __asm__ __volatile__("csrsi mstatus, 8");
#endif
        if (++g > 400000u) { errs++; break; }
    }
    t_ring += mmio_r(CLINT_MTIME) - t0;
}

/* Wait for one array run.  The sticky IRQ status bit is a W1C register that the
 * handler clears for *both* sources, so polling it can miss a completion that
 * lands while another interrupt is being serviced.  Counting completions in the
 * handler (a progress counter) is immune to that race - same idea as RING_DONE. */
static void array_run(uint32_t page)
{
    uint32_t t0 = mmio_r(CLINT_MTIME);
    uint32_t want = arr_count + 1;
    gpcp_cgra_array_start(CGRA_BASE, page, CHAIN_CYCLES);
    uint32_t g = 0;
    while (arr_count < want) {
        __asm__ __volatile__("csrci mstatus, 8");
        if (arr_count < want)
            __asm__ __volatile__("wfi");
        __asm__ __volatile__("csrsi mstatus, 8");
        if (++g > 200000u) { errs++; break; }
    }
    t_array += mmio_r(CLINT_MTIME) - t0;
}

/* --------------------------------------------------------------- microcode */
static void emit(uint32_t cyc, uint32_t pe, uint32_t op, uint32_t a, uint32_t b,
                 uint32_t dst, uint32_t imm)
{
    chain_prog[cyc * NPES + pe] = gpcp_cgra_inst(op, a, b, dst, imm);
}

static void build_chain(void)
{
    for (uint32_t c = 0; c < CHAIN_CYCLES * NPES; c++)
        chain_prog[c] = gpcp_cgra_inst(GPCP_OP_NOP, 0, 0, 0, 0);

    for (uint32_t p = 0; p < NPES; p++) {
        /* ---- stage A: LS channel estimate (chnest16) ---- */
        emit(0,  p, GPCP_OP_LD, 0, 0, 0, GPCP_SPAD_IDX(p, W_YR));
        emit(1,  p, GPCP_OP_LD, 0, 0, 1, GPCP_SPAD_IDX(p, W_YI));
        emit(2,  p, GPCP_OP_LD, 0, 0, 2, GPCP_SPAD_IDX(p, W_XR));
        emit(3,  p, GPCP_OP_LD, 0, 0, 3, GPCP_SPAD_IDX(p, W_XI));
        emit(4,  p, GPCP_OP_MUL, 0, 2, 4, 0);           /* Yr*Xr */
        emit(5,  p, GPCP_OP_MUL, 1, 3, 5, 0);           /* Yi*Xi */
        emit(6,  p, GPCP_OP_ADD, 4, 5, 4, 0);
        emit(7,  p, GPCP_OP_SHR, 4, 0, 4, 15);          /* Hr */
        emit(8,  p, GPCP_OP_MUL, 1, 2, 5, 0);           /* Yi*Xr */
        emit(9,  p, GPCP_OP_MUL, 0, 3, 6, 0);           /* Yr*Xi */
        emit(10, p, GPCP_OP_SUB, 5, 6, 5, 0);
        emit(11, p, GPCP_OP_SHR, 5, 0, 5, 15);          /* Hi */
        emit(12, p, GPCP_OP_ST, 4, 0, 0, GPCP_SPAD_IDX(p, W_HR));
        emit(13, p, GPCP_OP_ST, 5, 0, 0, GPCP_SPAD_IDX(p, W_HI));

        /* ---- stage B: MMSE numerator + |H|^2 (mmse16) ---- */
        emit(CYC_A + 0,  p, GPCP_OP_LD, 0, 0, 0, GPCP_SPAD_IDX(p, W_HR));
        emit(CYC_A + 1,  p, GPCP_OP_LD, 0, 0, 1, GPCP_SPAD_IDX(p, W_HI));
        emit(CYC_A + 2,  p, GPCP_OP_LD, 0, 0, 2, GPCP_SPAD_IDX(p, W_YR));
        emit(CYC_A + 3,  p, GPCP_OP_LD, 0, 0, 3, GPCP_SPAD_IDX(p, W_YI));
        emit(CYC_A + 4,  p, GPCP_OP_MUL, 0, 2, 4, 0);    /* Hr*Yr */
        emit(CYC_A + 5,  p, GPCP_OP_MUL, 1, 3, 5, 0);    /* Hi*Yi */
        emit(CYC_A + 6,  p, GPCP_OP_ADD, 4, 5, 4, 0);
        emit(CYC_A + 7,  p, GPCP_OP_SHR, 4, 0, 4, 15);   /* Nr */
        emit(CYC_A + 8,  p, GPCP_OP_MUL, 1, 2, 5, 0);    /* Hi*Yr */
        emit(CYC_A + 9,  p, GPCP_OP_MUL, 0, 3, 6, 0);    /* Hr*Yi */
        emit(CYC_A + 10, p, GPCP_OP_SUB, 5, 6, 5, 0);
        emit(CYC_A + 11, p, GPCP_OP_SHR, 5, 0, 5, 15);   /* Ni */
        emit(CYC_A + 12, p, GPCP_OP_MUL, 0, 0, 6, 0);    /* Hr^2 */
        emit(CYC_A + 13, p, GPCP_OP_MUL, 1, 1, 7, 0);    /* Hi^2 */
        emit(CYC_A + 14, p, GPCP_OP_ADD, 6, 7, 6, 0);
        emit(CYC_A + 15, p, GPCP_OP_SHR, 6, 0, 6, 15);   /* |H|^2 */
        emit(CYC_A + 16, p, GPCP_OP_ST, 4, 0, 0, GPCP_SPAD_IDX(p, W_NR));
        emit(CYC_A + 17, p, GPCP_OP_ST, 5, 0, 0, GPCP_SPAD_IDX(p, W_NI));
        emit(CYC_A + 18, p, GPCP_OP_ST, 6, 0, 0, GPCP_SPAD_IDX(p, W_MAG));

        /* ---- stage D: 16-QAM max-log soft demapper ----
         * Works on the matched-filter output (Nr,Ni) and the channel gain
         * |H|^2, so the decision metric M(s) = |H|^2 s^2 - 2 Re{y conj(H)} s
         * needs no division.  PAM-4 Gray label on each axis:
         *   a=-3:"10"  a=-1:"11"  a=+1:"01"  a=+3:"00"
         * LLR(bit) = min_{b=1} M - min_{b=0} M, normalised by >> LLR_SHIFT. */
        {
            const uint32_t b = CYC_A + CYC_B;
            for (uint32_t axis = 0; axis < 2; axis++) {
                const uint32_t base = b + axis * 20;
                const uint32_t rw   = axis ? W_NI : W_NR;
                const uint32_t l1   = axis ? W_LLR_Q1 : W_LLR_I1;
                const uint32_t l0   = axis ? W_LLR_Q0 : W_LLR_I0;

                emit(base + 0,  p, GPCP_OP_LD, 0, 0, 0, GPCP_SPAD_IDX(p, rw));   /* r  */
                emit(base + 1,  p, GPCP_OP_LD, 0, 0, 1, GPCP_SPAD_IDX(p, W_MAG));/* m  */
                emit(base + 2,  p, GPCP_OP_SHL, 0, 0, 2, 1);                      /* 2r */
                emit(base + 3,  p, GPCP_OP_MUL, 2, GPCP_SRC_IMM, 3, 3);          /* 6r */
                emit(base + 4,  p, GPCP_OP_SHL, 1, 0, 0, 3);                      /* 8m */
                emit(base + 5,  p, GPCP_OP_ADD, 0, 1, 4, 0);                      /* 9m */
                emit(base + 6,  p, GPCP_OP_SUB, 1, 2, 5, 0);                      /* M(+1) */
                emit(base + 7,  p, GPCP_OP_ADD, 1, 2, 6, 0);                      /* M(-1) */
                emit(base + 8,  p, GPCP_OP_SUB, 4, 3, 0, 0);                      /* M(+3) */
                emit(base + 9,  p, GPCP_OP_ADD, 4, 3, 7, 0);                      /* M(-3) */
                /* MSB: 1 for the negative half */
                emit(base + 10, p, GPCP_OP_MIN, 6, 7, 1, 0);                      /* min(M-1,M-3) */
                emit(base + 11, p, GPCP_OP_MIN, 5, 0, 2, 0);                      /* min(M+1,M+3) */
                emit(base + 12, p, GPCP_OP_SUB, 1, 2, 3, 0);
                emit(base + 13, p, GPCP_OP_SHR, 3, 0, 4, LLR_SHIFT);
                emit(base + 14, p, GPCP_OP_ST, 4, 0, 0, GPCP_SPAD_IDX(p, l1));
                /* LSB: 1 for {-3,+1} */
                emit(base + 15, p, GPCP_OP_MIN, 7, 5, 1, 0);                      /* min(M-3,M+1) */
                emit(base + 16, p, GPCP_OP_MIN, 6, 0, 2, 0);                      /* min(M-1,M+3) */
                emit(base + 17, p, GPCP_OP_SUB, 1, 2, 3, 0);
                emit(base + 18, p, GPCP_OP_SHR, 3, 0, 4, LLR_SHIFT);
                emit(base + 19, p, GPCP_OP_ST, 4, 0, 0, GPCP_SPAD_IDX(p, l0));
            }
        }

        /* ---- stage C: min-sum check node (ldpc_cn) ---- */
        {
            const uint32_t b = CYC_A + CYC_B + CYC_D;
            /* the four soft bits of the 16-QAM symbol are the four messages */
            emit(b + 0, p, GPCP_OP_LD, 0, 0, 0, GPCP_SPAD_IDX(p, W_LLR_I1));
            emit(b + 1, p, GPCP_OP_LD, 0, 0, 1, GPCP_SPAD_IDX(p, W_LLR_I0));
            emit(b + 2, p, GPCP_OP_LD, 0, 0, 2, GPCP_SPAD_IDX(p, W_LLR_Q1));
            emit(b + 3, p, GPCP_OP_LD, 0, 0, 3, GPCP_SPAD_IDX(p, W_LLR_Q0));

            /* abs(x) = (x ^ (x>>31)) - (x>>31), stored to word 8..11 */
            uint32_t cyc = b + 4;
            for (uint32_t k = 0; k < 4; k++) {
                emit(cyc + 0, p, GPCP_OP_SHR, k, 0, 4, 31);
                emit(cyc + 1, p, GPCP_OP_XOR, k, 4, 5, 0);
                emit(cyc + 2, p, GPCP_OP_SUB, 5, 4, 5, 0);
                emit(cyc + 3, p, GPCP_OP_ST, 5, 0, 0, GPCP_SPAD_IDX(p, 8 + k));
                cyc += 4;
            }

            /* sign product of the four messages -> word 6 */
            emit(b + 20, p, GPCP_OP_SHR, 0, 0, 6, 31);
            emit(b + 21, p, GPCP_OP_SHR, 1, 0, 7, 31);
            emit(b + 22, p, GPCP_OP_XOR, 6, 7, 6, 0);
            emit(b + 23, p, GPCP_OP_SHR, 2, 0, 7, 31);
            emit(b + 24, p, GPCP_OP_XOR, 6, 7, 6, 0);
            emit(b + 25, p, GPCP_OP_SHR, 3, 0, 7, 31);
            emit(b + 26, p, GPCP_OP_XOR, 6, 7, 6, 0);
            emit(b + 27, p, GPCP_OP_ST, 6, 0, 0, GPCP_SPAD_IDX(p, W_SIGN));

            /* pair min/max of |a|,|b| and |c|,|d| -> words 12..15 */
            emit(b + 28, p, GPCP_OP_LD, 0, 0, 0, GPCP_SPAD_IDX(p, 8));
            emit(b + 29, p, GPCP_OP_LD, 0, 0, 1, GPCP_SPAD_IDX(p, 9));
            emit(b + 30, p, GPCP_OP_LD, 0, 0, 2, GPCP_SPAD_IDX(p, 10));
            emit(b + 31, p, GPCP_OP_LD, 0, 0, 3, GPCP_SPAD_IDX(p, 11));
            emit(b + 32, p, GPCP_OP_MIN, 0, 1, 4, 0);
            emit(b + 33, p, GPCP_OP_MAX, 0, 1, 5, 0);
            emit(b + 34, p, GPCP_OP_ST, 4, 0, 0, GPCP_SPAD_IDX(p, 12));
            emit(b + 35, p, GPCP_OP_ST, 5, 0, 0, GPCP_SPAD_IDX(p, 13));
            emit(b + 36, p, GPCP_OP_MIN, 2, 3, 6, 0);
            emit(b + 37, p, GPCP_OP_MAX, 2, 3, 7, 0);
            emit(b + 38, p, GPCP_OP_ST, 6, 0, 0, GPCP_SPAD_IDX(p, 14));
            emit(b + 39, p, GPCP_OP_ST, 7, 0, 0, GPCP_SPAD_IDX(p, 15));

            /* min1 = min(s1,s2); min2 = min(max(s1,s2), min(h1,h2)) */
            emit(b + 40, p, GPCP_OP_LD, 0, 0, 0, GPCP_SPAD_IDX(p, 12));
            emit(b + 41, p, GPCP_OP_LD, 0, 0, 1, GPCP_SPAD_IDX(p, 14));
            emit(b + 42, p, GPCP_OP_MIN, 0, 1, 4, 0);
            emit(b + 43, p, GPCP_OP_ST, 4, 0, 0, GPCP_SPAD_IDX(p, W_HR));
            emit(b + 44, p, GPCP_OP_MAX, 0, 1, 5, 0);
            emit(b + 45, p, GPCP_OP_LD, 0, 0, 2, GPCP_SPAD_IDX(p, 13));
            emit(b + 46, p, GPCP_OP_LD, 0, 0, 3, GPCP_SPAD_IDX(p, 15));
            emit(b + 47, p, GPCP_OP_MIN, 2, 3, 6, 0);
            emit(b + 48, p, GPCP_OP_MIN, 5, 6, 7, 0);
            emit(b + 49, p, GPCP_OP_ST, 7, 0, 0, GPCP_SPAD_IDX(p, W_HI));
        }
    }
}

static void load_chain(void)
{
    for (uint32_t i = 0; i < CHAIN_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, chain_prog[i]);
}

/* ----------------------------------------------------------------- payload */
static int32_t pseudo(int seed, int mod)
{
    return (seed % mod) - (mod / 2);
}

static void fill_slot(int s)
{
    for (int i = 0; i < 16; i++) {
        int32_t yr = pseudo(s * 97 + i * 37 + 13, 8001);      /* +-4000 */
        int32_t yi = pseudo(s * 53 + i * 29 + 71, 8001);
        int32_t xr = ((s + i) & 1) ? 23170 : -23170;          /* Q15 pilot */
        int32_t xi = ((s + i) & 2) ? 23170 : -23170;
        in[s][i * 4 + 0] = yr;
        in[s][i * 4 + 1] = yi;
        in[s][i * 4 + 2] = xr;
        in[s][i * 4 + 3] = xi;
    }
    for (int i = 0; i < WORDS_OUT; i++)
        out[s][i] = 0;
}

static int32_t sx16_s(int32_t v)
{
    v &= 0xFFFF;
    return (v & 0x8000) ? v - 0x10000 : v;
}

static uint32_t self_check(void)
{
    uint32_t bad = 0;
    for (int s = 0; s < SLOTS; s++) {
        for (int p = 0; p < 16; p++) {
            int32_t yr = sx16_s(in[s][p * 4 + 0]);
            int32_t yi = sx16_s(in[s][p * 4 + 1]);
            int32_t xr = sx16_s(in[s][p * 4 + 2]);
            int32_t xi = sx16_s(in[s][p * 4 + 3]);
            int32_t hr = (yr * xr + yi * xi) >> 15;
            int32_t hi = (yi * xr - yr * xi) >> 15;
            int32_t nr = (hr * yr + hi * yi) >> 15;
            int32_t ni = (hi * yr - hr * yi) >> 15;
            int32_t mag = (hr * hr + hi * hi) >> 15;

            /* 16-QAM max-log demapper, mirroring the microcode exactly */
            int32_t msgs[4];
            for (int axis = 0; axis < 2; axis++) {
                int32_t r = axis ? ni : nr;
                int32_t m = mag;
                int32_t two_r = (int32_t)((uint32_t)r << 1);
                int32_t six_r = (int32_t)((uint32_t)sx16_s(two_r) * 3u);
                int32_t m8 = (int32_t)((uint32_t)m << 3);
                int32_t m9 = m8 + m;
                int32_t mp1 = m - two_r, mm1 = m + two_r;   /* M(+1), M(-1) */
                int32_t mp3 = m9 - six_r, mm3 = m9 + six_r; /* M(+3), M(-3) */
                int32_t min_pos = mp1 < mp3 ? mp1 : mp3;
                int32_t min_neg = mm1 < mm3 ? mm1 : mm3;
                int32_t min_lsb1 = mm3 < mp1 ? mm3 : mp1;   /* min(M-3,M+1) */
                int32_t min_lsb0 = mm1 < mp3 ? mm1 : mp3;   /* min(M-1,M+3) */
                int32_t llr_msb = (int32_t)(min_neg - min_pos) >> LLR_SHIFT;
                int32_t llr_lsb = (int32_t)(min_lsb1 - min_lsb0) >> LLR_SHIFT;
                msgs[axis * 2 + 0] = llr_msb;
                msgs[axis * 2 + 1] = llr_lsb;
            }

            uint32_t ab[4];
            int sign = 0;
            for (int k = 0; k < 4; k++) {
                int32_t v = msgs[k];
                int32_t sg = v >> 31;                 /* arithmetic */
                ab[k] = (uint32_t)((v ^ sg) - sg);
                sign ^= (uint32_t)((v >> 31) & 1);
            }
            uint32_t s1 = ab[0] < ab[1] ? ab[0] : ab[1];
            uint32_t h1 = ab[0] < ab[1] ? ab[1] : ab[0];
            uint32_t s2 = ab[2] < ab[3] ? ab[2] : ab[3];
            uint32_t h2 = ab[2] < ab[3] ? ab[3] : ab[2];
            uint32_t min1 = s1 < s2 ? s1 : s2;
            uint32_t lo = s1 < s2 ? s2 : s1;
            uint32_t hi2 = h1 < h2 ? h1 : h2;
            uint32_t min2 = lo < hi2 ? lo : hi2;
            int32_t sgv = sign ? -1 : 0;

            if (out[s][p * 3 + 0] != (int32_t)min1) bad++;
            if (out[s][p * 3 + 1] != (int32_t)min2) bad++;
            if (out[s][p * 3 + 2] != sgv) bad++;
        }
    }
    return bad;
}

/* --------------------------------------------------------------- descriptors */
static void desc_set(int idx, uintptr_t src, uint32_t dst, uint32_t len,
                     uint32_t page, uint32_t out_dir, uint32_t stride, uint32_t row)
{
    desc[idx][0] = (uint32_t)src;
    desc[idx][1] = (dst & 0xFFFFu) | (page ? GPCP_DMA_DESC_PAGE1 : 0u) |
                   (out_dir ? GPCP_DMA_DESC_OUT : 0u) | GPCP_DMA_DESC_IRQ;
    desc[idx][2] = len & 0xFFFFu;
    desc[idx][3] = (stride & 0xFFFFu) | (row << 16);
    desc[idx][4] = 0;
    for (int i = 5; i < 8; i++)
        desc[idx][i] = 0;
}

/* --------------------------------------------------------------------- main */
int main(void)
{
    uint32_t *res = RESULT_BASE;

    for (int s = 0; s < SLOTS; s++)
        fill_slot(s);

    build_chain();
    load_chain();

    /* ring: IN 4 words/bank, OUT 3 words/bank (min1,min2,sign) */
    desc_set(0, (uintptr_t)&in[0][0], 0, WORDS_IN, 0, 0, 32, 4);
    desc_set(1, (uintptr_t)&in[1][0], 0, WORDS_IN, 1, 0, 32, 4);
    desc_set(2, (uintptr_t)&out[0][0], 4, WORDS_OUT, 0, 1, 32, 3);
    desc_set(3, (uintptr_t)&in[2][0], 0, WORDS_IN, 0, 0, 32, 4);
    desc_set(4, (uintptr_t)&out[1][0], 4, WORDS_OUT, 1, 1, 32, 3);
    desc_set(5, (uintptr_t)&in[3][0], 0, WORDS_IN, 1, 0, 32, 4);
    desc_set(6, (uintptr_t)&out[2][0], 4, WORDS_OUT, 0, 1, 32, 3);
    desc_set(7, (uintptr_t)&out[3][0], 4, WORDS_OUT, 1, 1, 32, 3);

    plic_setup();
    gpcp_cgra_irq_enable(CGRA_BASE, GPCP_CGRA_IRQ_ARR | GPCP_CGRA_IRQ_DMA);
    irq_unmask();

    uint32_t t0 = mmio_r(CLINT_MTIME);

    /* slot 0 and 1 are prefetched, then the chain alternates pages */
    gpcp_cgra_ring_start(CGRA_BASE, (uintptr_t)&desc[0][0], 16, 2);
    wait_ring(2);
    array_run(0);                        /* slot 0 on page 0 */
    gpcp_cgra_ring_push(CGRA_BASE, 4);    /* OUT0 (page 0) + IN2 (page 0) */
    array_run(1);                        /* slot 1 on page 1, engine drains p0 */
    wait_ring(4);
    gpcp_cgra_ring_push(CGRA_BASE, 6);    /* OUT1 (page 1) + IN3 (page 1) */
    array_run(0);                        /* slot 2 on page 0, engine drains p1 */
    wait_ring(6);
    gpcp_cgra_ring_push(CGRA_BASE, 7);    /* OUT2 (page 0) */
    array_run(1);                        /* slot 3 on page 1, engine drains p0 */
    gpcp_cgra_ring_push(CGRA_BASE, 8);    /* OUT3 (page 1) */
    wait_ring(8);
    uint32_t t1 = mmio_r(CLINT_MTIME);    /* chain end, before the export */

    gpcp_cgra_irq_enable(CGRA_BASE, 0);
    mmio_w(PLIC_ENABLE(0), 0);
    __asm__ __volatile__("csrci mstatus, 8");

    for (int s = 0; s < SLOTS; s++)
        for (int i = 0; i < WORDS_OUT; i++)
            res[s * WORDS_OUT + i] = (uint32_t)out[s][i];

    res[192] = 0xCAFEBABEu;
    res[193] = irq_count;
    res[194] = bad_traps;
    res[195] = errs + self_check();
    res[196] = gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_RING_DONE);
    res[197] = t1 - t0;
    res[198] = 0x600Du;
    res[199] = t_array;      /* array waiting (4 run of 87 cycles) */
    res[200] = t_ring;       /* descriptor waiting */

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
