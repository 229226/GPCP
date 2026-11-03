# GPCP CGRA 微架构 / ISA / 寄存器映射（v0.1，M0 实现）

本文描述的是**已经实现并通过 Verilator 验证**的 M0 版本，位于 `rtl/cgra/`。
目标版（8×8、scratchpad、双发射）在本文末尾给出演进方向。

---

## 1. 执行模型

- 阵列：`N×N` PE（M0 为 4×4，`cgra_pkg.CGRA_N` 可改）。
- 控制：**全局 PC**，所有 PE 同一周期执行同一条微码地址上的**各自槽位**（空间 VLIW）。
- 时序：每个周期每条指令发射一次；执行完 `NUM_CYCLES` 后自动停机并置 `done`。
- 数据通路：`int16` 有符号乘（Q15），`int32` 累加器；寄存器/链路均为 32 bit。
- 路由：PE 的 **link 寄存器**保存「上一周期发射的值」，N/E/S/W 邻居读到的就是这个值，
  即最近邻、1 周期延迟的路由。

### 1.1 link 语义（重要）

| 指令 | 是否更新 link |
|---|---|
| `MOV/ADD/SUB/MUL/SHR/SHL/AND/XOR/MAX/MIN/LOADI` | 更新为写入值 |
| `MAC` | **不更新**（保持上一次发射值，便于移位+累加并行） |
| `MACL` | 更新为累加结果（用于脉动链） |
| `LACC` | 更新为装载值 |
| `NOP` | 保持 |

因此「偶数拍移位、奇数拍累加」的 FIR 写法可以工作：奇数拍的 MAC 不会破坏邻居看到的样本。

---

## 2. 指令编码（32 bit / PE / cycle）

| 位域 | 宽度 | 含义 |
|---|---|---|
| `[15:0]` | 16 | `imm`：立即数（`SRC_IMM`）或移位量 `imm[4:0]` |
| `[18:16]` | 3 | `dst`：目的寄存器 `r0..r7` |
| `[22:19]` | 4 | `src_b`：操作数 B |
| `[26:23]` | 4 | `src_a`：操作数 A |
| `[30:27]` | 4 | `op` |
| `[31]` | 1 | 保留（0） |

### 2.1 操作码

| op | 名称 | 语义 |
|---|---|---|
| 0 | `NOP` | 无写回，link 保持 |
| 1 | `MOV` | `dst = a` |
| 2 | `ADD` | `dst = a + b` |
| 3 | `SUB` | `dst = a - b` |
| 4 | `MUL` | `dst = sx16(a) * sx16(b)`（32b 乘积） |
| 5 | `MAC` | `acc += sx16(a) * sx16(b)`；link 保持 |
| 6 | `LACC` | `acc = a`；link = a |
| 7 | `SHR` | `dst = a >>> imm[4:0]`（算术右移） |
| 8 | `SHL` | `dst = a << imm[4:0]` |
| 9 | `AND` | `dst = a & b` |
| 10 | `XOR` | `dst = a ^ b` |
| 11 | `MAX` | `dst = signed_max(a, b)` |
| 12 | `MIN` | `dst = signed_min(a, b)` |
| 13 | `MACL` | `acc += a*b`，且 `link = acc`（脉动链） |
| 14 | `LD` | `dst = scratchpad[imm[9:0]]`，`imm = {bank[4:0], word[4:0]}` |
| 15 | `ST` | `scratchpad[imm[9:0]] = a` |

### 2.2 源选择器

| 编码 | 源 | 说明 |
|---|---|---|
| 0–7 | `r0`–`r7` | 本地寄存器 |
| 8 | `N` | 北邻居 link |
| 9 | `E` | 东邻居 link |
| 10 | `S` | 南邻居 link |
| 11 | `W` | 西邻居 link；`PE(0,0)` 的西边界 = 外部流输入 `din` |
| 12 | `zero` | 常量 0 |
| 13 | `imm` | 16b 立即数（符号扩展到 32b） |
| 14 | `acc` | 累加器 |
| 15 | `link` | 自身 link 寄存器 |

边界：除 `PE(0,0)` 的西边界接 `din` 外，其余边界恒 0。

---

## 3. 主机寄存器映射（AXI4-Lite，16 bit 字节地址）

| 地址 | 读/写 | 内容 |
|---|---|---|
| `0x0000` | W | `CTRL`：`[0]` start，`[1]` clear done |
| `0x0000` | R | `STATUS`：`[0]` busy，`[1]` done，`[31:16]` cycle_cnt |
| `0x0004` | W | **`RUN`**：`[15:0]` cycles，`[16]` buffer page，`[31]` start（推荐一次写入启动） |
| `0x0008` | R | `CYCLE_COUNT` |
| `0x000C` | R | `ID` = `{0xCA, ISA_VER=0x01, N, N}` |
| `0x0010` | W | `DMA_SRC`：RAM 源/目的字节地址（32 bit） |
| `0x0014` | W | `DMA_DST`：scratchpad 起始索引（页内 `bank*32 + word`） |
| `0x0018` | W | `DMA_LEN`：传输字数（≤ 1024，超出被截断） |
| `0x001C` | W | `DMA_CTRL`：`[0]` start，`[1]` dir（1 = spad→RAM），`[2]` clear done，`[3]` **目标 = IMEM**（批量装程序） |
| `0x001C` | R | DMA 状态：`[0]` busy，`[1]` done |
| `0x0020` | W | `BUF_PAGE`：阵列使用的 scratchpad 页（0/1） |
| `0x0024` | W | `DMA_PAGE`：拷贝引擎写入/读出用的页（0/1） |
| `0x0028` | W | `DMA_STRIDE`：2D 拷贝的行间 scratchpad 步长 |
| `0x002C` | W | `DMA_ROW`：2D 拷贝每行字数（0 = 线性搬运） |
| `0x0030` | R | `DMA_RUN_SRC`：本次传输锁存的源地址（调试） |
| `0x0034` | R | `DMA_RUN_LEN`：锁存的长度（调试） |
| `0x0038` | R | `DMA_RUN_STRIDE`：锁存的步长（调试） |
| `0x003C` | R | `DMA_RUN_ROW`：锁存的每行字数（调试） |
| `0x0040` | R/W | `IRQ_STATUS`：`[0]` 阵列完成、`[1]` 拷贝完成；写 1 清零（W1C） |
| `0x0044` | R/W | `IRQ_ENABLE`：同上位定义，使能后汇总为 `irq_o` 电平 |
| `0x0048` | R/W | `RING_BASE`：描述符环在 RAM 的起始地址（见 3.3） |
| `0x004C` | R/W | `RING_SIZE`：环深度（2 的幂） |
| `0x0050` | R/W | `RING_HEAD`：引擎下一个要取的描述符 |
| `0x0054` | R/W | `RING_TAIL`：软件推进它来提交描述符 |
| `0x0058` | R/W | `RING_CTRL`：`[0]` 使能；读 `[1]` idle、`[2]` busy |
| `0x005C` | R | `RING_DONE`：已完成描述符计数（进度） |
| `0x1000 + pe*32 + r*4` | RW | PE 寄存器 `r0..r7`（预装载 / 回读） |
| `0x1200 + pe*4` | RW | PE 累加器 |
| `0x2000 + idx*4` | RW | 指令存储器，`idx = cycle*NPES + pe`（8192 words，占 0x2000–0x9FFF） |
| `0xA000 + idx*4` | RW | 分布式 scratchpad，64 banks × 32 words × 32-bit（8 KiB），`idx = bank*32 + word` |

- PE 编号：`pe = row*N + col`（行优先，`(0,0)` 为左上）。
- AXI 写为单 outstanding；`wr_en` 在总线握手后一拍产生脉冲。
- **IMEM 批量装载**：`DMA_CTRL[3]=1` 时拷贝引擎把 8B beat 直接写进指令存储器
  （`word index = DMA_DST + n`，`row = idx>>4`、`lane = idx&15`），一个 beat 两条指令；
  宿主用 `gpcp_cgra_imem_load(base, prog, words)`（内部按 1024 字分块）。
  程序数组必须 **8 字节对齐**（AXI 拍宽）。实测每字比逐字 AXI-Lite 写省 ~5.5 周期
  （`docs/10` §14 有 A/B 数据）。
- **启动一律用 `RUN`（0x0004）一次写**：`cycles | page<<16 | start<<31`；写 `CTRL.start`
  仍然可用，但必须先单独写 `NUM_CYCLES` 并插入 `fence`（原因见下）。
- 运行中读 PE 窗口返回当前值（调试用）；建议仅在 idle 时预装载。
- scratchpad 分两页：绝对 bank 0–31 = page 0，32–63 = page 1；`LD/ST` 与 `DMA_DST`
  的索引都是**页内相对**（`{bank[4:0], word[4:0]}`），主机窗口地址是**绝对**的。

### 3.0 为什么有 `RUN` 寄存器（CVA6 的写合并坑）

CVA6 的 write-through 单元会把**同一 8 字节字内的两次相邻 32-bit store 合并成一次
64-bit AXI 写**（实测 `aw.size=3, wstrb=FF`）。对 CGRA 这种 32 位寄存器窗口：

- 合并后只有“每字节最后写入的值”保留 → 同一寄存器写两次（例如先 `CTRL=clear_done`、
  再 `CTRL=start`）时，前一次**直接消失**；
- 依次写 `NUM_CYCLES(0x04)` 再 `CTRL(0x00)` 也会被合并成一个 64-bit 写，两个寄存器
  的“先后顺序”丢失（首次运行会看到 cycles=0 却已经 start）。

因此把「周期数 + 页 + 启动」压进**一次 store**（`RUN`），从根上避开合并；DMA 描述符
这类必须成组写入的寄存器，则用 `fence`（`gpcp_cgra_dma_start()` 已封装）保证顺序。
SoC 侧对 64-bit store 也会拆成两次 32-bit 寄存器写（见 `gpcp_soc.sv` 的 `cgra_hi_*`）。

### 3.0 scratchpad 写端口模型

每个 bank 在 RTL 模型里带一个**写交叉开关**：同一拍内多个 PE 可以写同一 bank 的
**不同 word**（同 word 冲突按 PE 编号优先；主机写 > 拷贝引擎 > PE）。FFT kernel
「16 个 PE 同拍写同一 bank 的不同字」因此不需要拆成多拍；L1 的四个 kernel 仍然
是非冲突访问。真实 bank 若只有单写口，微码就需要把这些 store 错开（`docs/07` 4.7）。

### 3.1 拷贝引擎（DMA，突发 + 描述符环）

- 一条描述符 = `{SRC, DST, LEN, PAGE, DIR, STRIDE, ROW}`；两种启动方式：
  - **单发**：写寄存器后写 `DMA_CTRL[0]`；
  - **环模式**：描述符放 RAM，引擎自己取（见 3.3）；
- 启动时描述符被**锁存**到引擎内部的 run 寄存器，因此 CPU 可以在传输进行中
  直接写下一组描述符（ping-pong 调度），不会破坏正在跑的传输；
- 传输进行中写 `DMA_CTRL[0]` 被忽略（`busy` 为 1 时不启动）；
- 方向：`DIR=0` 为 RAM→scratchpad，`DIR=1` 为 scratchpad→RAM；
- **突发（真 AXI4 主口）**：拷贝引擎对外是一个 64-bit 的 AXI4 master（`ARLEN/AWLEN`、
  `ARBURST=INCR`、`WLAST/RLAST`、`B` 响应、ID 固定 0）；
- **突发长度**：`cgra_top.DMA_BEATS` 参数（默认 **32 拍 = 64 字 = 256 B**，最大可到 128），
  同时按 **4 KiB 边界**裁剪（AXI 规定的 burst 不得跨页），所以一次 AW/AR 覆盖尽可能长的
  连续区间——这就是「写合并」：同样的数据用更少的写事务；
- **流式数据通路**（无数据缓冲）：入向每个 R 拍**同拍**写进 scratchpad 的两个字，
  出向每个 W 拍**同拍**从 scratchpad 读出两个字；所以一拍 = 2 字，
  引擎里只保留 4 拍（32 B）的描述符缓冲；
- **SoC 侧**：CGRA 主口连到一个 AXI4 从机（第 2 个 master），与 CVA6 的从机共用 RAM；
  仲裁规则是 **CPU 优先**（CPU 那一拍在握手时，CGRA 的 beat 就不授予），
  所以 CPU 的读写不会丢、也不会被 DMA 拖慢（突发是逐拍可被抢占的）；
- **scratchpad 侧**：一拍内的两个 32 位字用**两个写端口**写同一 bank 的不同 word
  （读出方向也有两个读端口）；
- 实测吞吐：**1024 个字的线性拷贝 712 周期（0.70 周期/字）**，比逐字版本（约 3 周期/字）
  快约 4 倍；（改造前 8 拍缓冲 + 分两段的版本是 1.24 周期/字）
- 对齐要求：**SRC（以及出向的 RAM 目的地址）必须 8 字节对齐**（beat 是 64 位，
  RAM 按 64 位字取数）；裸机侧用 `__attribute__((aligned(8)))`；
- 2D 行走：`ROW != 0` 时，每行 `ROW` 个字，行末索引 +`STRIDE-ROW+1`；
  例：`STRIDE=32, ROW=4, LEN=64` 把 RAM 里连续的 64 个字铺到
  页内 bank 0–15 的 word 0–3（`bank i word k`）。一拍内两个关键字必须落在
  不同的 `{bank, word}`（正常布局天然满足）；
- scratchpad 页隔离：拷贝引擎只访问 `DMA_PAGE` 页，阵列只访问 `BUF_PAGE` 页，
  因此「阵列算 page 0、DMA 填 page 1」可以在同一拍同时进行（不同 bank 组）；
- **软件必须在写 `DMA_CTRL.start` 之前执行 `fence`**（裸机里是 `fence iorw, iorw`，
  已封装进 `gpcp_cgra_dma_start()`）:start 是 posted store，可能越过前面的描述符写，
  导致锁存到旧值（例如 `ROW=0`，2D 拷贝退化成线性）；
- CPU 与 DMA 共享 RAM 时 **CPU 优先**：CGRA 的每一拍只在 CPU 不使用总线的周期授予，
  CPU 的读写不会丢；CGRA 侧是完整 AXI4 握手（AW/W/B、AR/R），
  由 SoC 内的第 2 个 AXI 从机（`astate` 状态机）服务。
- **AXI 握手的硬约束（实现时踩过坑）**：`valid` 一旦拉高必须保持到 `ready` 到达，
  而且传输只在 `valid && ready` 同时成立的那一拍才算数——**只看到 ready 就推进是错的**
  （`valid` 还没拉高的那一拍会把数据/地址算掉）。两侧状态机读的是同一组线，
  不要用"某一侧多等一拍"之类的补丁，否则两侧会不对称而死锁。
- **拷贝引擎的 FSM 不能被主机写"抢占整拍"**：早期写法是 `if (wr_en) <配置写> else <FSM>`，
  主机写寄存器的那一拍 FSM 完全不推进；如果正好赶上 B 响应握手，从机取走了 B 而引擎没看见，
  环就永久停住。正确写法是 FSM 每拍都推进，配置写只作为覆盖（启动写仅在 idle 时生效）。
- `0x0060` 是只读调试寄存器：`[3:0]` 引擎 FSM 状态、`[7:4]`… 用于定位卡死（见 3.1 的环状态）。

### 3.2 中断

- `IRQ_STATUS(0x0040)` 是**粘滞**状态位：阵列完成置 `[0]`、拷贝完成置 `[1]`；
- `IRQ_ENABLE(0x0044)` 使能后，`irq_o = |(IRQ_STATUS & IRQ_ENABLE)`（电平，直到软件清掉）；
- 清除用 **W1C**（写 1 到对应位）；同一拍新到的事件不会被清除吞掉（硬件按
  `(status & ~mask) | events` 处理）；
- 软中断处理顺序必须是 **claim（PLIC）→ 读状态 → W1C → complete**：先 claim 再清状态，
  否则电平可能在 claim 之前掉下去，claim 会返回 0（变成伪中断）；
- **两个事件挨得很近时，W1C 会合并两次完成**（第一次 ISR 清状态时第二次事件已置位，
  一次清除把两个都清掉）。所以“完成数”不要数中断，要读**进度计数器**
  `RING_DONE`（环模式）或 `DMA_CTRL.done`（单发）；中断只当唤醒用；
- SoC 里这条线接 CVA6 的机器外部中断（`irq_i[0]`，cause 11），详见 `docs/06-linux-b3.md` 1.3。

### 3.3 描述符环

描述符放在 SoC RAM（**32 字节 / 8 字节对齐**），引擎自取、自写状态、自推进 head：

| 偏移 | 内容 |
|---|---|
| `+0x00` | `src`（u32，8 字节对齐） |
| `+0x04` | `dst`（spad 页内索引）`\|` `page<<16` `\|` `dir<<17` `\|` `irq<<18` |
| `+0x08` | `len`（字数，≤1024） |
| `+0x0C` | `stride`（低 16）`\|` `row<<16` |
| `+0x10` | 状态：引擎完成时写 `0xCAFE0000 \| len`（只写低 32 位） |
| `+0x14..1F` | 保留 |

寄存器（`0x0048` 起）：

| 地址 | 内容 |
|---|---|
| `0x0048` | `RING_BASE`：描述符 0 的 RAM 地址 |
| `0x004C` | `RING_SIZE`：环深度（2 的幂） |
| `0x0050` | `RING_HEAD`：引擎下一个要取的描述符（只读，可用写复位） |
| `0x0054` | `RING_TAIL`：最后一个有效描述符的下一个（软件推进它来提交任务） |
| `0x0058` | `RING_CTRL`：`[0]` 使能；读 `[1]` 空闲（head==tail）、`[2]` busy |
| `0x005C` | `RING_DONE`：已完成描述符计数（**进度**，避免数中断） |

行为：使能且 `head != tail` 时引擎自动开始；每个描述符完成后写状态字、`RING_DONE++`、
`head` 前进（到 `size` 回绕），描述符的 `irq` 位置 1 时发完成中断；
`head == tail` 时回到空闲，`RING_CTRL.idle` 置位。`RING_TAIL` 允许写成 `RING_SIZE`
（**"最后一个的下一个"与 0 是同一个槽**），引擎内部按 `tail mod size` 比较
（`ring_tail_n = (ring_tail >= ring_size) ? 0 : ring_tail`）；否则 head 回绕到 0 的
批次会把前几个描述符再执行一遍（台架 `--ring-size 2 --ring-tail 2` 可复现）。

---

## 4. 软件 ABI（`sw/include/gpcp_cgra.h`）

```c
// 伪代码：一次任务
cgra_write(NUM_CYCLES, cycles);
for (i = 0; i < cycles*NPES; i++) cgra_write(IMEM + i*4, prog[i]);
for each (pe, reg, val) cgra_write(0x1000 + pe*32 + reg*4, val);
cgra_write(CTRL, 1);
while (!(cgra_read(STATUS) & 2)) ;     // poll done（后续改为中断）
for each (pe, reg) v = cgra_read(...);
```

驱动层（M1 起）：`/dev/gpcp-cgra` + `ioctl(GPCP_CGRA_RUN, &task)`，任务结构包含
`prog_dma_addr / prog_len / preload[] / in_dma_addr / out_dma_addr / cycles / flags`。

---

## 5. 已实现 kernel 与映射方法

### 5.1 `matmul4x4`（4 周期）

`C[i][j] = Σ_k A[i][k]·B[k][j]`

- 预装载：`PE(i,j)` 的 `r0..r3 = A[i][0..3]`，`r4..r7 = B[0..3][j]`；
- 微码：第 k 拍执行 `MAC rk, r(4+k)`；4 拍完成，结果在 `acc`。
- 验证：`build/kernels/matmul4x4/`，与 Python 黄金模型逐 PE 比对。

### 5.2 `fir16`（16 tap，2 周期/样本）

`y[n] = Σ_t h[t]·x[n-t]` 的前端：样本沿 4×4 的**蛇形路径**逐拍推进，每个 PE 持有一个抽头系数。

蛇形路径（PE 编号按行优先）：
```
(0,0) (0,1) (0,2) (0,3)
(1,3) (1,2) (1,1) (1,0)
(2,0) (2,1) (2,2) (2,3)
(3,3) (3,2) (3,1) (3,0)
```

- 偶数拍：`MOV r1, <路径前一跳方向>`（W/N/E/S，`(0,0)` 用 `W` 取 `din`）；
- 奇数拍：`MAC r0, r1`；
- 第 `2k` 拍后，路径位置 `t` 的 PE 持有 `x[k-t]`；
- 验证：`r1` 的移位正确性 + `acc = h[t]·Σ x[m]` 逐 PE 比对。

### 5.3 已知限制（M0）

1. 无 PE 本地 scratchpad，数据只能靠寄存器 + 最近邻路由；
2. 无跨行/跨列的 2 跳以上路由，复杂 FFT 需要多拍搬运；
3. 结果归约（如 FIR 的空间求和、FFT 蝶形跨距 2/4/8）由软件多拍实现，效率低；
4. 单发射、无谓词/无循环硬件；
5. 拷贝引擎是 AXI4 主口，但一次只发一个突发（无 outstanding），也没有
   narrow/unaligned transfer 支持（要求 8 字节对齐、INCR）。

---

## 6. 演进方向（M1–M3）

| 能力 | 设计要点 |
|---|---|
| 8×8 / 16×16 | `CGRA_N` 参数化；分层互连（簇内全连、簇间 2 跳） |
| scratchpad | 每 PE 64×32b（双口），支持 `LD/ST imm` 与邻 PE 共享 |
| 双发射 | 64b 指令：1 算术 + 1 访存/路由 |
| 谓词与循环 | `pred` 位 + `loop_start/loop_end` 硬件计数 |
| DMA | 描述符环 + 双缓冲 + AXI4 主口（**双缓冲、2D 拷贝、INCR 突发、环、中断已实现**，见 3.1/3.3） |
| 复数支持 | `CMAC`：一次完成 (ar·br − ai·bi, ar·bi + ai·br) |
| FFT 专用 | 蝶形跨距路由网络（stage 可配置） |
| 编译 | MLIR dialect（`cgra`）→ 布局布线 → 微码二进制 |
