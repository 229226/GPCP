# 22 — 协议级 CPU / CGRA 落点对比

> 同一份协议 benchmark（同一 `plan.h`、激励、判据）编译成两条执行路径：
> **CGRA 路径**（现有实现，尽量把算法放到阵列）与 **CPU 路径**（同一算法全部在
> CVA6 上跑），逐阶段比周期。所有数字都是 Verilator 全 SoC 仿真实测。
>
> 与 kernel 微基准的区别：这里保留完整的阶段边界、缓冲区、协议流程和判据，
> 不把算法从链路里摘出来单独计时。

---

## 1. 结论

| 算法 | CPU / CGRA（周期比） | 落点 | 依据 |
|---|---:|---|---|
| 4096 点 FFT | **10.1–30.9×** | **CGRA** | 所有含 FFT 的阶段至少 10×，最高 31× |
| 200 点 DFT（Bluestein） | **4.5–4.7×** | **CGRA** | 对照是旧 O(N²) 标量实现 |
| 256 点 DFT（WiFi） | **1.65×**（阶段） | **CGRA** | 复用 radix-4 引擎；整链只 1.16×，因为 WiFi LDPC 还在 CPU |
| LDPC CN/BN（BG2，8 迭代） | **2.4–2.6×** | **CGRA，编排是瓶颈** | 阵列只忙 6%，其余是主机编排/DMA/重构；按 `docs/21` P2 改造后才能拉开 |
| Polar SC 译码 | **0.55×**（CPU 快 1.8×） | **CPU** | 1120 次阵列运行/场景，固定开销主导 |
| Polar 编码（9 级） | **1.20×** | **CGRA** | 一次装载、9 次运行，值得留阵列 |
| 2×2 MMSE 合并 | **0.35×**（CPU 快 2.8×） | **CPU** | 每次只算 16 个 RE，24 次阵列运行的固定开销吃掉收益 |
| PRACH ZC 相关（139 假设） | **11.5×** | **CGRA** | 长向量、规则累加 |
| PUCCH ZC 相关（2 假设） | **9.9×** | **CGRA** | 同 PRACH |
| SSS 相关（336 假设） | **1.50×** | **CGRA** | 阵列仍略快，但编排占比很大 |
| PSS 相关（3 假设） | **1.26×** | **CGRA** | 余额很小 |
| LTE 小区搜索（PSS+SSS） | **0.24×**（CPU 快 4.1×） | **CPU** | 62 点短相关 + 507 次阵列运行，固定开销远大于计算 |
| LTE Turbo 译码 | **1.00×** | **CPU（暂无阵列实现）** | 纯 C 是唯一路径，占 LTE 场景 50–62% |
| QAM / 加扰 / 速率匹配 / CRC / 编码 | **1.00×** | **CPU（暂无阵列实现）** | 两条路径相同 |

一句话：**FFT/DFT 和长向量相关放 CGRA；Polar SC、MMSE、小区搜索放 CPU；
LDPC 留 CGRA 但先修编排；Turbo 与 WiFi LDPC 需要新的阵列 kernel。**

---

## 2. 方法：同一协议，两条执行路径

- 默认：`make run-proto-<scenario>`（CGRA 路径）。
- 对照：`make run-proto-cpu-<scenario>`，编译时加 `-DPROTO_CPU_ONLY`。
- 两条路径使用**同一个** `plan.h`、同一份 `stimulus.bin`、同一套
  `proto/models/<scenario>.py` 黄金判据；只有阶段内部算法不同。
- 阶段周期来自运行器每阶段的 `mtime()` 差值（与 `perf-report` 同一账本）。
- CPU 实现是直白标量 C（`-O2`），没有手工展开/SIMD；因此 CPU 侧数字偏保守，
  对 CGRA 有利。结论只用于量级判断。

### 2.1 已补齐的 CPU 实现

| 阶段 | CPU 实现位置 |
|---|---|
| FFT4096 / DFT256 | `sw/soc/fft_cpu.inc` |
| LDPC（NR PDSCH/PUSCH） | `pdsch_stages.inc` / `pusch_stages.inc` 的 `PROTO_CPU_ONLY` 分支 |
| DFT200（LTE PUSCH） | `lte_ul_stages.inc`（复用旧 O(N²) 参考） |
| Polar SC | `pdcch_stages.inc` / `ssb_stages.inc` 的 `node_run` CPU 分支 |
| Polar 编码 | `pdcch_stages.inc` 的 `polar_transform` CPU 分支 |
| MMSE 2×1 | `mimo_stages.inc`（走已有的标量回退路径） |
| PRACH / PUCCH ZC 相关 | `zc_corr.inc` 的 `zc_corr_cpu()` |
| PSS / SSS 相关 | `ssb_stages.inc` 的 `corr_one` CPU 分支 |
| LTE 小区搜索 | `lte_cell_stages.inc` 的标量循环 |

### 2.2 已补齐的阵列实现

| 阶段 | 阵列实现 | 结果 |
|---|---|---|
| WiFi 256 点 DFT | 复用 `ssb_fft.inc` 的 radix-4 引擎 | 阶段 1.65×，整链 1.16× |
| LTE 小区搜索 | 复用 `prach_corr`（127 tap 补零） | 比 CPU 慢 4.1×，落点回到 CPU |

### 2.3 仍缺阵列实现的算法

| 算法 | 状态 | 说明 |
|---|---|---|
| LTE Turbo 译码 | **待补** | 8 状态 Max-Log-MAP × 8 迭代 × K=104；现有 DFG 编译器只支持直线 DFG，需要可循环的微码调度器 |
| WiFi LDPC（N=648） | **待补** | 324 个校验行、648 个变量、每行最多 24 条边；不是 QC 块结构，现有 CN/BN 镜像放不下 |
| QAM 映射 | **待补** | 16 符号/次可以映射，但按当前固定开销估算收益很小（见 §6） |
| LDPC 编码 | **待补** | 矩阵乘/XOR 型，阵列优势不明显 |
| 加扰/速率匹配/CRC/交织 | 不需要 | 纯比特流水，阵列没有收益 |

---

## 3. 整链结果（17 个场景）

| 场景 | CGRA 路径 | CPU 路径 | CPU/CGRA | 备注 |
|---|---:|---:|---:|---|
| `nr_csi` | 209,094 | 4,588,956 | **21.95×** | 链短，几乎全是 FFT |
| `nr_mimo` | 505,137 | 9,258,144 | **18.33×** | MMSE 已换 CPU，仍被 FFT 主导 |
| `nr_prach` | 109,938 | 1,196,790 | **10.89×** | 相关换 CPU 后整链慢 10.9× |
| `lte_prach` | 109,966 | 1,192,629 | **10.85×** | 同上 |
| `nr_pdsch_harq` | 3,124,993 | 23,416,287 | **7.49×** | 两次传输的 FFT + LDPC |
| `nr_ssb` | 3,228,453 | 20,992,140 | **6.50×** | 4 次 FFT + PSS/SSS + Polar |
| `nr_pdsch` | 4,005,766 | 24,143,322 | **6.03×** | FFT + LDPC |
| `nr_pdcch` | 1,770,115 | 10,042,190 | **5.67×** | 编码留阵列、SC 换 CPU |
| `nr_pusch` | 2,547,116 | 14,201,582 | **5.58×** | FFT + LDPC |
| `nr_pdsch_16qam` | 2,694,177 | 14,326,816 | **5.32×** | 同上 |
| `nr_pdsch_64qam` | 2,805,819 | 14,445,539 | **5.15×** | 同上 |
| `lte_pusch` | 5,619,699 | 27,589,268 | **4.91×** | FFT + DFT200 + Turbo |
| `lte_pdsch` | 4,489,026 | 21,745,509 | **4.84×** | FFT + Turbo |
| `nr_pucch` | 6,827 | 22,966 | **3.36×** | 2 次 ZC 相关 |
| `nr_pusch_tp` | 7,475,170 | 19,103,460 | **2.56×** | 360 点 IDFT 本来就是标量 |
| `wifi_11be` | 1,090,496 | 1,264,799 | **1.16×** | DFT256 已下沉；LDPC 仍在 CPU |
| `lte_cell_search` | 1,822,652 | 446,556 | **0.24×** | 阵列版短相关反而慢 4.1× |

---

## 4. 逐阶段（真正换侧的阶段）

| 场景 | 阶段 | CGRA | CPU | CPU/CGRA |
|---|---|---:|---:|---:|
| nr_ssb | ofdm_demodulate（4 FFT） | 595,238 | 17,991,160 | 30.23× |
| nr_mimo | ofdm_demodulate | 293,685 | 9,122,639 | 31.06× |
| nr_csi | ofdm_demodulate | 150,457 | 4,534,050 | 30.14× |
| nr_pdsch_harq | ofdm_demodulate ×2 | 657,839 | 17,971,318 | 27.32× |
| nr_pusch | ofdm_demodulate | 329,572 | 8,962,434 | 27.19× |
| nr_pdcch | estimate | 165,415 | 4,498,555 | 27.19× |
| nr_pdsch | estimate | 344,054 | 8,931,710 | 25.96× |
| nr_pdsch_16qam | estimate | 339,223 | 8,964,861 | 26.43× |
| nr_pdsch_64qam | estimate | 339,224 | 8,960,519 | 26.41× |
| lte_pdsch | ofdm_demodulate | 334,050 | 9,013,402 | 26.98× |
| lte_pusch | ofdm_demodulate | 332,448 | 8,925,007 | 26.85× |
| nr_pdsch | ofdm_modulate | 931,270 | 9,447,054 | 10.14× |
| lte_pdsch | ofdm_modulate | 922,846 | 9,540,227 | 10.34× |
| nr_pdcch | ofdm_modulate | 460,815 | 4,763,875 | 10.34× |
| lte_pusch | modulate（含 DFT200） | 656,066 | 3,075,794 | 4.69× |
| lte_pusch | equalise_demap（含 IDFT200） | 688,169 | 3,111,082 | 4.52× |
| wifi_11be | ofdm_demodulate（DFT256） | 280,843 | 462,411 | 1.65× |
| nr_pdsch | decode（LDPC） | 1,961,226 | 4,994,594 | 2.55× |
| nr_pusch | decode（LDPC） | 1,971,492 | 4,985,215 | 2.53× |
| nr_pdsch_harq | decode（LDPC） | 2,125,742 | 5,103,491 | 2.40× |
| nr_pdcch | encode（Polar 9 级） | 81,364 | 97,891 | 1.20× |
| nr_pdcch | decode（Polar SC） | 861,755 | 478,065 | **0.55×** |
| nr_ssb | decode（Polar SC） | 843,562 | 445,383 | **0.53×** |
| nr_ssb | correlate（SSS 336） | 1,506,945 | 2,264,913 | 1.50× |
| nr_ssb | correlate（PSS 3） | 25,157 | 31,630 | 1.26× |
| nr_prach | correlate（139） | 103,346 | 1,190,176 | 11.52× |
| lte_prach | correlate（139） | 103,374 | 1,186,015 | 11.47× |
| nr_pucch | correlate（2） | 1,823 | 18,000 | 9.87× |
| nr_mimo | mimo_equalise（MMSE） | 119,554 | 42,145 | **0.35×** |
| lte_cell_search | correlate（PSS 3） | 17,463 | 4,331 | **0.25×** |
| lte_cell_search | correlate（SSS 504） | 1,795,779 | 433,329 | **0.24×** |

---

## 5. 为什么整链只有 5–7×，而不是 20×+

以 `nr_pdsch` 为例（CPU 路径 24.14 M）：

| 部分 | 周期 | 占比 |
|---|---:|---:|
| FFT（`ofdm_modulate` + `estimate`） | 18.38 M | 76% |
| LDPC `decode` | 4.99 M | 21% |
| 编码/调制/DMRS/映射/均衡（本来就 CPU） | 0.77 M | 3% |

CGRA 路径把 18.38 M 压到 1.28 M，但 4.99 M 的 LDPC 只压到 1.96 M，加上其余
0.77 M，整链就是 4.0 M。**只要 LDPC 的编排不降下来，NR PDSCH 的天花板就是
2.5× 左右的那一块。**

`lte_pdsch` 更直接：Turbo 2.75 M 在两条路径里都一样，占 **CGRA 路径**的 61%。
FFT 从 18.5 M 压到 1.26 M 后，整链 4.49 M 里 Turbo 仍占 61%——**LTE 下行要再快，
只能把 Turbo 做成阵列 kernel（`docs/21` A4），否则 FFT 再快也没用。**

`wifi_11be` 是另一个例子：DFT256 换成阵列后阶段快 1.65×，但 LDPC 703 k 仍占
64%，整链只从 1.28 M 降到 1.09 M。

---

## 6. 落点建议（按优先级）

1. **FFT4096 / DFT200 / DFT256：确认放 CGRA。** 10–31× / 4.5× / 1.65×，
   后续只做搬运/描述符瘦身（`docs/21` A3）。
2. **LDPC：留在 CGRA，但先做 `docs/21` P2 的三件事**（block-major 布局、
   旋转表、行/列循环内收）。当前 2.5× 是"编排拖累后的净值"，不是阵列上限。
3. **Polar SC / MMSE / 小区搜索：放 CPU。** 实测阵列分别慢 1.8×、2.8×、4.1×，
   共同原因是每次只有几十到几百个 tap/RE，却要付一次阵列启动 + DMA。
4. **Polar 编码：留 CGRA。** 9 级变换一次装载多次运行，1.2×。
5. **PRACH/PUCCH 相关：放 CGRA（10–11×）；SSS/PSS 相关：CGRA（1.3–1.5×）。**
6. **Turbo：先留 CPU；要做阵列，需要新的循环式微码调度器**（8 状态 ACS，
   K=104、8 迭代）。这是本项目下一个真正的编译器/架构任务，不是加分支。
7. **WiFi LDPC：先留 CPU；** 324×648 的 flooding 图需要新的变量节点 kernel，
   现有 CN/BN 镜像（20 边 / 24 消息 / Zc 块）放不下。
8. **QAM 映射、LDPC 编码：暂留 CPU。** 若要做 QAM 阵列版，16 符号/次的固定
   开销约 600 拍，720 bit 的 `modulate` 只能省几万拍，收益远小于 FFT/LDPC。

---

## 7. 复现

```bash
# 默认（尽量 CGRA）与 CPU 对照各跑一次
make run-proto-nr_pdsch
make run-proto-cpu-nr_pdsch

# 逐阶段对比（写 build/proto/compare_nr_pdsch.csv）
python3 tools/proto_compare.py --scenario nr_pdsch \
    --cgra sim/soc/build/proto_nr_pdsch_dump.txt \
    --cpu  sim/soc/build/proto_nr_pdsch_cpu_dump.txt
```

17 个场景全部双跑；汇总数据在 `build/proto/compare_all.csv`（131 行），
每个场景另有 `compare_<scenario>.csv`、`report.md`（CGRA）和
`report_cpu.md`（CPU）。

---

## 8. 局限

- CPU 侧是直白标量实现，没有针对 CVA6 调优；真实 CPU 上限会低于表中数字，
  因此"CGRA 优势"被高估，而"CPU 更合适"的结论会被低估。
- 单次运行、固定激励；没有统计多个信道/负载。
- `-DPROTO_CPU_ONLY` 只替换算法路径，不改阶段划分、缓冲区布局和协议流程。
- Turbo / WiFi LDPC / QAM / LDPC 编码的阵列版仍缺；表中它们的 1.00× 表示
  "未换侧"，不表示"CGRA 不划算"。
