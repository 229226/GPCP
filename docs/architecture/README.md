# 硬件架构与平台

## 1. 系统组成

```
            ┌──────────────────────────────┐
            │  CVA6 (RV64GC)               │
            │  I$ / D$ / MMU / FPU         │
            └──────────┬───────────────────┘
                       │ AXI4 (CPU 口)
            ┌──────────┴───────────────────┐
            │  SoC 互连 + 地址译码          │
            ├──────────┬──────────┬────────┤
            │ RAM      │ CGRA     │ 外设    │
            │ 64 MiB   │ 0x5000…  │ PLIC   │
            │          │          │ CLINT  │
            └──────────┴────┬─────┴────────┘
                            │ AXI4 (CGRA 主口)
                    ┌───────┴────────┐
                    │ 拷贝引擎 + 描述符环 │
                    │ 4×4 CGRA 阵列      │
                    └────────────────┘
```

- CPU 与 CGRA 的 DMA 共用一个 AXI 主口，SoC 逐拍仲裁；DMA 只在 CPU 当拍不发
  存储器请求时获得总线。
- 阵列与 DMA 可并行：阵列读 spad 的一页，DMA 填/排另一页（双页 ping-pong）。
- 中断：阵列完成、DMA 完成各一路，经 PLIC 源 1 接到 CPU 机器外部中断。

## 2. 地址空间

| 区域 | 基址 | 大小/说明 |
|---|---|---|
| RAM | `0x8000_0000` | 64 MiB（Linux 映射）；裸机链接脚本只用前 512 KiB |
| CLINT | `0x0200_0000` | 机器定时器/软件中断，slot 时基用它 |
| PLIC | `0x0C00_0000` | 4 MiB，claim/complete 在 `0x0C20_0000` |
| UART | `0x1000_0000` | 16550 |
| 仿真出口 | `0x4001_0000` | 写该地址结束仿真（`EXIT_ADDR`） |
| CGRA | `0x5000_0000` | AXI4-Lite 主机窗口，见 §3 |

裸机程序的内存布局由 `sw/soc/link.ld` 给出：

- `.text/.rodata/.data/.bss` 从 `0x8000_0000` 起，栈顶 `0x8008_0000`；
- 描述符环基址 `_ring_base = MAX(ALIGN(4096)+4096, 0x8002_0000)`，随 `.bss`
  自动上移；环窗口 32 KiB，链接期 `ASSERT` 保证不越过 `0x8004_0000`；
- 大结果区（如 ifft/ssb_tx）放在 `0x8004_0000` 以上，避免与环重叠。

## 3. CGRA 主机接口（MMIO）

| 偏移 | 名称 | 作用 |
|---|---|---|
| `0x0000` | CTRL / STATUS | 写 `[0]` 启动；读 `[0]` busy、`[1]` done、`[31:16]` 周期 |
| `0x0004` | NUM_CYCLES | 写 `[31]` 启动、`[16]` 页、`[15:0]` 拍数（一次 store 完成） |
| `0x0010`–`0x001C` | DMA | src/dst/len/ctrl（寄存器发起的单次搬运） |
| `0x0020`–`0x002C` | 页/2D | 阵列页、DMA 页、stride、row |
| `0x0040`/`0x0044` | IRQ | 状态（W1C）与使能：bit0 阵列、bit1 DMA |
| `0x0048`–`0x005C` | 描述符环 | base/size/head/tail/ctrl/done |
| `0x0064` | DESC_MODE | 0 = 32 B 描述符，1 = 64 B 复合描述符 |
| `0x006C` | ROT_ZC | 每 bank 旋转表的循环模（0 视为 32） |
| `0x0070` | WIN_STATUS | 输出写窗口越界（sticky，W1C） |
| `0x0074` | IMEM_BANK | `[0]` 主机/拷贝引擎写 IMEM 的 bank（0/1）；读回当前写 bank |
| `0x0080` | ROT_BASE | `0x80 + bank*4`，每 bank 7 bit 旋转表项 |
| `0x1000` | PE 寄存器 | `0x1000 + pe*32 + reg*4` |
| `0x2000` | IMEM | `0x2000 + idx*4`，`idx = cycle*16 + pe` |
| `0xA000` | spad 窗口 | `0xA000 + bank*128 + word*4` |

启动阵列的写法是「一次 32-bit store 到 `NUM_CYCLES`」：`[31]=1`、`[16]=页`、
`[15:0]=拍数`。拆成两次 store 会被 CVA6 的写缓冲合并/丢弃，必须避免。

## 4. 平台状态

- B2 裸机：`make -C sim/soc run`，CVA6 → CGRA matmul 与黄金模型一致。
- L1 数据面：DMA 双页、描述符环、完成中断均已在 SoC 上验证。
- B3 Linux：OpenSBI v1.9 + Linux 6.6 已进入内核虚拟地址空间，受 Verilator
  速度限制尚未到 initramfs/用户态。
- 环境要求：所有 Verilator/make 调用前清 `LD_LIBRARY_PATH`（工具链自带旧
  libstdc++），仓库 Makefile 已用 `env -u` 处理。

## 5. 相关入口

- CGRA 微架构、ISA、描述符格式：[cgra.md](cgra.md)
- 软件驱动方式：[../software/README.md](../software/README.md)
- 门禁与验收：[../verification/README.md](../verification/README.md)
