/* -----------------------------------------------------------------------------
 * cgracc 硬件闭环：编译器生成的微码在真实 RTL 上跑一遍。
 *
 * 微码来自 cgracc 仓库（examples/imem_dot.json -> imem_dot_prog.h）：
 *   cycle 0  PEk: mul r0, r1 -> r2     (r0=3, r1=5 -> 15)
 *   cycle 1  PEk: mul r2, r1 -> r3     (15*5 -> 75)
 *   cycle 2  PEk: st  r3   -> spad[1]
 *   cycle 0  PEm: ld  spad[0] -> r4    （独立分支，验证多 PE 并行）
 * cgracc 的 `private_registers: true` 会把整条依赖链绑到同一颗 PE。
 *
 * 结果区（0x8001_0000，32-bit words）：
 *   [0] marker 0x0FF1C1A1  [1] IMEM 拍数  [2] STATUS
 *   [3] spad[bank0,word1]（期望 75）  [4] CYCLE_CNT  [5] 0x600D 结尾字
 * ----------------------------------------------------------------------------- */
#include <stdint.h>
#include "../../include/gpcp_cgra.h"
#include "cgracc_dot_prog.h"     /* cgracc 生成，勿手改 */

#define CGRA_BASE    0x50000000ul
#define EXIT_ADDR    0x40010000ul
#define RESULT_BASE  ((uint32_t *)0x80010000ul)
#define NPES         16u

int main(void)
{
    uint32_t *res = RESULT_BASE;
    uint32_t pre[NPES * 4];      /* 每 PE 两组 (idx, val)：r0=3, r1=5 */
    uint32_t i, status;

    for (i = 0; i < NPES; i++) {
        pre[4 * i + 0] = i * 8u + 0u;
        pre[4 * i + 1] = 3u;
        pre[4 * i + 2] = i * 8u + 1u;
        pre[4 * i + 3] = 5u;
    }

    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_CTRL, GPCP_CGRA_CTRL_CLRDONE);

    /* 编译器产物逐字写进 IMEM（不走 DMA，保证字节一一对应） */
    for (i = 0; i < IMEM_DOT_PROG_CYCLES * NPES; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_IMEM_BASE + i * 4u, imem_dot_prog[i]);

    for (i = 0; i < NPES * 2u; i++)
        gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_PE_BASE + pre[2 * i] * 4u, pre[2 * i + 1]);

    /* 一次 store 带上 cycles/page/start（裸机 ABI 的原子启动方式） */
    gpcp_cgra_array_start(CGRA_BASE, 0u, IMEM_DOT_PROG_CYCLES);
    while (!(gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_STATUS) & GPCP_CGRA_STATUS_DONE))
        ;

    status = gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_STATUS);
    res[0] = 0x0FF1C1A1u;
    res[1] = IMEM_DOT_PROG_CYCLES;
    res[2] = status;
    res[3] = gpcp_cgra_read(CGRA_BASE, GPCP_SPAD_ADDR(0, 1));   /* 期望 75 */
    res[4] = gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_CYCLE_CNT);
    res[5] = 0x600Du;

    __asm__ __volatile__("fence iorw, iorw" ::: "memory");
    *(volatile uint32_t *)EXIT_ADDR = 0x600Du;
    return 0;
}
