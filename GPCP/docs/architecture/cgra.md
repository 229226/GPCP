# CGRA 微架构与 ISA

## 1. 几何

| 项 | 值 | 来源 |
|---|---|---|
| 阵列 | 4×4 = 16 PE | `cgra/target.yaml` |
| 每 PE 寄存器 | 8 个（r0–r7） | 同上 |
| 数据宽度 | 16-bit 乘、32-bit 累加 | `rtl/cgra/cgra_pe.sv` |
| link | 最近邻四方向，1 周期 | `rtl/cgra/cgra_array.sv` |
| IMEM | 2 bank × 8192 字（每 bank 512 拍 × 16 PE） | `GPCP_CGRA_IMEM_WORDS`、`REG_IMEM_BANK` |
| 单次运行上限 | 512 拍 | `GPCP_CGRA_MAX_CYCLES` |
| spad | 32 逻辑 bank × 32 word × 2 页（物理 64 × 32×32，1R1W） | `target.yaml` |
| 时钟 | 100 MHz（SoC 目标） | `target.yaml: clock_hz` |

## 2. spad

- 8 KiB = 64 个物理 bank（2 页 × 32）× 32 word × 32 bit；每个物理 bank 是
  一条 1R1W SRAM（一拍一读一写）。
- 逻辑索引 `flat = bank*32 + word`（10 位，`imm[9:0]`）不变；物理映射为
  `phys_bank = {page, word ^ M(bank)}`、`phys_word = bank`（转置 +
  GF(2) 散列）。`M` 是 5×5 可逆线性映射（5 个异或项），把逻辑 bank 的 5 个位
  混合后异或到物理 bank：转置把“同一逻辑 bank 的 32 个字”摊到 32 个物理
  bank，`M` 再把跨 bank 的固定步长访问打散。`M` 是按 FFT / PDSCH-full /
  `pss_corr` / `sss_corr` 的真实访问序列求解的，四类微码在该映射下每拍
  每物理 bank 的读/写都 ≤1 个不同字。
- 16 个 PE 通过 16→32 地址/数据交叉开关访问当前页的 32 个 bank；每个 PE
  只有一个 64:1 读数据 mux（两页各 32:1 + 页选择）。同字读共享读口（广播）。
- 静态调度必须保证每拍每物理 bank ≤1 个不同读字、≤1 个写；仿真断言
  （`$error`）检查，硬件不暂停。
- 每 bank 一张 13-bit 循环旋转表：`[4:0]` 读旋转、`[5]` invalid（读回 0）、
  `[6]` keep（抑制写）、`[12:8]` 写旋转。由 `ROT_BASE + bank*4` 写入，
  `ROT_ZC` 设置循环模；`LD/ST` 的 `imm[15]` 决定该次访问是否参与旋转。
  读写可不同：PDSCH 的 CN 用 `sh` 做 m' 预移位（LD/ST 都带 rot），BN 读写
  都用 0；这样两个核都不需要同拍多端口。默认全 0，行为与无旋转表时一致。
- 主机窗口 `0xA000 + bank*128 + word*4` 不变；CPU 读写走同一映射。
- 两页由 `page` 位选择：阵列用一页时，拷贝引擎操作另一页。DMA 的 64-bit
  beat 两个连续字经转置落到不同物理 bank，所以 2 字/拍不变。
- 综合口径：8 KiB 用 64 个 `fakeram45_32x32`（1R1W，19.57×40.60 µm²）
  ≈ **0.051 mm²**；16→32 交叉开关（16 个 64:1 读 mux + 每 bank 16:1
  写地址/数据 mux）≈ **0.12 mm²**；合计 ≈ **0.17 mm²**（原 16R/16W 估算
  2.6 mm²）。见 `../performance/README.md` §4。

## 3. ISA

32-bit 指令，`op` 在 `[31:27]`，`imm` 16 位：

```
 31    27 26   23 22   19 18   15 14            0
┌────────┬───────┬───────┬───────┬──────────────┐
│  op    │  dst  │  srcA │  srcB │     imm      │
└────────┴───────┴───────┴───────┴──────────────┘
```

- `op`：`nop, mov, add, sub, mul, mac, lacc, shr, shl, and, xor, max, min,
  macl, ld, st`（16 个，见 `target.yaml` 的 `isa.ops`）。
- `src` 编码：`r0..r7`、`n/e/s/w`（link 四方向）、`zero`、`imm`、`acc`、`link`。
- `LD/ST` 的 `imm[9:0]` 是 spad 平坦地址；`imm[15]` 是旋转使能。
- 执行模型：静态调度，所有 PE 同步按拍执行同一 IMEM 行的各自列；
  并行来自「同一 kernel 的多个数据实例分到 16 个 PE」，不是空间映射。
- 时序：`LD` 组合读，`ST` 同步写；同拍同地址的 LD/ST 不可重排，
  FFT 调度器因此有专门的约束处理。

## 4. 描述符

两种格式，字段布局相同——32 B 描述符就是 64 B 描述符的前 4 个字。

### 32 B（8 个 32-bit 字）

| 字 | 字段 |
|---|---|
| 0 | `src`：RAM 源/目标地址 |
| 1 | `spad[15:0]`、`page[16]`、`dir[17]`、`irq[18]`、`tgt_rot[19]` |
| 2 | `in_len[15:0]`：搬入字数 |
| 3 | `stride[15:0]`、`row[31:16]`（row=0 为线性） |
| 4–7 | 保留（0） |

### 64 B 复合描述符（16 个 32-bit 字）

在 32 B 基础上增加：

| 字 | 字段 | 含义 |
|---|---|---|
| 6 | `cycles[15:0]`、`arr_page[16]`、`arr_imem[17]`、`arr_en[31]` | 搬入后跑一次阵列（`arr_imem` 选 IMEM bank 0/1） |
| 7 | `pf_page[0]`、`pf_stride[31:16]` | 预取目标 page 与 2D 走位行间步进 |
| 8 | `out_len[15:0]`、`out_spad[31:16]` | 搬出长度/起点 |
| 9 | `out_en[0]` | 是否搬出 |
| 10 | `out_dst` | 搬出到 RAM 的地址 |
| 11 | `pf_row[15:0]`、`pf_in[30]`、`pf_en[31]` | 预取每行字数 / 本行搬入已预取（跳过）/ 阵列运行时预取下一块 |
| 12 | `rep[15:0]`、`in_step[31:16]` | 重复次数与每轮搬入步进 |
| 13 | `out_step[15:0]` | 每轮搬出步进 |
| 14 | `pf_src` | 预取源地址 |
| 15 | `pf_len[15:0]`、`pf_spad[31:16]` | 预取字数与 spad 起点 |

**word 4/5 是保留的**：引擎退休时在 `desc_addr+16` 写状态字
`0xC0DE_0000 | len`，会覆盖这两个字。任何需要在多次运行间保留的字段
都不能放在 word 4/5（ITER-10 的预取源一开始放这里，第 2 次迭代就被
改成了 `0xC0DE_00C8`）。

字段契约的唯一事实来源是 `rtl/cgra/cgra_desc64.yml`。它生成三份视图：

- `rtl/cgra/cgra_desc_pkg.sv`（RTL 取值函数）
- `sw/include/gpcp_desc64.h`（C 侧 `gpcp_d64_set_* / gpcp_d64_*`）
- `sim/cgra/desc64.py`（台架构造/校验）

任何一侧都不允许再手写位移和掩码；`make yaml-check` 会检查生成物同步。

### 拷贝引擎

- 每次突发 ≤ 32 beat（64 字），且不跨 4 KiB 边界；奇数长度的末拍只写低 4 字节
  （`wstrb=0x0F`）。
- 支持 2D：`row` 为每行字数，`stride` 为 spad 行间步进。
- 输出写窗口：搬出目标必须落在 `[win_lo, win_hi)`；越界时 `wstrb=0` 并置
  `WIN_STATUS`，`--watch-writes` 台架会逐字检查窗口外不变。
- 预取（ITER-10）：`pf_en` 描述符在**阵列运行时**把 `pf_src/pf_len` 按同一套
  突发/走位规则流进 `pf_page`，下一条描述符用 `pf_in` 跳过自己的搬入。
  预取只与阵列重叠：`pf_busy` 未清零前主 FSM 不启动阵列、不进搬出/取指，
  因此 spad 的 DMA 口（`dma_page`）始终只有一方占用。
- 描述符环由硬件自取、自写状态字、自推进 head；`RING_DONE` 是完成计数。

## 5. 性能计数器

`cgra_top.sv` / `cgra_array.sv` 提供 PE 活跃、MAC、指令组合、spad 冲突、
DMA 等待等 32-bit 计数器。软件侧：

```
0x5000_0068  写任意值清零
0x5000_0100 + 4*idx  读计数器 idx
```

具体索引见 `sw/include/perf.h` 与 `tools/verify.sh` 的性能门禁。

## 6. 相关文档

- 主机接口与地址空间：[README.md](README.md)
- 软件侧描述符/环的用法：[../software/README.md](../software/README.md)
- 微码生成：[../compiler/README.md](../compiler/README.md)
