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

## 2. 综合前先修的两处 RTL（行为不变）

slang 是严格 SV 前端，暴露了两处 Verilator 容忍的问题（`tools/verify.sh quick` 6/6 回归通过）：

1. `cgra_top.sv` 里 `dstate_q` 的 `always_ff` **先用了后声明的** `dstate_e`/`D_IDLE` → 把 typedef 提前；
2. `buf_page` 被 DMA FSM 的复位块和 host-write 块**同时驱动**（多驱动）→ 复位只保留在
   sequencer 块（原本注释就写着"handled by the sequencer block (single driver)"，是遗留的一行）。

## 3. 面积

综合结果（`cgra_top`，逻辑部分）：

| 项目 | 数值 |
|---|---|
| 标准单元面积 | **54,596 µm² ≈ 0.0546 mm²** |
| 其中时序单元（5,072 个 `DFFR_X1`） | 26,994 µm²（**49.4%**） |
| 组合逻辑 | 27,602 µm² |
| 单元总数 | 25,262 个 |
| 主要类型 | `DFFR_X1` 5,072、`MUX2_X1` 3,770、`AOI21_X1` 2,130、`INV_X1` 1,733、`AND4_X1` 1,516 |

**存储阵列没有算在上面。** RTL 里有两块推断存储：

| 阵列 | 大小 | 若换成 fakeRAM45 宏（LEF 面积） |
|---|---|---|
| 微码 IMEM `imem[8192]×32b` | 32 KiB（262,144 bit） | 8 × `fakeram45_1024x32`（152.19×107.8 µm）= **131,248 µm²** |
| 暂存 `spad` 16 bank × 64 word × 32b | 4 KiB（32,768 bit） | 16 × `fakeram45_64x32`（20.14×61.6 µm）= **19,856 µm²** |

合计存储 ≈ **151,104 µm² ≈ 0.151 mm²**，因此：

> **CGRA（4×4）总面积 ≈ 0.055 + 0.151 ≈ 0.21 mm²（Nangate45，估算）**
> 其中 72% 是存储，28% 是逻辑。每 PE 的逻辑成本 ≈ 54.6k/16 ≈ **3.4k µm²/PE**（含寄存器堆与 spad 地址译码）。

注意两点：

- IMEM 在 RTL 里是"8192×32 扁平数组、16 个并行读口"，如果按每个读口复制一份宏，面积会变成
  16 × 0.131 ≈ 2.1 mm²。真实实现应把它改成 **512 行 × 512 bit 的宽字存储**（一拍给出 16 个 PE
  的微码），或者干脆 16 个独立的 512×32 单口宏；这是 RTL 结构问题，不是工艺问题。
- spad 需要 16 bank 并行读写，所以是 16 个小宏而不是 1 个大宏；用寄存器实现约 32k 个 flop
  （≈0.17 mm²），比宏贵一倍多。

## 4. 频率

iSTA 报告（三个综合目标点：500/800/900 MHz，ABC 的 `-D` 也随之收紧）：

| 综合目标 | 关键路径 | 最差 setup slack | iSTA 可行频率 |
|---|---|---|---|
| 500 MHz | 1.244 ns（`dma_busy` ← DMA 突发长度计算） | **+0.715 ns** | 778 MHz |
| 800 MHz | 1.244 ns（同一路径） | −0.035 ns | 778 MHz |
| 900 MHz | 1.244 ns（同一路径） | −0.174 ns | 778 MHz |

结论：

- **本设计在 Nangate45 TT、无布线 RC 的条件下，Fmax ≈ 1/1.244 ns ≈ 800 MHz**；
  Yosys/ABC 在 800/900 MHz 目标下已经给出同样的映射（面积也完全不变），说明是**结构瓶颈**而不是
  优化力度不够。
- 关键路径在**拷贝引擎的突发长度逻辑**（`dma_wleft/dma_nb_raw/dma_4k/dma_nb` 一串 16 位加减比较，
  再驱动 `dma_busy/wr_status/dstate`）。想再往上跑，第一步应该把这段流水化（或预先算好每拍的
  burst 数放在小 FIFO 里），而不是继续加 ABC 的 effort。
- 这是**pre-layout（无 P&R、无时钟树、无线延迟）**估值。经验上综合后 800 MHz 在 45nm 上
  放到布线后会掉到 **500~650 MHz**；文档里引用时请写"~0.8 GHz 综合后 / ~0.5–0.65 GHz 布局后"。

## 5. 功耗

iSTA `iPA`，toggle rate 0.1，仅逻辑（不含存储）：

| 频率 | 总功耗 |
|---|---|
| 500 MHz | **24.6 mW** |
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
按 0.21 mm² 估算，**面积效率 ≈ 60 GOPS/mm²（45 nm，含存储）**——这是本项目第一个可对外的
PPA 指标。

## 7. 已知局限（引用数字时必须带上）

1. **Nangate45 是教学/开源库**（密度远低于商用 28/16 nm；同样逻辑在 28 nm 大约小 3~4 倍）。
2. **pre-layout**：没有布局布线、没有时钟树、没有线延迟与串扰；频率会掉、面积会涨 ~20~40%。
3. **存储是估算**：用的是 fakeRAM45 宏的 LEF 面积，不是综合出来的；且 IMEM 的真实映射方式
   取决于是否重构 RTL（见 §3）。
4. **功耗**用默认 toggle 0.1、无 VCD 驱动；真实业务下需用仿真波形反标（`iPA` 支持）。
5. 只综合了 **CGRA**；SoC 顶层（CVA6 + AXI + UART/PLIC/CLINT + 64 MiB 行为 RAM）未包含。
   CVA6 属于第三方 IP，综合一次需要半小时级并要先处理其缓存/行为存储，建议单独一次任务做。
