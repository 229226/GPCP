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
| spad | 32 bank × 32 word × 2 页 | `target.yaml` |
| 时钟 | 100 MHz（SoC 目标） | `target.yaml: clock_hz` |

## 2. spad

- 平坦索引 `flat = bank*32 + word`，10 位（`imm[9:0]`，最大 1023）。
- 主机窗口 `0xA000 + bank*128 + word*4`，CPU 可直接读写任意字。
- 两页由 `page` 位选择：阵列用一页时，拷贝引擎操作另一页。
- 每 bank 有一张 7-bit 循环旋转表（`rot` 5 bit + `invalid` + `keep`），
  由 `ROT_BASE + bank*4` 写入，`ROT_ZC` 设置循环模；`LD/ST` 的 `imm[15]`
  决定该次访问是否参与旋转。默认全 0，行为与无旋转表时一致。
- 综合口径：当前 spad 需要每 bank 16R/16W，约 2.6 mm²；要降到 0.3 mm² 量级
  必须先改 kernel 数据布局（见 `../performance/README.md`）。

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
| 8 | `out_len[15:0]`、`out_spad[31:16]` | 搬出长度/起点 |
| 9 | `out_en[0]` | 是否搬出 |
| 10 | `out_dst` | 搬出到 RAM 的地址 |
| 12 | `rep[15:0]`、`in_step[31:16]` | 重复次数与每轮搬入步进 |
| 13 | `out_step[15:0]` | 每轮搬出步进 |

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
