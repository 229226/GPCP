# 性能与 PPA

## 1. 度量口径

| 指标 | 定义 | 来源 |
|---|---|---|
| 总周期 | 程序从开始到结束的时钟拍数 | 仿真台架 / 程序计数器 |
| 阵列运行次数 | `array_runs`，一次静态调度的启动 | 结果区 |
| DMA 次数 | `dma`，一次描述符或寄存器搬运 | 结果区 |
| 阵列忙拍数 | `PERF_ARRAY_BUSY`，硬件计数（全跑）；不要用软件 `busy`（只含译码） | 性能计数器 |
| 利用率 | `PERF_ARRAY_BUSY / PERF_CYCLES` | 性能计数器 |
| CGRA 覆盖率 | 阶段 / 工作量 / 有效覆盖，定义见 `tools/cgra_coverage.py` | 同左 |
| 按时率 | 1 ms 时隙内完成的传输块比例 | `proto/load.py` |

频率校正：Verilator 周期比隐含 CPU 与 CGRA 同频。实测 CVA6 fmax 约 264 MHz，
CGRA 约 1.05 GHz，**时间比 ≈ 周期比 × 4**。对外引用必须带这个校正。

## 2. 当前基线（2026-11-16，ITER-3 `56df22b`）

逐提交的优化记录（commit / wlbench 周期 / 覆盖率 / CPU-DMA-CGRA 占比 /
全部性能计数器）见 [iterations.md](iterations.md)。

| 场景 | 周期 | 阵列运行 | DMA | 备注 |
|---|---:|---:|---:|---|
| `run-proto-nr_pdsch` | 2,351,954 | 1200 | 4656 | 阵列忙 388,384 → 利用率 **16.52%** |
| `run-proto-nr_pdsch_16qam` | 1,647,380 | 976 | 4656 | Qm=4 |
| `run-proto-nr_pdsch_64qam` | 1,732,317 | 976 | 4656 | Qm=6 |
| `run-proto-nr_pdsch_harq` | 1,895,624 | 1200 | 4656 | rv=0/2 Chase 合并 |
| `run-pdsch-loop` | 2,633,117（程序内计数） | 1200 | 4656 | 手写自环参照 |

ITER-0（`da434fd`）为 3,077,988 / 12.62%，ITER-1（`d082938`）2,649,155 / 14.66%，
ITER-2（`84f2f7b`）2,482,309 / 15.65%；早期文档里的 7.03% 只统计了译码阶段的
软件 `busy`，已废弃。

其他参考数字：

- 1024 字线性 DMA：0.70 周期/字；
- FFT 256/1024/4096：17.2 / 22.4 / 28.5 周期/点；
- 单次阵列运行的固定开销：`T(c) = 32 + 1.0045c`（T0 台架拟合，R²=0.99966）；
- SSB 采样级链路：1907 次阵列运行、约 4.34 M 周期，MIB 32/32。

## 3. nr_pdsch 阶段拆分

| 阶段 | 周期 | 占比 | 阵列运行 |
|---|---:|---:|---:|
| decode | 792,128 | 33.7% | 752 |
| ofdm_modulate | 483,741 | 20.6% | 224 |
| transform | 313,209 | 13.3% | 224 |
| encode | 270,482 | 11.5% | 0（CPU） |
| demap | 124,769 | 5.3% | 0 |
| modulate | 112,459 | 4.8% | 0 |
| dmrs | 100,832 | 4.3% | 0 |
| map_to_grid | 78,816 | 3.4% | 0 |
| transport_block | 29,448 | 1.3% | 0 |
| equalise | 23,407 | 1.0% | 0 |
| estimate | 3,685 | 0.2% | 0 |
| channel | 37 | 0.0% | 0 |

结论：前三个阵列阶段合计 67.6%；`decode` 单阶段 752 次运行、平均约 1,053 拍/run，
仍远高于 32 拍固定开销，说明等待/DMA 间隔占大头。decode 内部实测拆分
（ITER-2/3 插桩）：环执行 ~460K（阵列 216K + DMA 244K，串行）、环构建/校验
~148K（一次性 CPU）、`msg` 先验初始化 ~94K（CPU）。

## 4. PPA（Nangate45 + sky130 HD）

`cgra_top` 的逻辑网表：IMEM 与 spad 两个阵列在综合前被剔除（宽字 IMEM 需要
16 份复制宏；降成触发器会让网表爆掉），所以下表只含逻辑，存储面积单列在
下面。两套开放 PDK 走同一个 Yosys+slang → ABC → iEDA 流程，只换 liberty：

| 项 | Nangate45（45 nm，1.1 V） | sky130 HD（130 nm，1.8 V） |
|---|---|---|
| 标准单元库 | `NangateOpenCellLibrary_typical.lib` | `sky130_fd_sc_hd__tt_025C_1v80.lib` |
| 逻辑面积 | 78,499 µm²（7,291 flop，占 49.3%） | 438,119 µm²（7,291 flop，占 41.8%） |
| 单元数 | 34,398 | 40,621 |
| 关键路径（pre-layout） | 1.429 ns | 10.762 ns |
| 综合目标 / slack | 500 MHz（2 ns）/ +0.54 ns | 50 MHz（20 ns）/ +8.49 ns |
| 由关键路径推得 Fmax | ≈ 700 MHz | ≈ 87 MHz |
| 功耗（toggle 0.1，无 VCD） | 37.5 mW @ 500 MHz | 90.0 mW @ 50 MHz |
| 复现 | `make -C syn sta CLK_FREQ_MHZ=500` | `make -C syn sta PDK_NAME=sky130hd CLK_FREQ_MHZ=50` |

**口径**：两组数字都是本次在 HEAD（ITER-3）上复测的。旧记录里的 54,148 µm² /
Fmax 1.05 GHz / 23.8 mW 是更早的 RTL（描述符契约、窗口检查、性能计数器、
IMEM 双 bank 之前）测的，触发器从 5,072 涨到 7,291，不能与上表混用。

sky130 的 90 mW 看着高，但换算成每周期能量是 1.8 µJ/cycle，对 Nangate45 的
75 nJ/cycle 约 24×，与 `C·V²` 的 130 nm/1.8 V 对 45 nm/1.1 V 的比值（≈22×）
一致。两条关键路径都卡在**未被扇出缓冲的高扇出网**上（sky130 那条 87 扇出、
10.9 ns 转换），真实布线后会明显改善，此处只作 pre-layout 对比。

| 存储与参考 | 值 |
|---|---|
| IMEM | 0.276 mm²（2 bank × 512×512，1R1W；ITER-2 起双 bank） |
| spad | 约 2.6 mm²（当前每 bank 16R/16W） |
| CPU 参考 | CVA6 fmax 约 264 MHz（关键路径在 FPU FMA） |

spad 的 16R/16W 是当前面积主项；要降到 0.3 mm² 量级，必须先改 kernel 数据布局，
减少每 bank 的同时读写端口需求。

## 5. 已有优化

- 64 B 复合描述符：`in → array → out → repeat` 由硬件 FSM 自动滚动，中间轮次
  不写状态字，显著减少描述符数量与 CPU 干预。
- 每 bank 旋转表：把 LDPC 的逐边旋转从 CPU/DMA 搬到 spad 访问路径。
- DMA 突发：32 beat / 4 KiB 裁剪，1024 字线性拷贝 0.70 周期/字。
- 双页 ping-pong：阵列与 DMA 重叠，`run-rxchain` 4 slot 514 周期/slot。

## 6. 下一步方向

1. DMA 与阵列重叠（P1 第二项）：环内每个描述符是 `搬入 → 阵列 → 搬出` 串行，
   若允许下一个描述符的搬入在上一个阵列运行时进行（双页 ping-pong + 两深
   上下文 + 旋转表分页），decode 的 460K 可压到约 360K，FFT 两阶段同理。
2. `ofdm_modulate` + `transform`（33.9%）：每个 tile 约 9–14 次 MMIO 寄存器写，
   CPU 247K/76K 主要花在这里；把 tile 参数打包成单次提交可再省一大截。
3. decode 的 CPU 残余：环构建/校验 148K（一次性，可用 64-bit 直写 + 校验
   采样收敛）、`msg` 先验初始化 94K（可考虑下沉成 CGRA 广播/旋转 kernel）。
4. `encode`（11.5%）：纯 CPU 的 LDPC 编码，评估下沉到阵列或与 DMA 重叠。
5. spad 端口压力：按 bank 旋转表重排数据布局，降低面积与冲突。

## 7. 引用规则

- 周期比不是时间比：对外说加速比时乘 4× 频率校正，或明确写「同频周期比」。
- 利用率必须带口径：`busy / cycles`，并注明场景与运行次数。
- 任何数字都要能追溯到一条门禁命令与一次提交。
