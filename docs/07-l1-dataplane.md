# L1 数据面：CGRA 上的 FFT / 信道估计 / MMSE / LDPC

> 里程碑：M4 的第一阶段（裸机 L1 数据面）。
> 状态：**四个 kernel 全部在 RTL（Verilator）上跑通，并与 Python 定点黄金模型逐位一致。**

## 1. 为什么要先扩硬件

M0 的 4×4 阵列只有 8 个寄存器 / 累加器 / 邻居链路，输入只能靠 `din` 顺序流入、
结果只能读寄存器。矩阵乘和 FIR 可以这样跑，但 FFT 需要在级间交换数据、
MMSE 要按子载波读写、LDPC 要做多轮消息传递——**必须有片上存储和跨 PE 的随机访问**。

这次给 CGRA 加了：

| 扩展 | 规格 | 说明 |
|---|---|---|
| 分布式 scratchpad | **64 banks × 32 words × 32-bit（8 KiB）**，分两页 | bank 0–15 存数据（每个 PE 一个 bank），bank 16–31 存每 PE 的系数/旋转因子；bank 32–63 是第二页（见第 4 节） |
| 每 PE 端口 | 1 读 + 1 写，可访问任意 bank/word | 读组合逻辑、写时序，1 周期完成 |
| 新指令 | `LD dst, imm` / `ST src, imm` | `imm[9:0] = {bank[4:0], word[4:0]}`（页内相对） |
| 主机窗口 | `0xA000 + bank*128 + word*4` | CPU 预装输入、回读输出（绝对 bank） |
| 地址冲突修复 | scratchpad 从 0x3000 移到 **0xA000** | 0x3000 与 IMEM 窗口（0x2000–0x9FFF）重叠，长程序会被写进 scratchpad |

调度约定：**同一周期内每个 bank 最多一次读、一次写**。本文所有 kernel 的调度都已满足，
冲突由微码生成器负责（不满足时硬件取最低编号 PE 的写）。

## 2. 定点约定（与黄金模型逐位对齐）

- 数据/系数：int16（Q15），存放为 32-bit 字；
- 乘法：`sx16(a) * sx16(b)` → int32（Q30）；
- 复数乘：`(Br*Wr - Bi*Wi) >> 15`、`(Br*Wi + Bi*Wr) >> 15`，`>>` 为**算术右移**；
- FFT 每级后 **右移 1 位**（缩放 FFT），防止 16 位溢出；
- `add/sub` 走 32 位，因此**参与加减的输入必须符号扩展**（预装时写成 `sx16(x) & 0xFFFFFFFF`）；
  只参与乘法的量可以存 16 位。
- 黄金模型（`tools/cgra.py`）用同一套整数运算，比较时按 32 位掩码逐位比对。

## 3. 四个 kernel

### 3.1 FFT-16（`fft16`，114 周期）

- 算法：基-2 DIT，输入按 4-bit 位反序预装到 bank i（`x[bitrev(i)]`）；
- 布局：`bank i word0/1 = Re/Im(x[i])`；`bank 16+i word 2s/2s+1 = 第 s 级旋转因子 (Wr, Wi)`；
- 每级 28 周期：6 拍载入（Ar,Ai,Br,Bi,Wr,Wi）→ 15 拍复数乘/加/缩放 → 4 拍写回；
- 每级 8 个蝶形并行（活跃 PE 满足 `i % 2^(s+1) < 2^s`，伙伴 `j = i + 2^s`）；
- 4 级共 **114 周期**，1824 条微码；
- 验证：与定点黄金模型逐位一致；与 `numpy.fft`（除以 16 的缩放）相比，
  误差在 Q15 量化噪声量级（绝对误差 ≤ 4 LSB）。

### 3.2 LS 信道估计（`chnest16`，15 周期）

- 每个 PE 负责一个子载波：`H = Y * conj(X)`；
- 布局：`bank i word0..3 = Yr, Yi, Xr, Xi`，结果写 `word4/5`；
- 运算：`Hr = (Yr*Xr + Yi*Xi) >> 15`、`Hi = (Yi*Xr - Yr*Xi) >> 15`；
- 16 个子载波全并行，**15 周期**。

### 3.3 MMSE 均衡（`mmse16`，20 周期）

- `w = conj(H) / (|H|² + σ²)`；阵列负责数据面：
  `Nr = Hr*Yr + Hi*Yi`、`Ni = Hi*Yr - Hr*Yi`、`mag = Hr² + Hi²`（均 `>>15`）；
- 除法留给 CPU（标量、每子载波一次），这是业界常见的「阵列做乘累加、CPU 做标量」分工；
- 布局：`bank i word0..3 = Hr, Hi, Yr, Yi`，结果 `word4/5/6`；
- **20 周期**。

### 3.4 LDPC min-sum 校验节点（`ldpc_cn`，52 周期）

- 每个 PE 一个校验节点、4 条边：`bank i word0..3 = m0..m3`；
- 输出：`word4 = min1`、`word5 = min2`、`word6 = 符号乘积（0 / -1）`；
- 实现要点：
  - `abs(x) = (x ^ (x>>31)) - (x>>31)`（算术右移 + XOR + SUB）；
  - 符号：4 个 `x>>31` 连 XOR；
  - `min1 = min(lo1, lo2)`，`min2 = min(max(lo1,lo2), min(hi1,hi2))`，其中 `lo/hi` 是两两 min/max；
- **52 周期**；边消息（`sign * min_other`）的展开留给下一阶段（可由 CPU 或再来一遍阵列）。

## 4. DMA + 双缓冲（slot 级流水）

L1 的真实用法是「每 0.5 ms 一个 slot，数据自己进来」，所以阵列不能靠 CPU 逐字写
scratchpad。这一阶段加了**描述符驱动的拷贝引擎 + 双页 scratchpad**：

| 机制 | 规格 |
|---|---|
| scratchpad | 64 banks × 32 words × 32-bit（**8 KiB**），bank 0–31 = page 0，32–63 = page 1 |
| `LD/ST` | `imm[9:0] = {bank[4:0], word[4:0]}`，**页内相对**；当前页由 `BUF_PAGE` 选择 |
| 拷贝引擎 | RAM↔scratchpad，每条描述符 1–1024 个 32-bit 字，`DMA_PAGE` 指定目标页 |
| 2D 行走 | `DMA_STRIDE` = 行间步长，`DMA_ROW` = 每行字数（0 = 线性）；行末索引 `+STRIDE-ROW+1` |
| 描述符锁存 | 写 `DMA_CTRL.start` 时把 SRC/LEN/STRIDE/ROW 锁进 run 寄存器 |
| 页隔离 | 引擎只碰 `DMA_PAGE`，阵列只碰 `BUF_PAGE`，所以两者可以在**同一拍**工作 |

### 4.1 软件调度（`sw/soc/dma_bm.c`）

```
warm-up: in(slot0 → P0)
loop s = 0,1,2:
    in(slot s+1 → P^1)     ─┐ 拷贝引擎与阵列并行
    array(P)               ─┘ 阵列读 P，引擎写 P^1
    out(P → out[s])        ─┐ 引擎把 P 的结果搬回 RAM，同时
    array(P^1)             ─┘ 阵列在另一页上算下一个 slot
```

对应用例：16 个子载波、每 bank 4 个字（`yr, yi, xr, xi`），阵列 14 拍算完
`H = Y·conj(X)`，结果写回该页 bank 16–31 的 word 0/1，再由引擎按
`stride=32, row=2, len=32` 搬回 RAM。

- 入向描述符：`stride=32, row=4, len=64`（RAM 里连续的 64 个字铺到 16 个 bank 的 4 个字）；
- 出向描述符：从页内索引 512（= bank 16 word 0）开始，`stride=32, row=2, len=32`。

### 4.2 验证

```bash
python3 tools/cgra.py test --kernel dma_pingpong   # 阵列级：两个页、两次 DMA
make -C sim/soc run-dma                            # SoC 级：CVA6 驱动 3 个 slot
```

SoC 级测试用 CVA6 真机（Verilator）跑完整个流水，把三组 H 值（96 个字）搬回 RAM，
再由 `tools/check_dma.py` 用独立的 Python 黄金模型逐位比对；裸机侧同时做一遍
软件自检，两者都必须为 0 才算通过：

```
[ OK ] DMA + double buffering: 3 slots of 32 H values match the golden model
```

3 个 slot 的完整流水（含 4 次入向、3 次出向拷贝、3 次阵列运行）在 **约 16.5k 周期**
内完成；其中 64 字的一次拷贝约 190 周期（每字 2–3 周期，未做 burst）。

### 4.3 中断驱动（PLIC + `wfi`）

`sw/soc/irq_bm.c` 用同一套流水，但**完全不轮询 CGRA**：

- CGRA 在阵列完成 / 拷贝完成时置粘滞状态位，使能后把 `irq_o` 拉高；
- SoC 的最小 PLIC（源 1）把这条电平线变成 CVA6 的**机器外部中断**（cause 11）；
- `trap_handler` 依次做 `claim → 读 IRQ_STATUS → W1C → complete`，把事件位发布给主循环；
- 主循环用 `wfi` 休眠（`mie.MEIE` 打开），只在检查标志与 `wfi` 之间短暂关中断，
  避免「检查后、睡前」漏掉中断：
  ```c
  while (!(irq_flags & need)) {
      csrci mstatus, 8;            /* 关中断检查 */
      if (!(irq_flags & need)) wfi;/* 线还高会立刻返回，不会死等 */
      csrsi mstatus, 8;
  }
  ```

验证：`make -C sim/soc run-irq` 跑 2 个 slot，5 次完成中断（3 次拷贝 + 2 次阵列，
部分合并），结果与黄金模型逐位一致，且 `bad_traps = 0`。

### 4.4 集成时发现的缺陷（都已修）

| 现象 | 根因 | 修复 |
|---|---|---|
| CPU 写 CGRA 寄存器偶发丢失，阵列不启动 | SoC 的 AXI 仲裁里 DMA 分支排在 CPU 写之前，同一拍握手被丢弃但仍返回 BRESP | DMA 分支移到 CPU 读写之后（CPU 优先） |
| DMA 搬回的 32 位数据全错 | RAM 按 64 位字存取，但引擎没看 `addr[2]`，总是取低半 | 读写按 `addr[2]` 选高低半 |
| 阵列空闲后结果持续被改写 | PE 的 `spad_we` 由操作码组合产生，`en=0` 时仍在执行最后一拍（可能是 `ST`） | scratchpad 写口加 `en` 门控 |
| 2D 描述符退化成线性 / 首次运行 `cycles=0` 却已 start | **CVA6 把同一 8 字节字内的相邻 32-bit store 合并成一次 64-bit AXI 写**，同一寄存器写两次时前一次直接消失 | 新增 `RUN` 寄存器（cycles+page+start 一次写入）；SoC 把 64-bit store 拆成两次寄存器写；成组描述符写用 `fence` |
| 中断永远不陷入（`wfi` 却立刻返回） | CVA6 的 `irq_i[0]`=机器外部、`irq_i[1]`=S 模式外部，SoC 接反了 | 改成 `irq_i = {1'b0, plic_irq}`，`ipi_i = msip[0]` |

第四条是 ABI 层面的坑：**只要寄存器窗口是 32 位、而 CPU 会合并相邻写，就必须保证
「一次 store 就能表达一个完整动作」或中间插入 `fence`**。ABI 里 `gpcp_cgra_array_start()`
与 `gpcp_cgra_dma_start()` 已经按这个规则实现。

## 5. 验证结果（`python3 tools/cgra.py test`）

```
[ OK ] matmul4x4: 6 cycles, 96 microcode words
[ OK ] fir16:    16 cycles, 256 microcode words
[ OK ] fft16:    114 cycles, 1824 microcode words
[ OK ] chnest16: 15 cycles, 240 microcode words
[ OK ] mmse16:   20 cycles, 320 microcode words
[ OK ] ldpc_cn:  52 cycles, 832 microcode words
[ OK ] dma_pingpong: 15 cycles, 240 microcode words
ALL TESTS PASSED
```

每个 kernel 的检查都覆盖**全部输出字**（FFT 16 个复数点、LS/MMSE 16 个子载波、
LDPC 16 个校验节点的 min1/min2/sign），任何一位不一致都会报错。

复现：

```bash
python3 tools/cgra.py build --force      # 重新生成 Verilator 模型
python3 tools/cgra.py test              # 全部 kernel
python3 tools/cgra.py test --kernel fft16
```

## 6. 已知限制 / 下一步

1. **DMA 还没有描述符环**：目前是「一条描述符 = 一组寄存器」，`busy` 期间写入的
   start 会被忽略，也没有 AXI burst（每字 2–3 周期）。完成通知已经可以用中断了
   （CGRA `irq_o` → PLIC 源 1 → CVA6 机器外部中断）。下一步做描述符环（环放 RAM、
   引擎自取），CPU 每 slot 只需推进一次指针。
2. **FFT 规模**：目前 16 点（demo 用），5G NR 需要 4096 点；
   路线是「16 点 kernel + 多级分解 + 旋转因子表」，或在阵列里做多级流水。
3. **MMSE 除法**：留给 CPU；若要完全 offload，可加倒数 LUT 或 Newton-Raphson 微码。
4. **LDPC**：目前是校验节点核心（min1/min2/sign），边输出与迭代调度未做；
   5G NR 的 BG1/BG2 需要更大的消息存储（当前 8 KiB scratchpad 不够，需扩到 64–128 KiB）。
5. **实时性**：当前是两个页的 ping-pong（深度 2）；DU 的 L1 通常要 3–4 个 slot 的
   缓冲深度，并配合中断/RTOS 定时器，这部分在 M5（RTOS 集成）做。

## 7. 与「裸机/RTOS L1」路线的关系

- 这些 kernel 的输入/输出格式已经固定（bank/word 布局 + 定点约定），
  RISC-V 裸机驱动只需按同一布局写 scratchpad、启动、等 done；
- `sw/include/gpcp_cgra.h` 已加入 `GPCP_OP_LD/ST`、`GPCP_CGRA_SPAD_BASE`、DMA 寄存器
  （`GPCP_CGRA_DMA_*`）和 `gpcp_cgra_dma_start()`（内含必要的 `fence`），裸机/内核驱动
  可以直接使用；
- `sw/soc/dma_bm.c` 是「一个 slot 级流水」的可运行范例：CVA6 只负责发描述符、
  轮询完成、导出结果，数据搬运全部由拷贝引擎完成；
- 下一步（M4 第二阶段）：把 `chnest16 → mmse16 → LDPC` 串成一条接收链路，
  由裸机程序按 slot 调度，主机只负责灌数据与读结果。
