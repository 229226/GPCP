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

### 4.10 SoC 级 PSS 检测（`sw/soc/sync_bm.c`）

环 + 阵列 + CPU 的第一次 5G 功能闭环：对 `N_ID(2) = 0,1,2` 三个候选各跑一遍
`bpsk_corr`（52 拍），CPU 在 64 位里把 16 个 per-PE 复相关分块相加，取 `|re|+|im|`
最大者作为检出（`tools/check_sync.py` 复算）。

| 项 | 值 |
|---|---|
| spad 布局 | rx 254 字（线性，bank 0..7）；ref 127 字（2D：每 bank 16 字，bank 8..15）；结果 32 字（每 bank 2 字，bank 16..31） |
| 描述符 | 每候选 3 条：`in rx`、`in ref`（`stride=32,row=16`）、`out`（`stride=32,row=2`） |
| 实测 | 3 次阵列运行、**2688 周期**；度量 `(282647, 36947678, 447186)` → 检出 `N_ID2=1`（真值 1）；72 个 partial 字**逐位一致** |

复用同一份微码（`sw/soc/sync_bm.c` 里用 `gpcp_cgra_inst()` 生成）即可做 SSS：
把参考换成 SSS 序列并循环 112 个候选（`m0` 由已检出的 `N_ID(2)` 决定）。

调试中踩到两次，均已写进代码注释：

* **8 字节对齐**：候选 ref 的起始地址 `+4*127` 只有 4 字节对齐，DMA 的 64-bit beat 读出的
  数据错位（cand1 全错、cand2 部分错）→ 每个候选的 ref 块按 128 字（512 B）对齐；
* **度量溢出**：`(|re|+|im|)` 是 ~3.8e10，先转 32 位再 `>>10` 会截断 → 先移位再截断。

### 4.11 SoC 级 SSS 假设搜索（`sw/soc/sss_bm.c`）

PSS 给出 `N_ID(2)` 后，SSS 只需搜 `N_ID(1) = 0..335`（`m0 = 15⌊N_ID1/112⌋ + 5N_ID2`，
`m1 = N_ID1 mod 112`）。同一份 `bpsk_corr` 微码，**每个候选 3 条描述符**
（rx 254 字线性、ref 127 字 2D `stride=32,row=16`、out 32 字 2D `stride=32,row=2`），
端口上 `publish → 等输入 → 52 拍阵列 → push → 等输出 → 关环`，CPU 用 int64 求和取
`|re|+|im|` 的 argmax。

| 项 | 值 |
|---|---|
| 假设数 | 336（`N_ID1`），`N_ID2` 取 PSS 的检出值 |
| 参考块 | 启动时预生成 **336 × 128 字 = 172 KB**（`0x8010_0000`），全部 8 字节对齐 |
| 结果块 | **每候选独立 32 字**（`0x8018_0000`，第 337 块留给复核） |
| 实测 | **337 次阵列运行、218,894 周期**；检出 `N_ID1=200`（真值 200）；detected 与 true 两个候选的 32 个 partial 字全部逐位一致 |

界面上一个关键坑（已写进代码注释）：**引擎写 RAM 不会让 CPU 的 D$ 失效**。
最初所有候选复用同一个结果缓冲，CPU 读到的是上一候选的缓存行（表现为"336 个候选
度量完全相同"）；改成每候选独立结果块（首访必然 miss → 看到 RAM 新值）后正常。
同理，参考块原本"每候选现写再立刻 DMA"，CPU 写缓冲还没排空、引擎读到旧数据，
改为**启动时一次性预生成**。

### 4.12 Polar SC 译码的树遍历（`sw/soc/polar_sc_bm.c`）

N=64、K=32，冻结集按经典极化次序（`bitrev6(i) < N-K`）。CPU 递归走 SC 树，
每个内部节点把 `(a, b, β)` 三元组交给 `polar_node` 微码（min-sum `f`/`g`，22 拍，
16 对/次），`β` 为左子节点的部分和；叶子层的部分和就是译出的信息位。

| 项 | 值 |
|---|---|
| 节点调用 | 树共 127 个节点（63 个内部 + 64 叶子），内部节点按 `ceil(pairs/16)` 切块 → **128 次阵列运行** |
| 描述符 | 每次 5 条（`in a`/`in b`/`in beta` → bank 0/1/2；`out f`/`out g` ← bank 16/17） |
| 实测 | **79,025 周期**；译出 64/64 位与发送字完全相同（32 个信息位全对）；与 `tools/check_sc.py` 的 Python SC **逐位一致** |
| 输入 | `LLR = ±3000 + (LCG 噪声 ±512)`，编码用同一蝶形 `x[i]^=x[i+step]` |

两个容易踩的点（已写进注释）：**SC 的输出是叶子层 `bbuf[DEPTH]`**（根节点
`bbuf[0]` 是重建码字，拿它比对会得到 ~50% 错误）；以及每个节点都要用**新的结果块**
（引擎写 RAM 不会让 CPU 的 D$ 失效，见 4.11）。

### 4.13 SSB 片段串联（`sw/soc/ssb_bm.c` + `tools/ssb_plan.py`）

整条 SS/PBCH block 接收链在一个程序里跑通，结构与 38.211/38.212 对齐，参数做了简化
（**明确的限制**见下）：

| 阶段 | 做法 | 实测 |
|---|---|---|
| 发射（CPU 造数据） | 240 RE × 4 符号：PSS(sym1)/SSS(sym3) 占中间 127、PBCH DMRS 每 4 个 RE 一个（60 个/符号，Gold 序列 `c_init=3·N_ID1+N_ID2`）、其余 RE 放 QPSK 数据；标量信道 (2,1)、噪声 ±512 | — |
| 1. PSS 检测 | `bpsk_corr` 微码 3 次 | N_ID2 = 1 ✓ |
| 2. SSS 搜索 | 同一微码 336 次（`m0` 由 N_ID2 决定） | N_ID1 = 200 ✓ |
| 3. DMRS 信道估计 | 每符号 60 个导频 `Σ y·d*`（`H_SCALE=16` 保留小数，避免 1.99→1 的整数截断） | H = (32,16)/(31,15)/(32,16)/(32,16) 与黄金一致 ✓ |
| 4. QPSK 解调 + PBCH 解扰 | `Re(y·conj(H)) >> 2` 作软值，Gold 解扰（同 `c_init`） | — |
| 5. Polar SC 译码 | N=512、K=32（`bitrev9(i) < N-K`），CPU 走树、`polar_node` 微码算 f/g | 信息位 32/32 ✓ |
| 6. 与发射端比对 | MIB 载荷（24 bit + 8 bit 补零） | **逐位一致**，mism = 0 ✓ |

合计 **1459 次阵列运行、2,414,474 周期**（≈1.6k 周期/次，主要花在每个节点的
5 条描述符 + 环启停 + CPU 搬运上；这是正确性闭环，不是吞吐最优）。

**简化的部分（不要误当成完整 PBCH）**：

* 频域起步 —— 4096 点 FFT 已单独验证（`run-fft` 逐位一致），尚未在本程序里串联；
* 无速率匹配/交织：真实 PBCH 传 E=864 bit，这里直接传 N=512 个码字比特；
* 无 CRC24：32 个信息位 = 24 bit MIB + 8 bit 补零；
* 冻结集用极化次序 `bitrev9` 而非 38.212 的 5G 可靠度表；DMRS/解扰的 `c_init`
  也做了简化。
* 单 RX 天线、平坦信道（没有多径/频偏/时偏）。

### 4.14 采样级 SSB：4096 点 FFT → PSS/SSS 检测（`sw/soc/ssb_fft_bm.c`）

第一个真正"从时域样本开始"的用例：`tools/ssb_plan.py` 把 4 个 SSB 符号做成
4096 点时域样本（`ssb_plan.py dump` → `build/ssb/td.bin`，`TD_SCALE = 256`），程序对
PSS 符号（1）和 SSS 符号（3）各跑一次 **4096 点 radix-4 FFT**（`sw/soc/ssb_fft.inc`，
与 `run-fft` 同一份微码/ROM），按**基-4 位反序**取出 127 个 RE
（`X[k] = data[rev4(k)]`，归一化输出 ×16 = `<<4` 正好抵消 `4096 / TD_SCALE`），再做
PSS 三候选相关与 SSS 336 假设搜索（同一个 `bpsk_corr` kernel，52 拍；rx 只搬一次，
每个假设只搬 127 个参考字 + 32 个结果字）。

| 项 | 值 |
|---|---|
| 实测 | **1907 次阵列运行（448 FFT + 3 PSS + 336 SSS + 1120 个 polar 节点块）、4,335,553 周期**；检出 **N_ID2 = 1**、**N_ID1 = 200**、**MIB 32/32 位**（全部与模型一致），`errs = 0` |
| PSS | `|c0|/|c1|/|c2| = 2990 / 104,909 / 13,166`，模型 6,051 / 209,847 / 26,295（比值 35 / 0.125 与模型一致） |
| SSS | 检出假设的度量 6,207,810（>>10 后），与真假设的度量完全相同；模型 argmax 也是 200 |
| DMRS | 每个符号的信道估计都是 **H = (16, 0)**（`H_SCALE = 16`，即 1.0）——时域样本不带信道，这就是黄金值 |
| PBCH | QPSK 解调 + 解扰 → 512 LLR → polar SC(22 拍/节点) → `01110011110101011100101100000000`，与 `tools/ssb_plan.py` 的 MIB 逐位一致 |
| 逐 bin 自检 | `run-fftprobe`：4096 个 bin 与 `tools/fft_plan.py` 的定点参考**逐位一致** |
| 逐 PE 自检 | `run-ssbfft` 导出 127 个参考字 + 16 个 PE 累加器 + 254 个 rx 字 + spad 银行 8..23 + 引擎锁存的描述符，脚本用 16 位 MAC 逐 PE 重算，全部一致 |

接 PBCH 时又暴露一个平台级缺陷：**polar 节点的结果块会回绕复用**。SC 树一共产生
1120 个节点块（512 点的 9 层树，小节点每块只装 1 个 LLR），最初只开了 64 块
（`OUT_BLOCKS`），第 65 次起复用旧块，而引擎写 RAM **不使 CPU D$ 失效**，于是 CPU 读到
的是上一轮缓存行 —— 表现为 MIB 全错但 PSS/SSS 正常。现在节点结果固定放在
`0x8033_0000` 的 **2048 块**池里、单调递增不复用（`nblk` 超限才计错并回绕）。另一个小修正：
信道估计改成**四舍五入**（`div_round`），4 个符号都精确得到 16，而不是 15/16 混杂，那一处
1 位 MIB 误差也随之消失。

### 4.15 采样级 PSS 的"保真度缺口"：三个真实缺陷（都已修）

排查顺序是"先在纯 RTL 台架定论，再改 RTL，再回 SoC"，结论是 **RTL 的 2D 走位一直是对的**，
问题全在软件/端口：

| 现象 | 根因 | 修复 |
|---|---|---|
| 参考副本只填到偶数 bank 8/10/12/14，奇数 bank 全 0；`pe & 2` 的 8 个 PE 恒为 0 | **参考缓冲按 32 字跨度打包**（`pref[(t>>4)*32 + (t&15)]`），但引擎从 RAM 按**线性**读 127 字 —— `t=16..31` 正好落在打包的空洞（0），于是第 1、3、5、7 行被写进 0 | `pref[t] = ...`（线性），源布局与 `bank 8+r / word t%16` 的目标布局由 2D 走位负责 |
| 硬件上真候选只有模型的 ~1/7，噪声候选却吻合 | 同上（真候选是相干项，对少掉的 64 个 tap 最敏感；噪声项本来就不相干，看不出差别） | 同上 |
| 频谱与模型"完全不相关"（相位无规律） | `ssb_fft.inc` 移植时把输出顺序写成 `bitrev12`（**二进制**位反序），而 radix-4 DIF 引擎是**基-4 位反序**（`rev4`，6 个 2-bit 数字） | `.inc` 里改成 `rev4()`；`run-fftprobe` 逐 bin 比对定点参考，防止端口再次走样 |
| 相关幅度只有模型的 ~0.5 | 定点 FFT **归一化**（÷4096）且 `TD_SCALE=256`，所以 `<<4` 恰好抵消；剩余 0.5 是定点截断损失，只影响绝对幅度，不影响比值 | 文档记录：比值才是验收指标 |

**纯 RTL 台架的用法**（`sim/cgra/tb_cgra.cpp`）：`--dma-mem`（RAM 图样）、`--dma-ops`
（单发描述符，支持 `0x` 前缀与 `stride/row`）、`--ring-desc` + `--ring-base/--ring-size`
（把 32 字节描述符放进 RAM 由引擎自己取）、`--spad-dump`。用它把 `len=127, stride=32,
row=16` 的 2D 走位逐字回读，行块起点 256/288/320/…、每行 16 字、无丢字——**先证明 RTL
没错，才把注意力转回软件**。

顺带修掉一个真实的 RTL 边界缺陷：**环回绕判空**。`RING_TAIL` 可以写成 `ring_size`
（"最后一个的下一个"），它与 0 是同一个槽；原来的比较用原始值，head 回绕到 0 时永不等于
tail，引擎会把本批次的前几个描述符**再执行一遍**（台架里表现为已经搬好的 spad 又被旧描述符
用当时已被改写的 RAM 覆盖）。现在统一按 `ring_tail_n = (ring_tail >= ring_size) ? 0 :
ring_tail` 比较（`D_IDLE`、完成后推进 head、状态寄存器 0x58 三处）。

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

### 4.16 采样级链路的 SNR 瀑布（第一个"定量"指标）

前面所有验收都是"单工作点逐位一致"，缺一条性能曲线。加了两个东西：

- `tools/ssb_plan.py` 的 `build_td(snr_db=, seed=)`：在 4 个符号的时域样本上加复高斯白噪声
  （SNR 定义在整块 4 符号上，即接收机还不知道 SSB 在哪时的信噪比）；转储时先乘 `TD_SCALE`
  **再四舍五入**（原来先 `int()` 再乘会把噪声的亚 LSB 细节截掉；无噪声镜像逐字节不变，已用 `cmp` 验证）。
- `tools/sweep_snr.py --model|--hw`：同一批含噪样本分别走纯 Python 模型和 CGRA（`ssb_fft_bm.c`），
  输出 N_ID2/N_ID1 是否正确、32 位 MIB 的误比特数、以及硬件周期。
  硬件点与 `tools/check_ssb_fft.py` 用同一套结果字解码。

实测（模型 8 个 seed 平均；硬件每个 seed 一次完整仿真（~2.2 分钟）；下表是 §4.18
"导频去重叠 + 真实 DMRS `c_init`"之后的结果，硬件列已扩到 3~6 个 seed）：

| SNR (dB) | 模型 MIB 误比特（8 seed） | N_ID 正确 | 硬件 MIB 误比特（seed 数） | 硬件周期 |
|---|---|---|---|---|
| -8 | **0.00** | 8/8 | **0.00**（3） | 4,378,729 |
| -9 | 0.00 | 8/8 | — | — |
| -10 | 1.50 | 8/8 | **0.00**（3） | 4,378,736 |
| -11 | 3.50 | 8/8 | — | — |
| -12 | 5.38 | 8/8 | **7.17**（6；两组 3 seed 分别为 1.00 / 13.33） | 4,378,711…736 |
| -13 | 4.88 | 8/8 | — | — |
| -14 | 7.25 | 8/8 | **10.17**（6；两组分别为 13.33 / 7.00） | 4,378,713…743 |
| -16 | 11.75 | 8/8 | **15.67**（3，已接近随机 16/32） | 4,378,749 |

对照要点：
- N_ID2 / N_ID1 在 **-20…+8 dB** 全部正确（336 个 SSS 假设在 -20 dB 仍能选中真值，
  127 点相干积累给出约 21 dB 扩频增益）；本次硬件 21 次仿真（−16…−8 dB）也全部 21/21。
- **SSB 从 DC 移到 bin 480..719（§4.17）后，-12 dB 的误比特从 15.2（5 seed，旧布局）
  降到 5.0（8 seed，新布局）**，说明"SSB 压在 DC 上"确实在浪费性能；
- **导频不再覆盖 PSS/SSS（§4.18）后，-12 dB 的硬件误比特从 9.00 降到 1.50**（2 seed）：
  旧的 60 个导频里有 32 个落在 PSS/SSS 的 127 个 RE 上，把同步信号挖掉了；
- 同一 SNR 上硬件与模型的差异来自定点流水线（Q15 旋转因子、逐级移位、`>>LLR_SHIFT`、
  硬判决 SC）与浮点模型不同；
- **方差比均值更值得记录**：-12 dB 两组 3 seed 分别是 1.00 和 13.33，-14 dB 是 13.33 和 7.00。
  也就是说 3 个 seed 的点完全不可信（单点可能差 13 个 bit），**均值要 ≥20 seed 才稳**
  （每点约 45 分钟仿真，必须分批）。模型侧同理：−13 dB（4.88）低于 −12 dB（5.38）就是 8 seed 的抖动。

结论与后续：
- 这张表给出了"链路在什么 SNR 开始坏、坏在哪一层"的第一个量化答案；硬件曲线的形状与模型一致
  （−8/−10 全对 → −12 开始错 → −14 明显劣化 → −16 接近随机）；
- `tools/sweep_snr.py` 现在会打印**逐 seed 的误比特**，并支持 `--csv build/snr_curve.csv`
  追加记录（`snr,mode,seed0,seeds,nid2_ok,nid1_ok,mib_err,per_seed,cycles`），
  分批跑也不会丢结果：`--hw --snr -12 --seed 4 --seeds 3 --csv build/snr_curve.csv`；
- 要提高可用 SNR，最直接的两处是 (a) DMRS 只有 28 个导频做 LS 估计（可做时/频平滑），
  (b) Polar 目前是硬判决 SC（可上 SCL/CA-SCL，或先用 CRC 辅助列表）。

### 4.18 PBCH DMRS 的两项 38.211 保真（导频去重叠 + 真实 `c_init`）

原状：DMRS 与 PBCH 载荷共用一条 Gold 序列、`c_init = 3*N_ID1 + N_ID2`（601）。
38.211 §7.4.1.1 的 DMRS 初始化是
`c_init = 2^11*(i_ssb+1)*(N_ID_cell/4+1) + 2^6*(i_ssb+1) + (N_ID_cell mod 4)`，
与载荷扰码不同源。

**第 1 步：导频不能压在 PSS/SSS 上。** 原来 `pilot_res()` 是 `0,4,...,236`，
而 PSS/SSS 占 RE 56..182；两者重叠，后写的导频把同步信号挖掉了（模型与硬件都如此，
所以测试一直"两边一致但两边都错"）。改为"跳过 PSS/SSS 占用的 RE"：
`pilot_res() = [r for r in range(0, 240, 4) if r not in mid_res()]`（28 个导频），
硬件侧对应 `pilot_re(p) = p<14 ? 4p : 184 + 4(p-14)`，并且**数据 RE 只跳过真正的导频**
（`r%4==0 && !is_mid(r)`）——只改导频而不同时改数据列表，采样级链路会立刻对不上。

**第 2 步：DMRS 用真实 `c_init`（`i_ssb=2`）**，载荷扰码仍用 `N_ID_cell`（38.212 §7.1.2），
因此需要**两条独立 Gold 序列**：

| 位置 | 改动 |
|---|---|
| `tools/ssb_plan.py` | 新增 `pbch_dmrs_c_init(nid1, nid2, i_ssb=2)`；`dmrs_values()` 用它，接收端扰码仍用 `nid_hash()` |
| `sw/soc/ssb_bm.c` | `gold_fill()/gold_init2()/gold_bit2()` 第二条序列 + `dmrs_c_init()`；TX 与 RX 的导频都用 `gold_bit2`，数据仍用 `gold_bit` |
| `sw/soc/ssb_fft_bm.c` | 同上，第二条序列放在 `GOLD_BASE + 8*GOLD_LEN` 的 RAM 区 |

**过程中抓到的坑（已写进 `AGENTS.md` 第 13 条）**：把 `gold2_x1/x2` 直接声明成静态数组，
`.bss` 从 49 KB 涨到 66 KB，越过了 `0x8001_0000`——而 `run-ssb` 的**结果窗口就在那里**。
`.bss` 与结果字互相覆盖，症状极有迷惑性：CPU 读回的 y 网格（我临时把 8 个复样本转储到
`RESULT_BASE[2000..]`）与模型**逐点一致**，但 PSS 相关值从 251,723 缩到 26,882、SSS 判对但
度量只剩 8,637、信道估计变成 27,779。把第二条序列放到固定地址 `0x8006_0000`、`.bss` 回到
0x8000_d5e0 之后一切恢复。

验收（两条链都逐位通过）：
- `make -C sim/soc run-ssb`：N_ID1=200、N_ID2=1、H=(32,15)、1459 runs、2,411,649 周期；
- `make -C sim/soc run-ssbfft`：N_ID2=1、N_ID1=200、H=(16,0)×4、MIB 32/32、1907 runs、
  4,378,711 周期；
- `tools/check_ssb.py` 的 PSS/SSS 期望值改为**有符号**比较（原来 `expect_corr()` 返回
  unsigned、`got` 走 `sx()`，相关变负时误报"数值全错"）；
- `verify.sh quick` 5/5 PASS；SNR 瀑布（§4.16）同步更新，-12 dB 的硬件误比特 9.00 → 1.50。

剩下还没做的 38.211 细节：`i_ssb`/`n_hf` 的组合（Lmax=8 时的 3+3 bit 编码）、
DMRS 位置随 `N_ID_cell mod 4` 的频移（`v_shift`）以及 PBCH 的速率匹配/交织。

### 4.17 SSB 的真实子载波位置（38.211 保真 P1 第一步）

原来模型与硬件都假设 `RE r ↔ FFT bin r`（SSB 落在 DC 上，且贴着 FFT 边缘），
这是不可实现的：DC 会被收发端滤波/直流偏移吃掉，边缘也没有保护带。

改动（已逐位回归）：

| 位置 | 改动 |
|---|---|
| `tools/ssb_plan.py` | `SSB_K0 = 480`，`subcarriers() = {r: SSB_K0 + r}` —— SSB 的 240 个 RE 落在 bin 480..719，避开 DC 与两侧边缘 |
| `sw/soc/ssb_fft_bm.c` | 三处频谱抽取全改为 `rev4(SSB_K0 + …)`（PSS 符号 1、SSS 符号 3、四符号网格） |
| `tools/check_ssb_fft.py` / `sweep_snr.py` | 模型侧同步用 `sp.subcarriers()`，否则"模型 vs 硬件"的比值会失真 |

**过程中又抓到一个构建陷阱**：`build/ssb/td.bin` 的 make 规则没有依赖 `tools/ssb_plan.py`，
改模型后镜像不重建，硬件拿旧样本去读新频点 → PSS 度量掉到 239/159/191（噪声水平）、
SSS 判错、MIB 全 0。加依赖后一切恢复正常。这条已写进 `AGENTS.md` 的构建陷阱。

验收（`make -C sim/soc run-ssbfft`）：N_ID2=1、N_ID1=200、四个符号 H=(16,0)、
MIB 32/32、1907 次阵列运行、4,378,711 周期；`verify.sh quick` 5/5 PASS。

意义：链路不再"把 SSB 放在 DC"，为后续加 CP、加保护带、加载波偏移（真实 RF）留出了空间。

### 4.19 CP 与保护带：P1 真映射链（`run-ssb-fid`）

§4.17 只把 SSB 搬到 bin 480..719，仍是"无 CP、无速率匹配"的简化映射。P1 链按 38.211/38.212
把剩下的三件事做齐（模型 `tools/ssb_plan_p1.py`，硬件 `sw/soc/ssb_fid_bm.c`）：

| 项目 | 做法 | 模型/硬件对应 |
|---|---|---|
| **CP** | 每个符号 4384 个复样本（288 点 CP + 4096 点有用符号，CP 是符号尾部 288 点的拷贝）；接收端丢弃 CP 再做 4096 点 FFT | `nr_pbch.add_cp/strip_cp`；`spec[s] = td + s*SYM_WORDS + 2*CP_LEN` |
| **DC 空置（保护带）** | SSB RE r 映射到 bin `NFFT/2 - 24 + r`，RE 24 正好落在 DC（bin 2048）并被置零；它本身是 DMRS 网格点，所以只损失 1 个导频（72→71 个可用导频，PBCH 数据仍是 180 RE/符号×2 符号 = 720 bit） | `bin_of(r)` / `re_bin(r)` |
| **真 38.211 布局** | PSS 在符号 0、SSS 在符号 2（PBCH 在 1/3），DMRS `c_init` 用 `i_SSB=3`、`n_hf=0`（`ibar = i_SSB + 4·n_hf`） | `PBCH_SYMS=[1,3]`、`pbch_dmrs_cinit()` |
| **38.212 速率匹配** | 512 位 polar 码 → 子块交织 → 720 位循环缓冲（重复）→ 三角交织；接收端反向（三角解交织 → 循环累加 → 子块解交织），LUT 由 `tools/gen_luts.py` 生成 `tools/ssb_fid_luts.h` | `nr_pbch.rate_match_pbch/rate_recover_pbch`；C 里 `tri_perm`/`sb_perm` |

**跑通这条链时连着踩了三个坑**（都写进了 `AGENTS.md` 第 16 条）：

1. **FFT 跑错窗口**：写成 `fft4096(td + s*2*FFT_N)`，既从符号开头（含 CP）开始，又按 4096 点
   的步长跳（正确步长是 `CP_LEN + NFFT`）。症状：PSS 度量落到噪声、`N_ID2` 随机、SSS 判 18。
2. **只给网格加了 bin 偏移**：PSS/SSS 仍按 `rev4(NPSS_RE0 + t)` 取，落在噪声上。改成
   `rev4(re_bin(NPSS_RE0 + t))` 后 `N_ID2=1`、`N_ID1=200`、DMRS `c_init`=0x12E101 全部对上。
3. **Gold 序列只填了 512 个 bit**：PBCH 扰码要 720 个（`E = RM_LEN`），而 `gold_fill()` 按
   `NPOL + 1600` 生成。后 208 个 bit 恒为 0，两个循环缓冲副本被反向解扰 → LLR 相消：
   最终只错 2 个 MIB bit，但 512 个 LLR 里有 46 个掉到 ±64 —— 这个"几乎全对"的症状最容易
   误导人。改成按 `RM_LEN + 1600` 生成后 32/32。

另外修了检查器的两处小问题：`check_ssb_fid.py` 需要的 `pbch_dmrs_cinit` 在 `ssb_plan_p1`
里重新导出；`gen_luts.py` 的自检原来把 0/1 位当 LLR 比对（必然 MISMATCH），且**导入即写文件**，
现在改成 ±1 LLR 组成的真实组合校验 + `__main__` 保护。`$(SSBFID_TD)` 也补上了模型依赖
（就是 §4.17 里 td 不重建的老陷阱）。

验收（`make -C sim/soc run-ssb-fid`）：N_ID2=1、N_ID1=200、`i_SSB=3`/`n_hf=0`、
DMRS `c_init`=0x12E101（与模型一致）、两个 PBCH 符号 H=(16,0) 且符号一致、MIB **32/32**、
1907 次阵列运行、4,411,511 周期、0 内部错误；`verify.sh quick` 6/6 PASS（213 s）。

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
