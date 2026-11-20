# wlbench

硬件无关的无线协议**整机 benchmark**：MAC 调度面 + 真实物理层链路，
外加正确性判定与实时性评分。

它不是系统级仿真器，也不是某个硬件后端的插件：benchmark 只负责把协议算完并
产出最终功能结果；正确性和时间判定全部在独立的 testbench / 评分器里完成。

## 三部分

| 部分 | 文件 | 职责 |
|---|---|---|
| benchmark | `src/wlbench.c` | 整个基站时隙循环：信道、CQI 老化、业务、NR DL/UL 调度、HARQ、OLLA、TPC、PRACH、SSB/SIB1、LTE、WiFi |
| 物理层 | `src/wl_phy.c` | CP-OFDM 链路：FFT/IFFT、QAM、DMRS、TDL 信道、信道估计、MMSE 均衡、卷积码、CRC |
| host I/O | `src/host_main.c` | 只做参数解析、调用核心、打印/落盘结果 |
| testbench | `testbench/check.py` | 同源双编对账：主机版 golden vs GPCP 结果字，按字段容差判定 |
| 评分器 | `eval/score.py` | 输入频率与逐时隙周期，输出按时率、达标频率、分数 |

benchmark **不做任何中间正确性检查**，也不包含断言、黄金值或自检分支。
这样 GPCP 跑的就是真实场景下的程序，而不是为了自检额外插入的路径。

## 物理层真的在算，不是查表

`--phy on`（默认）时，调度器每发一个 TB 就跑一条完整链路：

```
载荷 -> CRC24A -> 卷积编码(K=7,133/171) -> 速率匹配 -> Gold 加扰
     -> QAM 映射(38.211 星座) -> 资源映射 -> IFFT -> 加 CP
     -> TDL 三径(分数时延) + 多径衰落 + AWGN
     -> 去 CP -> FFT -> DMRS 最小二乘估计 -> 频域平滑
     -> MMSE 均衡 -> max-log LLR -> 解扰 -> 解速率匹配
     -> Viterbi -> CRC 校验
```

ACK/NACK 由最后的 CRC 决定，不再由 BLER 公式抽样——**BLER 是量出来的**。
带宽由 `target_coded` 与 MCS 的 `qm` 反推（默认约 1024 个编码比特、
2~6 个 PRB），码率严格取调度给的 `mcs->rate`，所以链路的难易程度与
AMC 的决策是同一口径。

数值自检（`make phy`）验证四件事，而不是“跑起来就算过”：

| 检查 | 判据 |
|---|---|
| FFT | 32 点与朴素 DFT 对拍，max\|Δ\| < 1e-9；IFFT 往返同样 |
| 星座 | QPSK/16/64/256QAM 平均符号功率 = 1.000 |
| AWGN BER | QPSK 在 0/4/8 dB 与理论 Q 函数误差 < 1% |
| 链路 | BLER 随 SNR 单调下降；6 dB 上 post-FEC 8.7e-5 vs 未编码 2.3e-2 |

`--phy off` 回到纯统计模型（旧的 `wl_bler_req` 曲线），用于快速回归与对照。

## 构建与运行（WSL）

```bash
cd /mnt/d/Data/GPCP/wlbench
make
./build/wlbench_host --scenario mixed --slots 500 --seed 1
./build/wlbench_host --scenario mixed --slots 500 --seed 1 --result build/result.txt
make phy          # 物理层数值自检
make test         # testbench 变异自检 + 物理层自检
```

500 时隙 mixed、seed 1 的典型输出：

```
NR   DL 12.25 Mb/s  UL 0.67 Mb/s  TB 2721  NACK 12.6% (首传 11.0%)  重传 340
     PDCCH 2904  SSB 25  SIB1 25  PRACH 13 (成功 3 / 失败 10)
     队列时延 164.74 slot (最大 499.00)  公平性 J=0.613  频选增益 +51.44%  CCE 占用 92.8%
     平均 1.23 层  OLLA -1.56 dB  信道起伏 15.9 dB
UL   功控平均偏置 -0.44 dB  gNB 实测 SINR 8.73 dB
LTE  DL 4.64 Mb/s  TB 1486  NACK 9.3%
WiFi 18.24 Mb/s  尝试 360  碰撞 0
PHY  链路 2721 条  BLER 12.6%  解调 9.50e-02 -> 译码 4.33e-03
     EVM -7.41 dB  CE-NMSE -11.72 dB  估计 SNR 13.31 dB  码率 0.451  PRB 2.7
result words: 232  config_hash 0x5628dfbb
```

## benchmark 契约

```c
void wl_default_config(wl_config *c);
void wl_init(wl_state *s, const wl_config *c);
void wl_slot(wl_state *s);          /* 推进 1 个时隙 */
void wl_run(wl_state *s);           /* 跑到 cfg.slots */
void wl_result(const wl_state *s, uint32_t *words);  /* 固定字结果 */
```

- 1 slot = `cfg.slot_ms`（默认 1 ms）；`wl_slot` 是硬件量测的天然边界。
- 纯 C11：只依赖 `<stdint.h>` 和 `src/wl_math.h`，不用 libc/libm、不用动态内存。
- 固定容量数组：最多 16 UE、100 RB、8 深 CQI 历史、8 PRB 的 PHY 链路。
- 确定性 PCG32：同一 seed 逐值可复现；PHY 用独立随机流，不扰动 MAC 的序列。
- 不出现 spad/bank/PE/IMEM/DMA/微码等任何硬件概念。

GPCP 后端只需要：调用 `wl_init` + 循环 `wl_slot`（或一次 `wl_run`），在每个
`wl_slot` 前后读周期计数，最后调用 `wl_result` 把结果字写出来。

## 结果字

结果不是浮点，而是 `uint32_t words[WL_RESULT_WORDS]`（当前 232 字）。
0..215 与 schema 1 完全一致，216..231 是 schema 2 追加的 PHY 块。浮点量
在核心内先转成整数单位：

- `ppm`：百万分之一（速率、比例）
- `mdb`：毫 dB
- `milli`：千分之一（平均层数、RBG 数、码率）
- `mslot`：千分之一时隙（时延）
- 64-bit 比特数：低字在前，高字在后

主机输出格式为每行 `index value`；`make names` 打印完整下标到字段名的映射。
`include/wlbench.h` 是唯一权威定义，`testbench/result_spec.py` 与它一一对应。

PHY 块：

| 下标 | 字段 | 含义 |
|---|---|---|
| 216/217 | `phy_tb_ok` / `phy_tb_fail` | CRC 通过 / 失败的链路数 |
| 218/219 | `phy_bits` | 译码成功的信息比特（64 位） |
| 220 | `phy_bler_ppm` | 实测 BLER |
| 221/222 | `phy_raw_ber` / `phy_post_ber` | 解调前 / 译码后误比特率 |
| 223 | `phy_evm_mdb` | 判决导向 EVM |
| 224 | `phy_nmse_mdb` | 信道估计归一化均方误差 |
| 225 | `phy_snr_est_mdb` | 由 DMRS 残差估出的 SNR |
| 226 | `phy_rate_milli` | 平均码率 |
| 227/228 | `phy_fft_ops` / `phy_viterbi_ops` | 运算量计数 |
| 229/230 | `phy_links` / `phy_prb_milli` | 链路数与平均带宽 |

## testbench

同源双编：用同一份 `src/wlbench.c` 的 gcc 主机版产生 golden，再和 GPCP 版
结果对账。默认容差按字段类别（计数、比特、速率、时延、dB、每 UE 字段）给定；
`--strict` 要求逐值相等，`--tol-scale` 整体缩放容差。

```bash
python3 testbench/check.py --host build/wlbench_host \
    --args "--scenario mixed --slots 500 --seed 1" \
    --dump /path/to/gpcp_dump.txt \
    --json build/correct.json
```

- `--selftest` 会做三次变异（计数、速率、缺字段），确认检查器不是空转。
- 退出码 0 = PASS，1 = FAIL；`--json` 给出 `correct` 与逐字段差异。
- 正确性口径刻意可调：定点化后的 GPCP 与 double 主机版不会逐位一致，
  但计数和 KPI 应落在带宽内；带宽收得多紧由 `--tol-scale` / `--strict` 决定。

## 评分器

输入逐时隙周期（每行一个整数；只有一个值时用 `--slots` 均摊）：

```bash
python3 eval/score.py --cycles build/cycles.txt --slots 500 \
    --clock-mhz 100,500,1053 --correct 1 --json build/score.json
```

判定：

```
budget_cycles = clock_hz × slot_ms / 1000
on_time_rate  = count(cycles[i] <= budget_cycles) / slots
required_hz   = max(cycles) × 1000 / slot_ms        # 100% 按时所需最低频率
score         = 100 × on_time_rate                  # 正确性失败时直接 0
```

输出每个频率下的预算、按时数、按时率、最坏/均值/P95 周期、所需频率、
分数与 verdict（`PASS` / `LATE` / `FAIL` / `TIMING_ONLY`）。
`--correct` 或 `--correct-json` 来自 testbench；缺省表示只做时间判定。

## 范围与边界

- 场景：`mixed`（NR + LTE + WiFi）与 `nr-only`。
- NR：PF 调度、EESM 有效 SINR、MCS/秩自适应、OLLA、8 进程 DL HARQ、
  4 进程 UL HARQ + 闭环功控、PRACH、SSB/SIB1、PDCCH CCE 容量约束。
- 物理层：CP-OFDM（8 个 PRB 以内）、K=7 卷积码、MMSE 接收机；
  **不含** LDPC/Turbo、MIMO 分层预编码、真实射频损伤。
- LTE：独立载波、简化 PDSCH；WiFi：CSMA 退避，可选 LBT 共存。
- 真实周期由 GPCP 后端测量后交给评分器，benchmark 里没有静态算力账。
- 本目录不包含 GPCP 的编译/运行适配；后续只需按上面的契约接一个薄运行器。
