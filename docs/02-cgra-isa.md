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
| `0x0004` | W | `NUM_CYCLES`（≤ 512，超出被截断） |
| `0x0008` | R | `CYCLE_COUNT` |
| `0x000C` | R | `ID` = `{0xCA, ISA_VER=0x01, N, N}` |
| `0x1000 + pe*32 + r*4` | RW | PE 寄存器 `r0..r7`（预装载 / 回读） |
| `0x1200 + pe*4` | RW | PE 累加器 |
| `0x2000 + idx*4` | RW | 指令存储器，`idx = cycle*NPES + pe`（8192 words） |

- PE 编号：`pe = row*N + col`（行优先，`(0,0)` 为左上）。
- AXI 写为单 outstanding；`wr_en` 在总线握手后一拍产生脉冲。
- 写 `CTRL.start` 前需保证 `NUM_CYCLES != 0`，且阵列空闲。
- 运行中读 PE 窗口返回当前值（调试用）；建议仅在 idle 时预装载。

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
3. 无 DMA/共享存储，数据入口只有单一边界流端口；
4. 结果归约（如 FIR 的空间求和、FFT 蝶形跨距 2/4/8）由软件多拍实现，效率低；
5. 单发射、无谓词/无循环硬件。

---

## 6. 演进方向（M1–M3）

| 能力 | 设计要点 |
|---|---|
| 8×8 / 16×16 | `CGRA_N` 参数化；分层互连（簇内全连、簇间 2 跳） |
| scratchpad | 每 PE 64×32b（双口），支持 `LD/ST imm` 与邻 PE 共享 |
| 双发射 | 64b 指令：1 算术 + 1 访存/路由 |
| 谓词与循环 | `pred` 位 + `loop_start/loop_end` 硬件计数 |
| DMA | 描述符环 + 双缓冲，AXI4 主口 |
| 复数支持 | `CMAC`：一次完成 (ar·br − ai·bi, ar·bi + ai·br) |
| FFT 专用 | 蝶形跨距路由网络（stage 可配置） |
| 编译 | MLIR dialect（`cgra`）→ 布局布线 → 微码二进制 |
