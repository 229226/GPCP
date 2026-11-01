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

### 4.4 流式突发拷贝（真 AXI4 主口）与描述符环

拷贝引擎对外改成**完整 AXI4 主口**：`ARLEN/AWLEN`、`ARBURST=INCR`、`WLAST/RLAST`、
`B` 响应，64-bit 数据、8 字节对齐。

- **流式**：入向每个 R 拍同拍写进 scratchpad 的两个字，出向每个 W 拍同拍读出两个字，
  引擎里不再有数据缓冲（只保留 4 拍描述符缓冲）→ **一拍 2 字**；
- **突发长度**：`DMA_BEATS` 参数（默认 32 拍 = 64 字 = 256 B），按 **4 KiB 边界**裁剪，
  因此同样的数据用更少 AW/AR（写合并）；64/128 拍也试过，收益已接近饱和；
- SoC 里加了**第 2 个 AXI 从机**服务这条主口，和 CVA6 的从机共用 RAM，
  仲裁 **CPU 优先**（逐拍可抢占）；
- 实测 **1024 字线性拷贝 712 周期（0.70 周期/字）**：逐字版约 3 周期/字（4×），
  改造前的「8 拍缓冲 + 读/写分两段」版本 1.24 周期/字（1.8×）；
- 代价：`SRC` / 出向 RAM 目的必须 **8 字节对齐**（裸机数组已加 `aligned(8)`）。

描述符环把「CPU 每组描述符写 7 个寄存器」变成「CPU 每批只推进一次 `RING_TAIL`」：

```
CPU: 填描述符 → RING_TAIL += n          （一次 fence）
引擎: AR 取 32B 描述符 → 突发拷贝 → AW 写状态字 → RING_DONE++ → HEAD++ → irq
```

`sw/soc/ring_bm.c` 用它跑完「2 个入向 + 2 个出向」四个描述符：CPU 只在两批之间
插两次阵列运行，其余全部由引擎自主完成，`RING_HEAD` 最终等于 `RING_TAIL`，
四个描述符的状态字都是 `0xC0DE0000 | len`。

### 4.5 接收链：chnest → MMSE → 16-QAM 软解调(LLR) → LDPC-CN

`sw/soc/rxchain_bm.c` 把四个阶段拼成**一段融合微码**，每个 slot 一次阵列运行
（**127 拍**），数据全部通过 scratchpad 传递，中间结果不经 CPU：

| 周期 | 阶段 | 读（页内 bank p） | 写 |
|---|---|---|---|
| 0–14 | LS 信道估计 | word 0–3（Yr,Yi,Xr,Xi） | word 4,5（Hr,Hi） |
| 15–34 | MMSE 分子 + \|H\|² | word 4,5（H）与 2,3（Y） | word 20,21,22（Nr,Ni,\|H\|²） |
| 35–74 | **16-QAM max-log 软解调** | word 20/21（Re/Im）+ 22（\|H\|²） | word 24–27（4 条 LLR） |
| 75–126 | LDPC 校验节点（min-sum） | word 24–27（4 条软比特） | word 4,5,6（min1,min2,sign） |

**解调器为什么能不下发到 CPU**：用 max-log 判决度量而不是"先归一化再切星座"：

```
M(s) = |H|²·s² − 2·Re{y·conj(H)}·s      （y·conj(H) 就是 MMSE 输出的 Nr/Ni，|H|² 是 mag）
LLR(b) = min_{s: b=1} M(s) − min_{s: b=0} M(s)      然后 >>10 归一化成软比特
```

每个轴只有 4 个候选点（PAM-4 Gray：−3:"10"，−1:"11"，+1:"01"，+3:"00"），
`M(±1)=m∓2r`、`M(±3)=9m∓6r` 全部由加法/移位/乘法构成——**没有除法**，
所以 4 条 LLR 直接喂给 LDPC 校验节点的 4 条消息。

- 输入 4 字/bank、输出 3 字/bank，都用 2D 描述符（`STRIDE=32, ROW=4/3`）；
- 调度（CPU 只推进 `RING_TAIL`）：
  ```
  ring: IN0(p0) IN1(p1) | OUT0(p0) IN2(p0) | OUT1(p1) IN3(p1) | OUT2(p0) OUT3(p1)
  数组跑 p0 时引擎在 p1 上出/入 —— 同一拍访问不同 bank 组（双缓冲）
  ```
- 实测：**4 个 slot 用 2057 周期（514 周期/slot）**，即阵列时间（127 拍）的 4 倍
  （其余是 DMA 搬运与中断/唤醒开销；阵列等待 1220、描述符等待 546）；
  8 个描述符全部由引擎自主完成，`RING_HEAD == RING_TAIL`；
- 软件约束（本轮踩到并修好）：**粘滞中断状态位是 W1C、且 ISR 一次清两位**，
  所以「等待阵列完成」不能轮询该状态位（会被并发的 DMA 中断清掉而永久错过），
  必须在 ISR 里累加**完成计数**（与 `RING_DONE` 同样的"进度"语义）。

### 4.6 FFT：radix-4 分级 + 融合 radix-16 尾段（`sw/soc/fft_bm.c`）

scratchpad 一页只有 1024 字、单次阵列运行上限 512 拍，所以 4096 点复数 FFT 必须
分块。实现分两段：

1. **完整级（L ≥ 256，M = L/4 ≥ 64）**：一级 radix-4 DIF，按 64 个蝶形（256 点）分块，
   镜像微码 448 拍，数据布局 `word = 128*j + 2*bi + h`（4 个子块各占 128 字），
   描述符是 4 条 **线性** 128 字拷贝 + 1 条 384 字旋转因子块；
2. **尾段（L = 64/16/4）**：三级都落在同一个 64 点块里，融合成一条 336 拍的
   **64 点块 kernel**（radix-16 = radix-4×radix-4，再 radix-4），每个块一次运行、
   一条 128 字线性 in + 一条 out；旋转因子是**级常量**，由 CPU 一次写入两页。

旋转因子表由 `tools/fft_plan.py` 离线生成（Q15，按 (size, stage, tile) 384 字一块，
外加 120 字的尾段常量块），作为 ROM 镜像加载到 `0x8010_0000`（88 KB）。
`tools/fft_plan.py` 同时是黄金模型：内置的 radix-4 DIF 逐步执行 + DMA/布局模型，
三者（微码级模型、SoC 程序、检查器）用同一份定义。

调度：**每块一个连续环窗口** `[in_i(5)][out_i(4)]`。顺序是
`启动环(HEAD=t_dstart[i], TAIL=t_in_end[i])` → 等输入 5 条完成 → 跑阵列（448/336 拍，
启动后加 `fence` 再轮询 `STATUS`）→ `push(t_out_end[i])` → 等输出 4 条完成 → 关环。
这样 out 拷贝只能在阵列写完之后读同一页，双页只用来隔离「上一块的页」与「下一块的页」。

> 早期版本为了重叠把 out_i 放到 in_{i+1} 之后发布（`[in_i][out_{i-1}]`），在 1024 点
> （每 stage 4 块、共 8 块）上触发了「输出先于阵列写回被排空」，表现为 j=2/j=3 子块与
> 最后一块的 j=0/j=1 整段错；片上自检（`-DCHK1024` + `fft_plan.py exp` 离线快照）
> 定位到 stage 0 后 640/1024 错、首个错点 192（块内偏移 0）。改成每块窗口后消失。

实测（`make -C sim/soc run-fft`）：

| N | 阵列运行 | 周期 | 周期/点 | 与黄金模型 |
|---|---|---|---|---|
| 256 | 5 | 4399 | 17.2 | **逐位一致** |
| 1024 | 24 | 22956 | 22.4 | **逐位一致** |
| 4096 | 112 | 116761 | 28.5 | **逐位一致** |

> 逐位一致的判据是完整 2N 个字（re/im）与黄金模型完全相同；`-DCHK1024` 调试构建
> （离线快照比对）保留在代码里，用于以后回归时分阶段定位。

### 4.7 集成时发现的缺陷（都已修）

| 现象 | 根因 | 修复 |
|---|---|---|
| CPU 写 CGRA 寄存器偶发丢失，阵列不启动 | SoC 的 AXI 仲裁里 DMA 分支排在 CPU 写之前，同一拍握手被丢弃但仍返回 BRESP | DMA 分支移到 CPU 读写之后（CPU 优先） |
| DMA 搬回的 32 位数据全错 | RAM 按 64 位字存取，但引擎没看 `addr[2]`，总是取低半 | 读写按 `addr[2]` 选高低半 |
| 阵列空闲后结果持续被改写 | PE 的 `spad_we` 由操作码组合产生，`en=0` 时仍在执行最后一拍（可能是 `ST`） | scratchpad 写口加 `en` 门控 |
| 2D 描述符退化成线性 / 首次运行 `cycles=0` 却已 start | **CVA6 把同一 8 字节字内的相邻 32-bit store 合并成一次 64-bit AXI 写**，同一寄存器写两次时前一次直接消失 | 新增 `RUN` 寄存器（cycles+page+start 一次写入）；SoC 把 64-bit store 拆成两次寄存器写；成组描述符写用 `fence` |
| 中断永远不陷入（`wfi` 却立刻返回） | CVA6 的 `irq_i[0]`=机器外部、`irq_i[1]`=S 模式外部，SoC 接反了 | 改成 `irq_i = {1'b0, plic_irq}`，`ipi_i = msip[0]` |
| 突发拷贝后 spad 里 xr/xi 变成了 yr/yi | 引擎在 `ready` 晚一拍的旧接口下，每个 beat 都保持同一个地址，RAM 重复返回上一拍数据 | SoC 改成**组合应答**（同拍 `ready`+数据）；引擎一拍一个 beat |
| 两个描述符完成得太快，只收到一次中断 | W1C 状态位与 ISR 竞态：第二次事件在第一次清除前已置位，一次清除清掉两个 | 环模式加 `RING_DONE` 进度计数器，软件等状态而不是数中断 |
| AXI 化之后所有数据读成 0 | `dma_baddr` 误声明成 16 位，`0x8000_0478` 被截成 `0x0478`，地址判成非 RAM | 改成 32 位 |
| OUT 方向第二个突发写回第一突发的数据 | W 阶段把 `dma_beat` 用到 7，进下一个 `D_SPAD_R` 前没归零 | 续突发时把 beat 清零 |
| 环模式的 OUT 描述符跑完不推进 head | AXI 重写时只给 IN 路径加了「写状态字」分支 | OUT 完成也走 `wr_status → D_AW → D_BRESP` |
| 接收链偶发挂死（等阵列） | 轮询粘滞 IRQ 状态位，ISR 处理并发 DMA 中断时把 ARR 位一起 W1C 清掉 → 永久错过 | ISR 里累加阵列完成计数，等待改成「进度 >= 目标」 |
| 接收链偶发挂死（等描述符） | **拷贝引擎的 FSM 被放在 `if (wr_en) … else <FSM>` 里**：主机写寄存器的那一拍整个 FSM 不推进；若正好是 B 响应握手，从机取走了 B 而引擎没看见 → 环永久停在同一条描述符 | FSM 改为**每拍都推进**，主机写只作为"配置覆盖"放在后面（写启动只在 idle 时有效，不冲突） |
| （排查记录）AXI 握手合法性 | 一度用"让从机把 bvalid 多保持两拍"来绕，反而因两侧各自加延迟而不对称、死锁 | 回到纯 AXI 语义：**valid 拉高一直保持到 ready，传输只在 `valid && ready` 成立**；两侧时钟域内状态机对同一拍达成一致 |

第四条是 ABI 层面的坑：**只要寄存器窗口是 32 位、而 CPU 会合并相邻写，就必须保证
「一次 store 就能表达一个完整动作」或中间插入 `fence`**。ABI 里 `gpcp_cgra_array_start()`
与 `gpcp_cgra_dma_start()` 已经按这个规则实现。

### 4.8 slot 时基调度骨架（`sw/soc/slot_rtos.c`）

把 `irq_bm` 的「等完成」改造成真正的 **slot 循环**：

- **CLINT 机器定时器**（`mtimecmp`）每 1200 拍产生一次机器定时中断，ISR 只做三件事：
  `ticks++`、按绝对时刻重装 `mtimecmp`（落后的 tick 记成 overrun）、置 `slot_pending`；
- 主循环 `while (slots_done < SLOTS) { wfi; if (slot_pending) slot_job(slots_done++); }`，
  服务一个 slot 后再睡回去；
- `slot_job` = 输入 DMA（64 字，row=4/stride=32）→ 15 拍 LS 信道估计 → 输出 DMA
  （32 字，bank 16 起）；两步 DMA 与阵列完成都由 PLIC 中断唤醒 `wfi`；
- 验收：8 个 slot、24 次完成中断、tick 间隔严格 1200 拍（抖动 0）、零 overrun，
  H 值与黄金模型逐位一致（`tools/check_slot.py`）。

### 4.9 NR 同步与 Polar kernel（B4 骨架的第一批积木）

序列定义（38.211 / 38.212，`tools/nr_seq.py` 与微码共用）：

| 信号 | 定义 |
|---|---|
| PSS | 127 点 m 序列 `x(i+7)=x(i+4)+x(i)`，`d(n)=1-2x((n+43·N_ID2) mod 127)` |
| SSS | 127 点 `[1-2x0((n+m0) mod 127)]·[1-2x1((n+m1) mod 127)]`，`m0=15⌊N_ID1/112⌋+5N_ID2`，`m1=N_ID1 mod 112`（x1 用 `x1(i+7)=x1(i+1)+x1(i)`） |
| Polar | `x=u·F⊗n`，`F=[[1,0],[1,1]]`，按 `x[i]^=x[i+step]` 的蝶形（step=1,2,…,N/2），无附加置换 |

**`pss_corr` / `sss_corr`（52 拍）**：16 个 PE，每个 8 个 BPSK 抽头（127/128 用满），
实部用 `MAC` 累加进 `acc`、虚部用普通寄存器累加，产出 16 个 per-PE 复相关分块；
CPU 把 16 个分块相加即为 127 点相关值（BPSK 参考符号为 Q15 全幅 ±32767）。
spad 布局：rx 点 `t` 在 bank `t>>4` 的 `2*(t&15)`（re/im 占两个字），参考在 bank `8+(t>>4)`。

**`polar_enc`（43 拍）**：512 bit 输入 = 16 个字（32 bit/字，LSB-first）。
step 1/2/4/8/16 在同一字内用「算术右移 + 编译期掩码 + XOR」，step 32…256 用 PE 间字异或
（只让 `(w & d)==0` 的 PE 写回，其余 NOP），9 级全部在一个阵列运行里完成。

**`polar_node`（22 拍）**：16 组 `(a,b,β)` → min-sum `f=sign(a)·sign(b)·min(|a|,|b|)`
与 `g=b+(1-2β)a`；`g` 用 `-β` 掩码 + 移位实现「乘以 ±1」，**不用 32 位乘法**（`MUL` 只取 16 位）。

以上 4 个 kernel 全部与 `tools/nr_seq.py` 的黄金模型**逐位一致**（进入 `python3 tools/cgra.py test`，
共 13 项）。这是 SSB/PBCH 链路（FFT → PSS/SSS → 信道估计 → 解调 → Polar）的算法积木；
尚未接入 SoC 链路，也没有 SSS 的 336 假设搜索与 SC 译码的树遍历调度。

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

1. **AXI 主口只有单 outstanding**：一次一个突发，没有多 outstanding 或乱序完成；
   突发长度由 `DMA_BEATS` 决定（默认 32 拍，已接近饱和）。
2. **环深度与页数**：环最多 8 个描述符（可扩），scratchpad 只有 2 页 ping-pong；
   DU 的 L1 通常要 3–4 个 slot 的缓冲深度。
3. **FFT 规模**：目前 16 点（demo 用），5G NR 需要 4096 点；
   路线是「16 点 kernel + 多级分解 + 旋转因子表」，或在阵列里做多级流水。
4. **接收链是结构演示**：chnest → MMSE → LDPC-CN 串起来了，但 MMSE 除法仍在 CPU、
   LDPC 的输入是 `{Nr,Ni,|H|²,Hr}` 而非真实 LLR，且只做了单次校验节点更新
   （没有迭代/边消息存储）；真正的接收链还需要解调（QAM 软判决）+ 速率解匹配 + 分层调度。
5. **MMSE 除法**：留给 CPU；若要完全 offload，可加倒数 LUT 或 Newton-Raphson 微码。
6. **LDPC**：目前是校验节点核心（min1/min2/sign），边输出与迭代调度未做；
   5G NR 的 BG1/BG2 需要更大的消息存储（当前 8 KiB scratchpad 不够，需扩到 64–128 KiB）。
7. **实时性**：中断已可用（`wfi` 唤醒），但还没有 RTOS 定时器/slot 调度框架。
   现在整条链 481 周期/slot（16 子载波），离真实 slot（0.5 ms ≈ 50k 周期 @100 MHz）
   还有规模化问题。

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
