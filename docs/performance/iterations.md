# 软硬件优化迭代记录

按提交记录 CGRA 相关软硬件优化的可复现性能。迭代范围包括 RTL、微码/调度、
DMA/描述符、阶段编排、驱动与运行时、编译器/落点调整。除特别说明外，所有数字
来自同一台 Verilator 模型、同一个 wlbench bundle：

```
/home/zzq/wlbench/build/bundles/nr_pdsch
clock_hz = 1,053,000,000   (backend/gpcp/clocks.json，pre-layout 综合)
```

## 口径

| 指标 | 定义 | 来源 |
|---|---|---|
| `commit` | 产生该组数字的代码提交 | `git rev-parse --short HEAD` |
| wlbench 总周期 | `metrics.json` 的 `total_cycles` | `backend/gpcp/run.py --backend gpcp-cgra` |
| 阶段覆盖率 | 阵列阶段数 / 总阶段数 | `tools/cgra_coverage.py` |
| 工作量覆盖率 | Σ(阵列阶段 CPU 周期) / Σ(全部阶段 CPU 周期) | 同上 |
| 有效覆盖率 | Σ max(0, CPU周期 − CGRA周期) / Σ CPU周期 | 同上 |
| 利用率 | `PERF_ARRAY_BUSY / PERF_CYCLES` | 硬件性能计数器（全跑） |
| CGRA/DMA/CPU 占比 | 互斥账本 `PERF_STATE_*`，三者之和 = `PERF_CYCLES` | 同上 |

计数器索引见 `sw/include/perf.h`；整跑快照在结果区 1420（ITER-0 在 1400）。
**注意**：早期文档的 7.03% 用的是软件变量 `busy`，它只累计了译码阶段的
216,352 拍；硬件 `PERF_ARRAY_BUSY` 还包含 `ofdm_modulate` 和 `transform`
两个阵列阶段，全跑应为 388,384 拍。本文件统一采用硬件计数器口径。

## 迭代总表

| 迭代 | commit | 变更 | wlbench 总周期 | 相对基线 | 阵列忙 | 利用率 | 阶段覆盖 | 工作量覆盖 | 有效覆盖 |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| ITER-0 | `da434fd` | 基线（LDPC 微码逐字 MMIO 装载） | 3,077,988 | — | 388,384 | 12.62% | 25.0% | 96.8% | 84.8% |
| ITER-1 | `d082938` | LDPC 微码改走拷贝引擎（DMA） | **2,649,155** | **−13.9%** | 388,384 | **14.66%** | 25.0% | 96.8% | 84.8% |
| ITER-2 | `84f2f7b` | IMEM 双 bank（CN/BN 常驻）+ 删环路径死初始化 | 2,482,309 | −19.3% | 388,384 | 15.65% | 25.0% | 96.8% | 84.8% |
| ITER-3 | `56df22b` | `build_tables` 只建一次（encode 已建过） | **2,351,954** | **−23.6%** | 388,384 | **16.52%** | 25.0% | 96.8% | 84.8% |
| ITER-4 | `9251a8a` | RTL 单驱动整理（为综合入口）+ PPA 双 PDK 复测 | 2,351,954 | −23.6% | 388,384 | 16.52% | 25.0% | 96.8% | 84.8% |

覆盖率来自 CPU-only 参考（`tools/cgra_coverage.py`）：阵列阶段 3/12，
CPU 参考总计 23,714,242 拍，CGRA 总计 3,622,982 拍，阵列加速比 8.06×，
端到端加速比 6.55×。

## CPU / DMA / CGRA 工作周期占比

互斥账本（每拍只归一类，`PERF_CYCLES` 为快照到的周期数，略小于 wlbench
`total_cycles`，差值来自启动/收尾）。

| 迭代 | PERF_CYCLES | CGRA 忙 | DMA 忙 | CPU |
|---|---:|---:|---:|---:|
| ITER-0 | 3,077,435 | 388,384 (12.62%) | 540,484 (17.56%) | 2,149,071 (69.83%) |
| ITER-1 | 2,648,604 | 388,384 (14.66%) | 590,726 (22.30%) | 1,669,998 (63.05%) |
| ITER-2 | 2,481,746 | 388,384 (15.65%) | 546,764 (22.03%) | 1,547,114 (62.34%) |
| ITER-3 | 2,351,394 | 388,384 (16.52%) | 546,143 (23.22%) | 1,417,378 (60.27%) |

## 性能计数器

| # | 计数器 | ITER-0 | ITER-1 | ITER-2 | ITER-3 | 变化（2→3） |
|---:|---|---:|---:|---:|---:|---:|
| 0 | `PERF_CYCLES` | 3,077,435 | 2,648,604 | 2,481,746 | 2,351,394 | −130,352 |
| 1 | `PERF_ARRAY_BUSY` | 388,384 | 388,384 | 388,384 | 388,384 | 0 |
| 2 | `PERF_ARRAY_RUNS` | 1,200 | 1,200 | 1,200 | 1,200 | 0 |
| 3 | `PERF_INST_ISSUED` | 6,214,144 | 6,214,144 | 6,214,144 | 6,214,144 | 0 |
| 4 | `PERF_INST_NOP` | 1,282,176 | 1,282,176 | 1,282,176 | 1,282,176 | 0 |
| 5 | `PERF_INST_MAC` | 0 | 0 | 0 | 0 | 0 |
| 6 | `PERF_INST_MUL` | 245,760 | 245,760 | 245,760 | 245,760 | 0 |
| 7 | `PERF_INST_LD` | 1,265,152 | 1,265,152 | 1,265,152 | 1,265,152 | 0 |
| 8 | `PERF_INST_ST` | 491,968 | 491,968 | 491,968 | 491,968 | 0 |
| 9 | `PERF_INST_ALU` | 2,929,088 | 2,929,088 | 2,929,088 | 2,929,088 | 0 |
| 10 | `PERF_SPAD_READ` | 1,265,152 | 1,265,152 | 1,265,152 | 1,265,152 | 0 |
| 11 | `PERF_SPAD_WRITE` | 491,968 | 491,968 | 491,968 | 491,968 | 0 |
| 12 | `PERF_SPAD_BANK_CONFLICT` | 46,576 | 46,576 | 46,576 | 46,576 | 0 |
| 13 | `PERF_SPAD_WORD_CONFLICT` | 0 | 0 | 0 | 0 | 0 |
| 14 | `PERF_DMA_BUSY`（可与阵列重叠） | 756,836 | 807,078 | 763,116 | 762,495 | −621 |
| 15 | `PERF_DMA_BEATS` | 381,840 | 420,688 | 386,696 | 386,696 | 0 |
| 16 | `PERF_DMA_AR_WAIT` | 4,710 | 5,355 | 4,802 | 4,507 | −295 |
| 17 | `PERF_DMA_AW_WAIT` | 3,739 | 3,659 | 3,737 | 4,126 | +389 |
| 18 | `PERF_DMA_B_WAIT` | 0 | 0 | 0 | 0 | 0 |
| 19 | `PERF_DMA_DATA_WAIT` | 89,675 | 99,216 | 90,847 | 90,135 | −712 |
| 20 | `PERF_DMA_STARTS` | 964 | 1,052 | 975 | 975 | 0 |
| 21 | `PERF_RING_DONE` | 7,152 | 7,152 | 7,152 | 7,152 | 0 |
| 54 | `PERF_STATE_DMA` | 540,484 | 590,726 | 546,764 | 546,143 | −621 |
| 55 | `PERF_STATE_DMA_WAIT` | 98,124 | 108,230 | 99,386 | 98,768 | −618 |
| 56 | `PERF_STATE_CPU` | 2,149,071 | 1,669,998 | 1,547,114 | 1,417,378 | −129,736 |

指令级计数（issued / nop / ld / st / alu / spad / bank conflict）在四个迭代里
完全不变：微码与数据搬运量没有变化，变的只是装载次数和 CPU 侧的初始化。
ITER-2 的 DMA beats 反而少 33,992，因为 IMEM 装载从 16 次降到 2 次。

## 分阶段

| 阶段 | ITER-0 墙钟 | ITER-1 墙钟 | ITER-2 墙钟 | ITER-3 墙钟 | 阵列忙 | DMA 忙（3） | CPU（3） |
|---|---:|---:|---:|---:|---:|---:|---:|
| ofdm_modulate | 483,033 | 482,992 | 482,863 | 483,741 | 86,016 | 150,911 | 246,960 |
| transform | 312,308 | 312,340 | 312,340 | 313,209 | 86,016 | 150,898 | 76,436 |
| decode | 1,510,723 | 1,082,145 | 913,031 | **792,128** | 216,352 | 244,334 | 331,583 |
| 其余（标量） | 752,801 | 752,766 | 755,109 | 743,935 | 0 | 0 | 743,935 |

ITER-0 的 decode DMA/CPU 分项无效：当时全量快照写在 1400，12 个阶段 ×17 字
一直写到 1407，覆盖了最后一阶段的 DMA/CPU 字段。ITER-1 把快照移到 1420 后
该分项才可用；因此 ITER-0 只记录 decode 的墙钟、阵列忙和 DMA beats。

## ITER-1 说明

- **改动**：`sw/soc/stages/pdsch_stages.inc` 的 `qs_imem_host_load()` 逐字
  AXI-Lite 写换成 `gpcp_cgra_imem_load()`（拷贝引擎，8 B/beat）。每轮译码装载
  CN 7,232 字 + BN 2,480 字，8 轮共 16 次，原路径每字约 4 拍。
- **收益**：总周期 −428,831；CPU 状态 −479,073；译码阶段 1,510,723 →
  1,082,145（−28.4%）。
- **代价**：DMA beats +38,848、DMA 状态 +50,242、数据等待 +9,541；仍远小于
  CPU 侧节省。
- **附带修复**：性能快照与最后一阶段账本重叠（1400 → 1420）。

## ITER-2 说明

- **改动 1（RTL）**：IMEM 从 1 个 512×512 宽字宏变成 2 个 bank；主机/拷贝引擎
  通过新寄存器 `REG_IMEM_BANK`（0x0074）选写哪个 bank，阵列运行时读启动拍锁存
  的 bank。描述符侧新增 `arr_imem`（word 6 bit17，仍只由
  `rtl/cgra/cgra_desc64.yml` 定义），寄存器启动固定 bank0。
- **改动 2（软件）**：`qs_decode_ring` 把 CN 微码装 bank0、BN 微码装 bank1 各
  一次（stage 内不再重装）；`qs_decode` 里给 `g_all` 清零的 25K 字在环路径是
  死代码（BN 的累加/硬判都在 spad 里），删掉。
- **收益**：总周期 2,649,155 → 2,482,309（−6.3%）；decode 1,082,145 →
  913,031（−15.6%）；利用率 14.66% → 15.65%。
- **代价**：IMEM 面积 0.138 → 0.276 mm²（多一个宏；`spad` 仍是 2.6 mm² 主项）。
  DMA beats/忙碌反而下降，因为装载次数从 16 降到 2。
- **口径修正**：ITER-1 记录里“重装 IMEM 占 decode CPU 388K”是估错的。用
  `mtime()` 分段实测（临时插桩后已回滚）：8 轮 × 2 次装载实际只有 **~51K**
  （每次约 3.2K，拷贝引擎 2 字/beat）。decode 里真正的大头是
  `build_tables()` 重复执行 **127K**（ITER-3 已去掉）、`msg` 先验初始化
  **~94K**、环描述符构建 + 校验（一次性）**~148K**、以及环执行期间被 CPU
  轮询覆盖的 460K（阵列 216K + DMA 244K，串行）。剩余优化按这个拆分来挑。
- **其他场景**（同一提交，`run-proto-*` 直接给周期）：16qam 1,813,472 →
  1,651,879；64qam 1,898,439 → 1,733,097；harq 2,193,520 → 2,029,188。

## ITER-3 说明

- **改动**：`build_tables()` 加一次性守卫。它只由协议常量决定，encode 阶段
  已经建过；decode 又建一遍纯属重复。实测一次 **127K 周期**（52×42×deg ≈
  11K 次条件判断 + 散写），是 decode 里最大的可去掉项。
- **收益**：总周期 2,482,309 → 2,351,954（−5.25%）；decode 913,031 →
  792,128（−13.2%）；利用率 15.65% → 16.52%。
- **同轮试过但回滚**（负结果留档）：
  1. `msg` 先验初始化改「旋转拆两段 + 64-bit 成对写」：+9K（多一遍 20 字的
     栈中转，写通 D$ 上的收益抵不过它）；
  2. 描述符先在栈上组包再 8 次 64-bit 写回：+11K（同上，直接写的行本来就在 L1）。
  结论：D$ 8 KB/写通下，**减少「不该做的活」比减少「每次写的事务数」更值钱**。
- **剩余 decode 账**（ITER-3 实测）：阵列 216K + DMA 244K（串行）+ CPU 332K
  （环构建/校验 ~148K、`msg` 初始化 ~94K、环内轮询外的杂项 ~90K）。

## ITER-4 说明

- **改动（RTL）**：描述符 FSM 与阵列序列器原来同时驱动 `arr_en_q / arr_req /
  arr_phase / arr_imem_q / desc64 / out_en_q / arr_run_q / out_run_q /
  rep_left / rot_tbl`。Verilator 容忍多驱动，但综合入口（yosys-slang）直接报
  “driven by always_ff procedure cannot be written to by any other process”，
  整个 PPA 流程无法 elaborate。把 FSM 自己的复位收进 FSM 复位块、把
  `REG_ROT_BASE` 的写搬到与 `dma_tbl_we` 同一个 always_ff。
- **周期**：完全不变（`run-proto-nr_pdsch` 2,351,954，5 个门全过）。
- **附带**：`read_slang` 加 `-DSYNTHESIS`（仿真专用的 `$error` 事务断言
  不被 slang 支持），并补读 `cgra_desc_pkg.sv`。
- **PPA 复测（HEAD）**：Nangate45 78,499 µm² / 1.429 ns / 37.5 mW @500 MHz；
  sky130 HD 438,119 µm² / 10.762 ns / 90.0 mW @50 MHz（`README.md` §4）。
  旧记录 54,148 µm² / 1.05 GHz 来自更早的 RTL（5,072 flop → 现 7,291）。

## 复现

```bash
# wlbench 周期（metrics.json）
python3 backend/gpcp/run.py \
  --bundle /home/zzq/wlbench/build/bundles/nr_pdsch \
  --backend gpcp-cgra --out backend/gpcp/build/metrics_nr_pdsch.json

# 协议门禁 + 阶段/计数器账本
env -u LD_LIBRARY_PATH make -C sim/soc run-proto-nr_pdsch

# 覆盖率
python3 tools/cgra_coverage.py --scenario nr_pdsch --json

# 利用率与计数器汇总
python3 tools/cgra_utilization.py --scenario nr_pdsch --json

# PPA（两套 PDK）
env -u LD_LIBRARY_PATH make -C syn sta CLK_FREQ_MHZ=500
env -u LD_LIBRARY_PATH make -C syn sta PDK_NAME=sky130hd CLK_FREQ_MHZ=50
```

下一轮候选：环内 DMA 与阵列重叠（P1 第二项）、FFT 逐 tile 控制开销合并；
见 `docs/performance/README.md` §6。
