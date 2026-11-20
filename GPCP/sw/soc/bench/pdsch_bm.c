/* -----------------------------------------------------------------------------
 * PDSCH LDPC iteration loop on the CGRA (P2 step 2a)
 *
 * The 38.212 BG2 code block does not fit in the 8 KiB scratchpad (52*Zc LLRs
 * plus 197*Zc messages), so a full block has to be blocked over DMA.  Before
 * building that, this benchmark proves the *iteration machinery* on hardware:
 *
 *   variable nodes v0..v7  (one PE each, bank 8+v)
 *   check    nodes c0..c3  (one PE each, bank c)
 *   edges (8 variables, degree 2; 4 checks, degree 4)
 *       c0: v0 v1 v2 v3      c1: v4 v5 v6 v7
 *       c2: v0 v1 v6 v7      c3: v2 v3 v4 v5
 *
 * One iteration = one check-node run (52 cycles, min1/min2/sign, the same
 * kernel as `ldpc_cn`) followed by one variable-node run (12 cycles):
 *
 *   v = ch + m0 + m1 ;  m' = v - m ;  hard = sign(v)
 *
 * The CPU reconstructs the per-edge check messages (magnitude = min1 unless
 * that edge produced min1, then min2) and shuttles words between the two
 * stages.  The golden model of the whole loop is tools/checks/check_pdsch.py, which
 * runs the same integer min-sum and compares the hard decisions bit-exactly.
 *
 * The channel LLRs are the first 8 values of a fixed LCG stream (seed 1): they
 * are chosen so that the min-sum loop actually *flips* two decisions relative
 * to the raw channel signs, otherwise "decode == hard-limit" would be a weak
 * check of the iteration itself.
 *
 * Results at 0x8001_0000 (words):
 *   [0..7]  hard decisions after ITERS iterations
 *   [8] 0xCAFEBABE   [9] iterations   [10] array runs   [11] cycles
 *   [12] errors/timeouts   [13] 0x600D
 * ----------------------------------------------------------------------------- */
#include "../../include/gpcp_cgra.h"
#include "../../include/soc_io.h"
#include "cn4_prog.h"       /* 编译器生成的 4 消息校验节点 */
#include "vn3_prog.h"       /* 编译器生成的 3 消息变量节点 */

#define CGRA_BASE        0x50000000ul
#define EXIT_ADDR        0x40010000ul
#define RESULT_BASE      ((uint32_t *)0x80010000ul)

#define NPES             16
#define NVAR             8
#define NCHK             4
#define EDGES_PER_CHK    4
#define CYC_CN           CN4_PROG_CYCLES
#define CYC_BN           VN3_PROG_CYCLES
#define ITERS            8
#define LCG_SEED         1u

static inline uint32_t mtime(void) { return mmio_r(0x0200BFF8ul); }

#define SPAD(bank, word)  (CGRA_BASE + GPCP_SPAD_ADDR((bank), (word)))

/* edge tables: check -> 4 variables, and variable -> its two (check, edge) */
static const int chk_var[NCHK][EDGES_PER_CHK] = {
    {0, 1, 2, 3}, {4, 5, 6, 7}, {0, 1, 6, 7}, {2, 3, 4, 5}
};
/* for each variable: the two (check node, edge index) pairs it belongs to */
static const int var_edge[NVAR][2][2] = {
    {{0, 0}, {2, 0}}, {{0, 1}, {2, 1}}, {{0, 2}, {3, 0}}, {{0, 3}, {3, 1}},
    {{1, 0}, {3, 2}}, {{1, 1}, {3, 3}}, {{1, 2}, {2, 2}}, {{1, 3}, {2, 3}}
};


static int32_t  ch_llr[NVAR];
static int32_t  msg_cv[NCHK][EDGES_PER_CHK];    /* check -> variable */
static int32_t  hard[NVAR];
static uint32_t errs, runs;
static uint32_t t_cycle;

/* ---- check node: 4 messages in words 0..3 -> min1/min2/signin 8/9/10 ---- */
static void build_cn(void)
{
    for (uint32_t i = 0; i < CYC_CN * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, cn4_prog[i]);
}

/* ---- variable node: ch + m0 + m1 in words 0..2 -> words 3..6 ---- */
static void build_bn(void)
{
    for (uint32_t i = 0; i < CYC_BN * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, vn3_prog[i]);
}

static void load_prog(const uint32_t *p, uint32_t words)
{
    for (uint32_t i = 0; i < words; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, p[i]);
}

static void array_run(uint32_t cycles)
{
    gpcp_cgra_array_start(CGRA_BASE, 0, cycles);
    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    uint32_t spins = 0;
    while (mmio_r(CGRA_BASE + GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_BUSY) {
        if (++spins > 200000u) { errs++; break; }
    }
    runs++;
}

static inline int32_t spad_rd(uint32_t bank, uint32_t word)
{
    return (int32_t)mmio_r(SPAD(bank, word));
}
static inline void spad_wr(uint32_t bank, uint32_t word, int32_t v)
{
    mmio_w(SPAD(bank, word), (uint32_t)v);
}

/* --------------------------------------------------------------- payload */
static uint32_t lcg = LCG_SEED;
static int32_t next_llr(void)
{
    lcg = lcg * 1664525u + 1013904223u;
    return (int32_t)((lcg >> 16) & 0x3FFu) - 512;    /* [-512, 511] */
}

int main(void)
{
    uint32_t *res = RESULT_BASE;

    for (uint32_t v = 0; v < NVAR; v++)
        ch_llr[v] = next_llr();

    build_cn();
    build_bn();

    /* initial variable -> check messages are just the channel LLRs */
    for (uint32_t v = 0; v < NVAR; v++)
        spad_wr(8 + v, 0, ch_llr[v]);
    for (uint32_t c = 0; c < NCHK; c++)
        for (uint32_t k = 0; k < EDGES_PER_CHK; k++)
            msg_cv[c][k] = ch_llr[chk_var[c][k]];

    uint32_t t0 = mtime();
    for (uint32_t it = 0; it < ITERS; it++) {
        /* ---- check node run ---- */
        for (uint32_t c = 0; c < NCHK; c++)
            for (uint32_t k = 0; k < EDGES_PER_CHK; k++)
                spad_wr(c, k, msg_cv[c][k]);
        load_prog(cn4_prog, CYC_CN * NPES);
        array_run(CYC_CN);

        /* reconstruct the per-edge messages: magnitude is min1 unless the
         * edge produced min1, then min2; sign = sign(msg) * sign(others) */
        int32_t m1[NCHK], m2[NCHK], sg[NCHK];
        for (uint32_t c = 0; c < NCHK; c++) {
            m1[c] = spad_rd(c, 8);
            m2[c] = spad_rd(c, 9);
            sg[c] = spad_rd(c, 10);
        }
        for (uint32_t c = 0; c < NCHK; c++)
            for (uint32_t k = 0; k < EDGES_PER_CHK; k++) {
                int32_t a = msg_cv[c][k];
                int32_t mag = (a < 0 ? -a : a);
                int32_t out_mag = (mag == m1[c]) ? m2[c] : m1[c];
                int32_t neg = (a < 0) ^ (sg[c] != 0);
                msg_cv[c][k] = neg ? -out_mag : out_mag;
            }

        /* ---- variable node run ---- */
        for (uint32_t v = 0; v < NVAR; v++) {
            spad_wr(8 + v, 0, ch_llr[v]);
            spad_wr(8 + v, 1, msg_cv[var_edge[v][0][0]][var_edge[v][0][1]]);
            spad_wr(8 + v, 2, msg_cv[var_edge[v][1][0]][var_edge[v][1][1]]);
        }
        load_prog(vn3_prog, CYC_BN * NPES);
        array_run(CYC_BN);

        /* next check inputs come from the variable-node outputs */
        for (uint32_t v = 0; v < NVAR; v++) {
            hard[v] = spad_rd(8 + v, 6);
            msg_cv[var_edge[v][0][0]][var_edge[v][0][1]] = spad_rd(8 + v, 4);
            msg_cv[var_edge[v][1][0]][var_edge[v][1][1]] = spad_rd(8 + v, 5);
        }
    }
    t_cycle = mtime() - t0;

    for (uint32_t v = 0; v < NVAR; v++)
        res[v] = (uint32_t)hard[v];

    res[8] = 0xCAFEBABEu;
    res[9] = ITERS;
    res[10] = runs;
    res[11] = t_cycle;
    res[12] = errs;
    res[13] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
