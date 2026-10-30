/* GPCP B3: user-space CGRA test (Linux, /dev/mem + mmap)
 *
 * Runs the same 4x4 matrix multiply as the bare-metal test, prints the
 * accumulators, then writes the simulation exit register.
 */

#include <stdio.h>
#include <stdint.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>

#define CGRA_PHYS   0x50000000UL
#define CGRA_SIZE   0x10000UL
#define EXIT_PHYS   0x40000000UL   /* write at +0x10000 */
#define REG_CTRL    0x0000
#define REG_NUM     0x0004
#define REG_STATUS  0x0000
#define REG_ACC     0x1200
#define REG_PE      0x1000
#define REG_IMEM    0x2000

static const int32_t A[4][4] = {
    {1, 2, 3, 4},
    {5, 6, 7, 8},
    {9, 1, 2, 3},
    {4, 5, 6, 7},
};

static const int32_t B[4][4] = {
    {1, 0, -1, 2},
    {3, 2, 1, 0},
    {-2, 1, 4, -3},
    {0, 5, 2, 1},
};

static volatile uint32_t *devmem_map(int fd, unsigned long phys, unsigned long size)
{
    void *p = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, (off_t)phys);
    if (p == MAP_FAILED) {
        perror("mmap");
        return NULL;
    }
    return (volatile uint32_t *)p;
}

int main(void)
{
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) {
        perror("open /dev/mem");
        return 1;
    }

    volatile uint32_t *cgra = devmem_map(fd, CGRA_PHYS, CGRA_SIZE);
    volatile uint32_t *exitr = devmem_map(fd, EXIT_PHYS, 0x20000);
    if (!cgra || !exitr)
        return 1;

    /* microcode: 4 cycles, MAC rk, r(4+k) */
    for (uint32_t k = 0; k < 4; k++)
        for (uint32_t pe = 0; pe < 16; pe++)
            cgra[(REG_IMEM + (k * 16 + pe) * 4) / 4] = (5u << 27) | (k << 23) | ((4 + k) << 19);

    for (uint32_t pe = 0; pe < 16; pe++) {
        uint32_t row = pe / 4, col = pe % 4;
        for (uint32_t k = 0; k < 4; k++) {
            cgra[(REG_PE + pe * 32 + k * 4) / 4] = (uint32_t)A[row][k];
            cgra[(REG_PE + pe * 32 + (4 + k) * 4) / 4] = (uint32_t)B[k][col];
        }
    }

    cgra[REG_NUM / 4] = 4;
    cgra[REG_CTRL / 4] = 1;

    uint32_t guard = 0;
    while (!(cgra[REG_STATUS / 4] & 2) && guard < 1000000u)
        guard++;

    printf("CGRA matmul (C = A x B):\n");
    for (uint32_t pe = 0; pe < 16; pe++) {
        int32_t v = (int32_t)cgra[(REG_ACC + pe * 4) / 4];
        printf("  C[%u][%u] = %d\n", pe / 4, pe % 4, v);
    }
    printf("CGRA poll guard = %u\n", guard);

    printf("=== CGRA TEST DONE ===\n");

    /* ask the simulation to stop */
    exitr[0x10000 / 4] = 0x600D;

    return 0;
}
