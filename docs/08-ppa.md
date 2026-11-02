# 08 — PPA：CGRA 综合面积 / 频率 / 功耗（Nangate45）

> 目的：把 `rtl/cgra` 真正综合到标准单元，拿到**面积（µm²）、频率（MHz）、功耗（mW）**，
> 而不是继续用"周期数 × 假设频率"来估性能。全部开源工具链，可在本机复现。

## 1. 流程与工具

| 环节 | 工具/库 | 说明 |
|---|---|---|
| SV 前端 | `yosys -m slang`（oss-cad-suite 自带 `slang.so`） | 我们的 RTL 是 SystemVerilog（`package cgra_pkg` + `import cgra_pkg::*;`），Yosys 自带前端不支持通配 import，必须用 slang |
| 综合 | Yosys 0.52 `synth -flatten` + `dfflibmap` + `abc -D <ps>` | ABC 脚本沿用 `~/yosys-sta` 的 `ifraig/retime/dch/map` 配方 |
| 工艺库 | Nangate45（45 nm 开源标准单元，`~/yosys-sta/pdk/nangate45`） | TT corner：`Nangate45_typ.lib`；`DFFR_X1` = 5.32 µm²，`MUX2_X1` = 1.862 µm² |
| 时序/功耗 | iEDA `iSTA`/`iPA`（`~/yosys-sta/bin/iEDA`） | SDC 只约束一个时钟（`clk`），无 I/O delay；功耗 toggle 0.1（工具默认） |

复现：

```bash
env -u LD_LIBRARY_PATH make -C syn syn CLK_FREQ_MHZ=500   # 综合（~85 s）
env -u LD_LIBRARY_PATH make -C syn sta CLK_FREQ_MHZ=500   # 综合 + STA/功耗
# 结果在 build/syn/cgra_top-<freq>MHz/：synth_stat.txt / cgra_top.rpt / cgra_top.pwr
```

流程文件：`syn/Makefile`、`syn/cgra_syn.tcl`、`syn/cgra_top.sdc`。

## 2. 综合前先修的 RTL（行为不变，`verify.sh quick` 6/6 逐拍回归）

slang 是严格 SV 前端，暴露了两处 Verilator 容忍的问题：

1. `cgra_top.sv` 里 `dstate_q` 的 `always_ff` **先用了后声明的** `dstate_e`/`D_IDLE` → 把 typedef 提前；
2. `buf_page` 被 DMA FSM 的复位块和 host-write 块**同时驱动**（多驱动）→ 复位只保留在
   sequencer 块（原本注释就写着"handled by the sequencer block (single driver)"，是遗留的一行）。

第三处是 §3.1 的 **IMEM 重构**（方案 B）：扁平 8192×32 → 512×512 宽字 + 同步读 + 一级预取，
所有 benchmark 的周期数与结果**逐位不变**。

## 3. 面积

综合结果（`cgra_top`，逻辑部分）：

| 项目 | 数值 |
|---|---|
| 标准单元面积 | **54,463 µm² ≈ 0.0545 mm²** |
| 其中时序单元（5,072 个 `DFFR_X1`） | 26,994 µm²（**49.6%**） |
| 组合逻辑 | 27,469 µm² |
| 单元总数 | 25,216 个 |
| 主要类型 | `DFFR_X1` 5,072、`MUX2_X1` ~3.7k、`AOI21_X1` 2,130、`INV_X1` 1,733、`AND4_X1` 1,516 |

**存储阵列没有算在上面。** RTL 里有两块推断存储：

| 阵列 | 大小 | 若换成 fakeRAM45 宏（LEF 面积） |
|---|---|---|
| 微码 IMEM（重构后：512 行 × 512 bit 宽字，1R1W） | 32 KiB（262,144 bit） | 8 × `fakeram45_512x64`（152.57×113.4 µm）= **138,408 µm²** |
| 暂存 `spad` 64 bank × 32 word × 32b（含 2 page） | 8 KiB（65,536 bit） | 若做成 1R1W bank：64 × `fakeram45_64x32`（20.14×61.6 µm）= **79,424 µm²** |

**但 spad 实际上做不成 1R1W**（现有 kernel 就需要同 bank 2 读/2 写，见 §3.2）：
按现状的全交叉开关实现，它要 flop + 多路选择，约 **2.6 mm²**，是最大的单项。因此：

| 口径 | 逻辑 | IMEM | spad | 合计 |
|---|---|---|---|---|
| spad 保持全交叉开关（现状 RTL） | 0.0545 | 0.138 | ~2.6 | **≈2.8 mm²** |
| spad 做成 2R2W bank（§3.2 方案 b） | 0.0545 | 0.138 | ~0.16 | **≈0.35 mm²** |
| spad 做成 1R1W + ISA 约束（§3.2 方案 c） | 0.0545 | 0.138 | 0.079 | **≈0.27 mm²** |

每 PE 的逻辑成本 ≈ 54.5k/16 ≈ **3.4k µm²/PE**（含寄存器堆与 spad 地址译码）。
注意：本文早先版本写的"≈0.21 mm²"是最乐观口径（spad 当成理想单口宏），**对外请用上面的区间**。

### 3.1 IMEM 重构（方案 B）：2.10 mm² → 0.138 mm²

原来的 RTL 是"8192×32 扁平数组"，而阵列每拍要读同一行 16 个 PE 的微码 → **16 个并行读口**。
存储编译器没有 16 读口选项，只能把整块复制 16 份：

| 结构 | Yosys 推断出的存储 | 物理实现 | 面积 |
|---|---|---|---|
| 旧：扁平 8192×32 | 8192×32，16R1W | 16 份复制 ×（8 × `1024x32`） | **2.10 mm²** |
| 新：宽字 512×512 + 预取 | **512×512，1R1W（写掩码）** | 8 × `fakeram45_512x64` | **0.138 mm²** |

改动（行为逐位不变，`verify.sh quick` 6/6、所有周期数与原来完全一致）：

1. 存储改成 `logic [NPES-1:0][31:0] imem [0:511]` —— 一拍给出整行 16 个微码字；
2. 主机写一个字 = 对 512 bit 里 32 bit 的**掩码写**（`WR_EN` 位掩码），仍然是**一个写口**；
3. 读改成**同步 SRAM + 一级预取**：`imem_raddr = busy ? cycle_cnt+1 : 0`，
   空闲时地址停在行 0，`busy` 上升的那一拍正好把第 0 行打进 `inst_flat`，所以一次
   `NUM_CYCLES` 运行**仍是 num_cycles 拍**，不多也不少（checker 逐拍核对，全部原值）；
4. 去掉主机回读口（原来 `rd_data = imem[...]` 会变成第 2 个读口，1R2W）。**没有任何软件回读微码**
   （`grep` 全部 `sw/` 只有写），因此该地址段现在返回 0；要回读就得用 2R1W 宏（约 1.5× 面积）。

Yosys 实际推断（`dump t:$mem_v2`）：`imem` SIZE=512、WIDTH=512、**RD_PORTS=1、WR_PORTS=1**，
两个端口都是时钟沿同步 —— 这正是存储编译器能做的 1R1W 宏。

**还没解决的同类问题（见 §3.2）**：`spad` 被推断成 SIZE=64、WIDTH=1024、**19 个读口、1216 个写口**
（64 个 bank 每 bank 19 读/19 写 = 全交叉开关），这部分**完全不在 54.5k µm² 里**（综合时存储被删掉了）。

## 3.2 spad（8 KiB 暂存）为什么不能简单"bank 化"

先把尺寸纠正过来：`SPAD_BANKS=64`、`SPAD_WORDS=32`、`SPAD_BYTES = 64×32×4 = 8 KiB`
（64 个 bank 已经含 2 个 page，即 2 page × 32 bank）。之前文档里写的"4 KiB / 16 bank"是错的。

Yosys 的实际推断（`dump t:$mem_v2`）：`u_array.spad` = **64 行 × 1024 bit**，
`RD_PORTS=19`、`WR_PORTS=1216`（= 64 bank × 19 写口）。也就是说，硬件上要求
**每个 bank 都能被任意 16 个 PE + 2 个 DMA 端口 + host 同时访问** —— 这是一个全交叉开关寄存器堆，
存储编译器做不出来，只能落到 flop + 多路选择。

于是我们加了一个临时探针（已移除），在真实 benchmark 里统计"同一个 bank 有多少个**不同字**的并发访问"：

| benchmark | 每 bank 峰值不同字读 | 每 bank 峰值不同字写 |
|---|---|---|
| `run-fft` | **16** | **16** |
| `run-ssb` | ≥2（首现 cycle 2） | ≥2（首现 cycle 17646） |

**16/16 不是意外，而是 kernel 的数据布局决定的**：FFT 的微码把"一组 PE 的一条向量"整条放进
**同一个 bank**（`bank = 组号`，`word = 向量元素号`），这样一次蝶形所有 PE 都在同一 bank 的
不同字上读写 —— 这正是"bank 内向量化"的设计意图。所以每个 bank 天生就是 16 读 + 16 写的交叉开关。

### 实测反证：把 bank 改成 2R2W 会直接算错

按方案 b1 实现过一版（64 个 `cgra_spad_bank`，每 bank 2R2W、异步读、PE/DMA/host 优先级映射），
结果：

| 测试 | 结果 |
|---|---|
| `run-ssb` | 通过（恰好没触发 3 路以上冲突） |
| `kernels` | **FAIL**：polar_enc 16 处、polar_node 28 处、fft_stage 8、fft_block 8 |
| `run-fft` | **FAIL**（9 处不一致） |
| `run-ssbfft` | **FAIL**（MIB 全 0） |
| `run-ssb-fid` | **FAIL**（1 个内部错误） |

已整体回退（`cgra_array.sv` 回到已验证版本，`verify.sh quick` 6/6 绿；探针也移除）。
这条负结果说明：**只改存储结构不够**，必须连 kernel 的 bank 映射一起改。

### 可行路径与面积

| 方案 | 说明 | 估算面积 | 代价 |
|---|---|---|---|
| (a) 保持现状：每 bank 16R16W 交叉开关 | flop + 多路选择 | **~2.6–2.9 mm²** | 无改动 |
| (b) 先把**数据布局**改掉（bank = 元素号 mod 16） | kernel 仍用 2R2W bank（甚至 1R1W） | **0.16 mm²（2R2W）/ 0.079 mm²（1R1W）** | 改 `tools/cgra.py` 里所有 kernel 的地址常量 + `ssb_fft.inc` + `sw/soc/*` 的预装布局 + 对应黄金模型（机械但量大） |
| (c) 跨 lane 数据走 link 网络 | 减少 bank 读口（PE 之间本来有 N/S/E/W link） | 介于两者 | 需要给 kernel 加"从邻 PE 取数"的调度，改动同样不小 |

写交叉开关的量级估计：2048 个 word、每个 32 bit 要从最多 19 个写源里选 → 每 word ≈ 19:1 × 32 bit
≈ 1.07k µm²，×2048 ≈ **2.2 mm²**，这是现状面积的最大单项，比 IMEM（0.138 mm²）还大一个量级。

### 因此：对外引用面积时要写清是哪种 spad

| 口径 | 逻辑 | IMEM | spad | 合计 |
|---|---|---|---|---|
| 现状（spad 全交叉开关，flop） | 0.0545 | 0.138 | ~2.6 | **≈2.8 mm²** |
| 改数据布局后（2R2W bank） | 0.0545 | 0.138 | ~0.16 | **≈0.35 mm²** |
| 改数据布局 + 1R1W bank | 0.0545 | 0.138 | 0.079 | **≈0.27 mm²** |

之前正文里写的"≈0.21 mm²"对应的是**最乐观口径（spad 理想单口宏 + 布局已改）**，应改成
"**现状约 2.8 mm²；若要 0.27–0.35 mm²，必须先做 §3.2 的 kernel 数据布局重排**"。

**结论**：spad 的 bank 化不是"改 RTL 40 行"就能拿到的收益，它和 FFT/corr 的向量化布局绑在一起，
属于 M3 级（架构+软件）改动。现在保留交叉开关并在文档里如实标注 2.8 mm² 更诚实；
如果后续要做，第一步是**改数据布局**（把向量的 16 个元素摊到 16 个 bank），而不是改存储结构。
（本轮的 2R2W 尝试已完整回退，没有留在仓库里；上面那张失败表就是"为什么不能只改 RTL"的证据。）

## 3.3 第二轮尝试：硬件地址加扰（不改软件）+ 4R3W bank

思路：软件地址完全不动，只在硬件里做一次**双射**重排：

```
logical address  = {page, bank[4:0], word[4:0]}      (软件一直用的 bank*32+word)
physical bank    = {page, bank[4:0] ^ word[4:0]}     physical word = word
```

因为读写都走同一个函数，功能完全等价（对全部 kernel/预装/回读/黄金模型透明），但它把
"同一 bank 的 16 个不同字"摊到了 16 个物理 bank。实测（探针，`run-fft`）：

| | 加扰前 | 加扰后 |
|---|---|---|
| 每 bank 峰值不同字读 | 16 | **3** |
| 每 bank 峰值不同字写 | 16 | **2** |

于是把 spad 做成 64 个 `cgra_spad_bank`（32×32，**4 读 3 写**、异步读、PE/DMA/host 优先级映射）。
过程里还修了一个真 bug：读口必须按"**不同字**"分配，否则两个 PE 读同一个字会把第三个 PE 的口抢掉。
修完后：

- `kernels`（13 个）**ALL TESTS PASSED**；
- `run-ring` / `run-fft` / `run-ssbfft` / `run-ssb-fid` **全部通过**（结果与周期逐位不变）；
- **`run-rxchain` 仍失败**（3 处：slot1 PE4/PE5 的 min1/min2 读回 0），原因尚未定位
  （怀疑与 slot 调度下"阵列运行中 host 回读 / LDPC-CN 的 PE 读口路由"有关；该 benchmark 的
  峰值本来只有 1R/1W，所以不是端口不够）。

**状态：本轮已整体回退**（`cgra_array.sv` 回到已验证版本，`verify.sh quick` 6/6 绿），
`cgra_spad_bank.sv` 与文件列表改动也一并撤销。上面的数字和结论保留在这里，供下一轮直接续做：
从 `run-rxchain` 的 3 处不一致入手，定位后再落地。

若这套方案最终跑通，spad 面积从 ~2.6 mm² 降到约 **0.9–1.0 mm²**（flop 0.35 + 4 个读口 ~0.24 +
3 个写口 ~0.21 + 回读交叉 ~0.12），CGRA 合计从 ~2.8 mm² 降到 **~1.1–1.2 mm²**（2.3×），
而**软件、微码、黄金模型一行都不用改**。

## 4. 频率

iSTA 报告（三个综合目标点：500/800/900 MHz，ABC 的 `-D` 也随之收紧）：

| 综合目标 | 关键路径 | 最差 setup slack | iSTA 可行频率 |
|---|---|---|---|
| 500 MHz | 1.226 ns（`wr_status` ← DMA 突发长度计算） | **+0.729 ns** | 787 MHz |
| 800 MHz | 1.230 ns（同一路径） | −0.017 ns | 789 MHz |
| 900 MHz | 1.230 ns（同一路径） | −0.156 ns | 789 MHz |

结论：

- **本设计在 Nangate45 TT、无布线 RC 的条件下，Fmax ≈ 1/1.23 ns ≈ 810 MHz**；
  Yosys/ABC 在 800/900 MHz 目标下已经给出同样的映射（面积也完全不变），说明是**结构瓶颈**而不是
  优化力度不够。
- 关键路径在**拷贝引擎的突发长度逻辑**（`dma_wleft/dma_nb_raw/dma_4k/dma_nb` 一串 16 位加减比较，
  再驱动 `dma_busy/wr_status/dstate`）。想再往上跑，第一步应该把这段流水化（或预先算好每拍的
  burst 数放在小 FIFO 里），而不是继续加 ABC 的 effort。
- 这是**pre-layout（无 P&R、无时钟树、无线延迟）**估值。经验上综合后 810 MHz 在 45nm 上
  放到布线后会掉到 **500~650 MHz**；文档里引用时请写"~0.8 GHz 综合后 / ~0.5–0.65 GHz 布局后"。

## 5. 功耗

iSTA `iPA`，toggle rate 0.1，仅逻辑（不含存储）：

| 频率 | 总功耗 |
|---|---|
| 500 MHz | **24.5 mW** |
| 800 MHz | 29.5 mW |
| 900 MHz | 31.2 mW |

功耗随频率增长很缓（VDD 固定 1.1 V、无电压缩放），说明主要是**静态/内部功耗**；
4×4 阵列本身的翻转不大，剩下的主要来自 IMEM 读口译码与拷贝引擎——把 IMEM 换成宏之后
这部分会明显下降（宏的读出功耗远低于 4096:1 的 flop 多路选择）。

## 6. 这些数字意味着什么（折算到链路性能）

以 SSB 采样级链路为例（`run-ssbfft`：4,378,711 周期/块）：

| 频率 | 一块 SSB 的耗时 | 对 20 ms slot 的余量 |
|---|---|---|
| 500 MHz | 8.76 ms | 2.3× |
| 800 MHz | 5.47 ms | 3.7× |

阵列峰值算力（16 个 int16 MAC）：500 MHz → 8 GMAC/s；800 MHz → 12.8 GMAC/s（≈25.6 GOPS int16）。
按 0.35 mm²（spad 做成 2R2W bank 的口径）估算，**面积效率 ≈ 73 GOPS/mm²（45 nm，含存储）**；
若不改 spad 结构（~2.8 mm²）则只有 ~9 GOPS/mm²。这是本项目第一个可对外的 PPA 指标。

## 7. 已知局限（引用数字时必须带上）

1. **Nangate45 是 45 nm 的真实商用单元库**（Nangate 出品，OpenROAD/iEDA 的参考流程都用它），
   不是"玩具模型"；局限在于 45 nm 这个节点偏老——同逻辑在 28 nm 约小 2 倍、16 nm 约小 3~4 倍。
2. **pre-layout**：没有布局布线、没有时钟树、没有线延迟与串扰；频率会掉、面积会涨 ~20~40%。
3. **存储是估算**：用的是 fakeRAM45 宏的 LEF 面积，不是综合出来的；且 IMEM 的真实映射方式
   取决于是否重构 RTL（见 §3）。
4. **功耗**用默认 toggle 0.1、无 VCD 驱动；真实业务下需用仿真波形反标（`iPA` 支持）。
5. 只综合了 **CGRA**；SoC 顶层（CVA6 + AXI + UART/PLIC/CLINT + 64 MiB 行为 RAM）未包含。
   CVA6 属于第三方 IP，综合一次需要半小时级并要先处理其缓存/行为存储，建议单独一次任务做。

## 8. 能不能换更"真实"的工艺库？（先说清选项）

| 库 | 节点 | 是否免费可下载 | 现状 |
|---|---|---|---|
| **Nangate45**（本流程在用） | 45 nm bulk | ✅ 已在本机（`~/yosys-sta/pdk`） | 真实商用单元库，学术/OpenROAD 事实标准 |
| **ASAP7** | 7 nm FinFET（预测性 PDK） | ⚠️ 需在 ASU 页面接受 license 后下载 | 公共 GitHub 仓库只放了 PDK 骨架，**标准单元库子模块是空的**（`asap7sc7p5t_28` 拉下来没有任何 `.lib`） |
| **Sky130 / GF180** | 130 nm / 180 nm | ✅ Apache-2.0 | 真硅验证，但**节点比 45 nm 更老**，面积约 2~4×、频率更低；本机当前网络到 GitHub 被拒（`port 443 Connection refused`），下载没成功 |
| 商用 PDK（TSMC 28/16/7、GF22FDX…） | 28 nm 及以下 | ❌ 需 NDA | 不可能在本机拿到 |

结论：**离线可用的"最真实"就是 Nangate45**；想再上一个台阶只有两条路——
① 拿到 ASAP7 的 license 后把 `asap7sc7p5t_28` 的 liberty 放进 `~/pdk/asap7`；
② 网络恢复后装 sky130（会得到一个"真硅但老节点"的对照点）。
接入新库需要改三样东西：liberty 路径、TIE 单元名（Nangate45 是 `LOGIC1_X1/LOGIC0_X1`，
sky130 是 `conb_1` 的 `HI/LO`）、以及给 iSTA 一份新的 pdk tcl（tech LEF + stdcell LEF）。

### 45 nm → 现代节点的**估算**（不是实测，别当结论引用）

按业界常用的密度/时序缩放经验值（同一 RTL、无架构改动）：

| 节点 | 逻辑面积（实测 0.0545 mm² 折算） | 存储（IMEM 0.138 + spad 0.16 折算） | Fmax（实测 0.81 GHz 折算） |
|---|---|---|---|
| 45 nm（实测） | **0.0545 mm²** | **~0.30 mm²** | **~0.81 GHz** |
| 28 nm | ~0.028 mm² | ~0.15 mm² | ~1.1 GHz |
| 16 nm | ~0.016 mm² | ~0.09 mm² | ~1.5 GHz |
| 7 nm | ~0.008 mm² | ~0.045 mm² | ~2.0 GHz |

折算后总面积约 **0.18 / 0.11 / 0.05 mm²**（28/16/7 nm），面积效率从 ~73 GOPS/mm² 升到
~140 / 230 / 510 GOPS/mm²。**这些倍数只是规划用，误差可达 ±40%**，正式对外请写
"45 nm 实测 + 按节点折算估计"。
