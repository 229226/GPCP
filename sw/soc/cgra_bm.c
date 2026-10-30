/* GPCP SoC - bare-metal CGRA test
 *
 * Runs a 4x4 integer matrix multiply on the CGRA from the CVA6 core:
 *   - preloads PE registers (A row i in r0..r3, B column j in r4..r7)
 *   - writes 4 microcode cycles (MAC rk, r(4+k))
 *   - polls the done bit
 *   - writes the 16 accumulators + a marker into RAM for the testbench
 */

typedef unsigned int   u32;
typedef unsigned long  u64;

#define CGRA_BASE    0x50000000UL
#define REG_CTRL     0x0000u
#define REG_NUM      0x0004u
#define REG_STATUS   0x0000u
#define REG_ACC      0x1200u
#define REG_PE       0x1000u
#define REG_IMEM     0x2000u

#define STATUS_DONE  (1u << 1)

#define RESULT_BASE  0x80002000UL
#define EXIT_ADDR    0x40010000UL
#define EXIT_MAGIC   0x600Du

#define OP_MAC 5u

static inline void mmio_w(u64 addr, u32 v) { *(volatile u32 *)addr = v; }
static inline u32  mmio_r(u64 addr)        { return *(volatile u32 *)addr; }

static u32 inst_mac(u32 a, u32 b)
{
    return (OP_MAC << 27) | (a << 23) | (b << 19);
}

static const int A[4][4] = {
    { 1,  2,  3,  4},
    { 5,  6,  7,  8},
    { 9,  1,  2,  3},
    { 4,  5,  6,  7},
};

static const int B[4][4] = {
    { 1,  0, -1,  2},
    { 3,  2,  1,  0},
    {-2,  1,  4, -3},
    { 0,  5,  2,  1},
};

int main(void)
{
    u32 pe, k, i;
    u64 base = CGRA_BASE;
    volatile u32 *results = (volatile u32 *)RESULT_BASE;

    /* microcode: 4 cycles, all PEs accumulate A row * B column */
    for (k = 0; k < 4; k++)
        for (pe = 0; pe < 16; pe++)
            mmio_w(base + REG_IMEM + (k * 16 + pe) * 4, inst_mac(k, 4 + k));

    /* preload */
    for (pe = 0; pe < 16; pe++) {
        u32 row = pe / 4, col = pe % 4;
        for (k = 0; k < 4; k++)
            mmio_w(base + REG_PE + pe * 32 + k * 4, (u32)(A[row][k] & 0xFFFF));
        for (k = 0; k < 4; k++)
            mmio_w(base + REG_PE + pe * 32 + (4 + k) * 4, (u32)(B[k][col] & 0xFFFF));
    }

    mmio_w(base + REG_NUM, 4);
    mmio_w(base + REG_CTRL, 1);

    /* poll done (bounded to keep a broken run from hanging forever) */
    u32 guard = 0;
    while (!(mmio_r(base + REG_STATUS) & STATUS_DONE) && guard < 100000u)
        guard++;

    for (pe = 0; pe < 16; pe++)
        results[pe] = mmio_r(base + REG_ACC + pe * 4);

    results[16] = 0xCAFEBABEu;
    results[17] = guard;
    results[18] = 0x600Du;

    for (i = 19; i < 24; i++)
        results[i] = 0;

    mmio_w(EXIT_ADDR, EXIT_MAGIC);
    return 0;
}
