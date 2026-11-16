# 软件栈与编程模型

## 1. 头文件分层

| 头文件 | 作用 |
|---|---|
| `sw/include/soc_io.h` | `mmio_r/mmio_w`、基本 IO |
| `sw/include/gpcp_cgra.h` | CGRA 软件 ABI：MMIO 偏移、位定义、PE/IMEM/spad 窗口 |
| `sw/include/gpcp_cgra_rt.h` | 运行时：`rt_array_run`、`rt_dma_begin/wait`、`rt_dcache_flush` |
| `sw/include/gpcp_desc64.h` | 64 B 描述符字段访问器（YAML 生成） |
| `sw/include/gpcp_ring.h` | 描述符环基址 `GPCP_DESC_BASE`（链接期 `_ring_base`） |
| `sw/include/perf.h` | 性能计数器读取（可选 `GPCP_CPU_PROFILE`） |
| `sw/soc/gen/*_layout.h` | 生成的 kernel 布局契约（区名 → bank/word） |

## 2. 程序骨架

一个典型的裸机程序：

```c
#include "../include/gpcp_cgra.h"
#include "../include/soc_io.h"
#include "../include/gpcp_cgra_rt.h"
#include "../include/gpcp_desc64.h"
#include "../include/gpcp_ring.h"
#include "my_kernel_prog.h"

int main(void) {
    uint32_t *d = (uint32_t *)_ring_base;

    gpcp_cgra_imem_load(CGRA_BASE, my_kernel_prog, MY_KERNEL_CYCLES * 16);

    /* 1. 用 gpcp_d64_set_* 组描述符（32 B 只用前 4 个字） */
    memset(d, 0, 64);
    gpcp_d64_set_src(d, src_addr);
    gpcp_d64_set_spad(d, 0);
    gpcp_d64_set_in_len(d, words);
    gpcp_d64_set_cycles(d, MY_KERNEL_CYCLES);
    gpcp_d64_set_arr_en(d, 1);
    gpcp_d64_set_out_len(d, words);
    gpcp_d64_set_out_spad(d, 0);
    gpcp_d64_set_out_en(d, 1);
    gpcp_d64_set_out_dst(d, dst_addr);

    /* 2. 写回 D$，再提交环 */
    rt_dcache_flush();
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_DESC_MODE, 1);   /* 1 = 64 B */
    gpcp_cgra_ring_start(CGRA_BASE, (uintptr_t)d, 1, 1);
    while (gpcp_cgra_read(CGRA_BASE, GPCP_CGRA_RING_DONE) != 1) { }
    gpcp_cgra_write(CGRA_BASE, GPCP_CGRA_RING_CTRL, 0);
}
```

## 3. 内存布局

- 链接脚本：`sw/soc/link.ld`，RAM 前 512 KiB，栈顶 `0x8008_0000`。
- 描述符环：`_ring_base`，由链接器放在 `.bss` 之后并 4 KiB 对齐，下限
  `0x8002_0000`；窗口 32 KiB（1024 × 32 B）。
- 固定结果区/工作区：如 `0x8004_0000`（大结果区）、`0x8010_0000`（twiddle）、
  `0x8040_0000`（协议激励）。新程序必须保证这些区间不与 `.bss`/环重叠；
  链接期有 `ASSERT` 兜底。
- `RESULT_BASE` 是 host/target ABI：仿真台架用 `--results <addr> --nwords N`
  读回，地址必须与程序约定一致。

## 4. 描述符与环

### 构造

- 64 B 描述符 16 个字；32 B 描述符是它的前 4 个字。两者字段布局一致。
- 字段定义只在 `rtl/cgra/cgra_desc64.yml`；软件只用 `gpcp_d64_set_*`。
- 32 B 描述符适合「搬入 → 可选运算 → 搬出」的简单流水；64 B 复合描述符
  可以在硬件里自动完成 `in → array → out → repeat`，中间轮次不回写状态字。

### 提交

```c
rt_dcache_flush();                 /* CPU 写的描述符必须落到 RAM */
gpcp_cgra_write(base, GPCP_CGRA_RING_BASE, DESC_BASE);
gpcp_cgra_write(base, GPCP_CGRA_RING_SIZE, RING_DEPTH);
gpcp_cgra_write(base, GPCP_CGRA_RING_HEAD, 0);
gpcp_cgra_write(base, GPCP_CGRA_RING_TAIL, n);
fence iorw, iorw;
gpcp_cgra_write(base, GPCP_CGRA_RING_CTRL, GPCP_CGRA_RING_EN);
```

轮询 `RING_DONE`（完成计数）或等待中断。收尾时写 `RING_CTRL=0`。

### DMA 规则

- 每次突发 ≤ 32 beat（64 字），不跨 4 KiB；奇数长度末拍 `wstrb=0x0F`。
- 2D 搬运用 `stride/row`，`row=0` 表示线性。
- 搬出窗口外的写会被硬件丢弃并置 `WIN_STATUS[0]`；台架 `--watch-writes`
  会在仿真后逐字检查窗口外不变。

## 5. 双页运行时

`gpcp_cgra_rt.h` 提供最小骨架：

```c
rt_dma_begin(base, src0, 0, n, 0, 0, stride, row);   // 预取第 0 行到 page 0
rt_dma_wait(base);
for (r = 0; r < rows; r++) {
    if (r + 1 < rows)
        rt_dma_begin(base, src(r+1), 0, n, (r+1)&1, 0, stride, row);
    rt_array_run(base, r & 1, cycles);               // 与预取并行
    if (r + 1 < rows) rt_dma_wait(base);
    rt_dma_begin(base, dst(r), out_spad, m, r & 1, 1, stride, row);
    rt_dma_wait(base);
}
```

约束：

- 阵列用一页（`BUF_PAGE`），拷贝引擎同时只能操作另一页（`DMA_PAGE`）。
- 阵列是静态调度，`cycles` 由生成器给出，跑完自己停。
- 启动是 posted write：先看到 `BUSY` 或 `DONE`，再等 `BUSY` 清零。
- 拷贝引擎单 outstanding，但可以与阵列并行。

## 6. 常见陷阱

| 现象 | 原因 | 处理 |
|---|---|---|
| 程序结果全 0 / 旧值 | `.inc`/头文件改动未触发重编，跑的是旧二进制 | `make -C sw/soc clean` 或 `-j8 all` |
| 引擎写完 CPU 读到旧数据 | CVA6 D$ 是 WT，引擎直写 RAM 不会失效缓存 | 读结果前 `rt_dcache_flush()` |
| 描述符被优化成全 0 | 用 `uint64_t*` 重写 32-bit 结构触发严格别名 UB | 只用 `gpcp_d64_set_*` 或 `may_alias` |
| 启动阵列偶发不生效 | 两次 MMIO store 被写缓冲合并/丢弃 | 用 `NUM_CYCLES` 一次 store 启动 |
| 环/结果区互相覆盖 | `.bss` 增长越过硬编码环基址 | 用 `_ring_base`，链接期 `ASSERT` 兜底 |
| 输出越界静默写坏内存 | 没有检查搬出窗口 | 查 `WIN_STATUS`，跑 `--watch-writes` |

## 7. 相关文档

- MMIO/地址空间：[../architecture/README.md](../architecture/README.md)
- 描述符字段与 ISA：[../architecture/cgra.md](../architecture/cgra.md)
- 门禁与调试：[../verification/README.md](../verification/README.md)
