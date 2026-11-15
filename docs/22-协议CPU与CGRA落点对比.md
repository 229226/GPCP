# 22 — 协议级 CPU / CGRA 落点对比

> 同一份协议 benchmark（同一 `plan.h`、激励、判据）编译成两条执行路径：
> **CGRA 路径**（现有实现，尽量把算法放到阵列）与 **CPU 路径**（同一算法全部在
> CVA6 上跑），逐阶段比周期。所有数字都是 Verilator 全 SoC 仿真实测。
>
> 与 kernel 微基准的区别：这里保留完整的阶段边界、缓冲区、协议流程和判据，
> 不把算法从链路里摘出来单独计时。
>
> **数字版本**：§1 汇总表、§3 整链、§9 频率校正按**账本 v4**（2026-11-15）。
> 历史沿革：v2（Turbo 译码走阵列微码 `turbo_seq`、`mimo_equalise` 换 `mmse_full`、
> FFT int16 定点修复）→ v3（IR 如实声明 4096 点/CP 288，CPU 参考路径重建）
> → **v4**：IR schema v2 把粗节点拆成 `equalise` / `demap` / 显式 `transform`，
> 并给 LDPC 换上 64 字节 `repeat` 复合描述符；17 个场景全部用重建后的二进制重跑。
> v4 的 LDPC 三场景整链周期比 v3 账本高约 2–3%，主要原因不是性能倒退，而是
> v3 的 LDPC 数字来自**旧二进制**（Makefile 曾漏掉 `pdsch_stages.inc` 依赖），
> 本轮补齐依赖后测到的是真实值。§2.x 与 §4 是优化过程记录，保留当时（v1，
> 2026-11-12）的实测值，用于说明「为什么这样改」，不作为当前结论引用。

---

## 1. 结论

| 算法 | CPU / CGRA（周期比） | 落点 | 依据 |
|---|---:|---|---|
| 4096 点 FFT | **18.6–31.2×** | **CGRA** | 所有含 FFT 的阶段至少 18.6×，最高 31.2×（反序查表 + 64-bit 描述符） |
| 200 点 DFT（Bluestein） | **4.5–4.7×** | **CGRA** | 对照是旧 O(N²) 标量实现 |
| 256 点 DFT（WiFi） | **1.65×**（阶段） | **CGRA** | 复用 radix-4 引擎；整链只 1.16×，因为 WiFi LDPC 还在 CPU |
| LDPC CN/BN（BG2，8 迭代） | **2.4–2.6×** | **CGRA，编排是瓶颈** | 阵列只忙 6%，其余是主机编排/DMA/重构；按 `docs/21` P2 改造后才能拉开 |
| Polar SC 译码 | **0.55×**（CPU 快 1.8×） | **CPU** | 1120 次阵列运行/场景，固定开销主导 |
| Polar 编码（9 级） | **1.20×** | **CGRA** | 一次装载、9 次运行，值得留阵列 |
| 2×2 MMSE 合并 | **0.93×**（基本打平，CPU 略快 1.07×） | **混合** | 求解下沉阵列 + y 压连续（§2.3.3）：阶段 61,030→**23,410**（−62%）；阵列只承担符号提取，喂它的 CPU 编排与 CPU 标量求解基本抵消；v2 换 `mmse_full`（一次覆盖两层，阵列运行减半）后仍是 0.93×，说明瓶颈在编排不在阵列 |
| PRACH ZC 相关（139 假设） | **11.5×** | **CGRA** | 长向量、规则累加 |
| PUCCH ZC 相关（2 假设） | **9.9×** | **CGRA** | 同 PRACH |
| SSS 相关（336 假设） | **1.50×** | **CGRA** | 阵列仍略快，但编排占比很大 |
| PSS 相关（3 假设） | **1.26×** | **CGRA** | 余额很小 |
| LTE 小区搜索（PSS+SSS） | **0.24×**（CPU 快 4.1×） | **CPU** | 62 点短相关 + 507 次阵列运行，固定开销远大于计算 |
| LTE Turbo 译码 | **1.88×** | **CGRA（`turbo_seq`）** | 阵列微码：α/β 递推一次算 32 步（8 PE 各管一个状态），LLR 后处理仍在 CPU；阶段 2,790,352→**1,481,818** |
| QAM / 加扰 / 速率匹配 / CRC / 编码 | **1.00×** | **CPU（暂无阵列实现）** | 两条路径相同 |

一句话：**FFT/DFT 和长向量相关放 CGRA；Polar SC、小区搜索放 CPU；MMSE 落点已变
混合（阵列仅提取符号，CPU 仍承担编排）；LDPC 留 CGRA 但先修编排；Turbo 与 WiFi LDPC
需要新的阵列 kernel。**

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

### 2.3 原「仍缺阵列实现」的四项（2026-11-11 已补齐 kernel，见 docs/23）

| 算法 | 状态 | 说明 |
|---|---|---|
| LTE Turbo 译码 | **已有实现** | `turbo_acs`：单步 ACS tile，一次运行完成一个状态的完整递推；K=104 步与 8 次迭代由 C 侧循环驱动（同 Polar SC 范式）。**不需要循环式微码调度器** |
| WiFi LDPC（N=648） | **已有实现** | `wifi_vn`（24 消息变量节点）+ `wifi_cn24`（复用 `schedule_cn(edges=24)`）。每边不同旋转量的 gather/scatter 仍由 C 侧编排 |
| QAM 映射 | **已有实现** | `qam_map` 三个变体：QPSK / 16QAM / 64QAM，16 PE 各映射 1 个符号，与 38.211 电平表逐值对账 |
| LDPC 编码 | **已有实现** | `ldpc_enc_acc`：一个提升行内 20 条边循环移位后异或累加 |
| 加扰/速率匹配/CRC/交织 | 不需要 | 纯比特流水，阵列没有收益 |

### 2.3.1 MMSE 归一化步已证明可省（2026-11-11）

`mimo_equalise` 原来每 RE 做 4 次复数乘 + 4 次 64 位除法
（`s = num*conj(det)/|det|^2`）。但整数路径里 `deti = 0 - Im(g01*conj(g01))` **恒等于 0**，
且 `det = g00*g11 - |g01|^2 > 0`（Cauchy-Schwarz 加 λI 保证正定），所以
`s = num/det`，乘正实数、除正实数都不改符号，硬判只需要 `sign(num)`。

实测三档硬判与原式逐位一致（25/15/10 dB = 720/711/618），阶段代价：
CGRA 路径 105,381 → **94,721**，CPU 路径 42,145 → **31,919**。
注意两条路径同时受益，且 CPU 侧降幅更大，因此 MMSE 的加速比从 0.35× 变为
**0.34×** —— 落点结论（回 CPU）不变，反而更明确。

### 2.3.2 MMSE 的搬运改为 CPU 直写 spad（2026-11-11 晚）

原实现每个 tile 先在 C 里把 4 个叶子摆进 `mleaf` 中转数组、再交给 DMA（24 趟）。
`gather` 实测 48,543 周期，是整段最大的单项，而它的成本全在"数据摆放顺序与阵列要的
形状不一致"——不是计算。

改动（`sw/soc/mimo_stages.inc`，新 kernel `cgra/kernels/mmse_dual.yaml`）：

- 一层算两层：同一批 y 只搬一次，**24 趟 → 12 趟**（`array_busy` 1056 = 12 × 88 拍）；
- y 由 CPU 直接 `mmio_w` 写进 spad（I/Q 交错，PE k 占 2k/2k+1），**删除 `mleaf` 与整条
  DMA 调用**；契约检查改在写之前用同一批已读入寄存器的值做 4 次比较，仍是免费搭车；
- 8 个信道系数走广播叶子，每页各写一次。

| 指标 | 改前 | 改后 |
|---|---:|---:|
| 阶段周期 | 94,721 | **61,030**（−35.6%） |
| 其中阵列忙拍 | 768 | 1,056 |
| Array runs | 24 | **12** |
| 加速比（CPU/CGRA） | 0.34× | **0.52×** |
| `nr_mimo` 整链 | 481,767 | **446,167**（19.18× → 20.71×） |

**曾试过但放弃的路线**：用描述符环让引擎"按表搬"并在阵列运算时预取下一页。
它在 172/192 个位置上正确、省 8.2%（440,948 周期），但引擎会**静默漏做一段连续描述符**
（tile 2/5 的天线 1，实测读回 `0` 与 `-1`），且 `RING_DONE` 照常报完成、不置错误位。
串行化、逐条 dump 描述符（全对）、环深核对均已排除软件因素，详见
`docs/wip-mmse-ring-notes.md`。最终改为 CPU 直写，周期与环方案基本持平（440,736）而正确性
100%：三档 25/15/10 dB 硬判 720/711/618 与原版逐位一致。

注意：`mimo_equalise` 的 DMA 计数因此变为 0，`proto/models/nr_mimo.py` 的 `platform.dma`
已相应置 false（FFT 的描述符本来就不计入平台锚点）。

> **注意「有实现」≠「已下沉」**：本表补齐的是 kernel 层，`run-new-kernels`
> 已在真实 SoC 上逐值验证 7 张镜像。但按本文 §6 的实测结论，默认落点仍是
> 控制核（`plan.h` 逐字节不变），用 `PROTO_NEW_ARRAY=1` 才切到阵列。
> 端到端协议链接入与周期收益仍需 C 侧编排与主机接口改造，见 `docs/23` §4。

### 2.3.3 MMSE 求解下沉阵列 + y 压连续（2026-11-12）

把"相关 + 2×2 求解 + 硬判"整段压成一张 `kind:image` 镜像
（`cgra/kernels/mmse_full.yaml`，6 段：corr 算 b 留 spad、4 段各算一个符号位、pack 打包成 1 个字），
并把 FFT 输出端的 180 个数据 RE 预置换成连续布局（`tools/nr_mimo_ce.py` 的 `td_from_grids` 在
`freq[rev4(BIN0+r)]` 处放数据 RE，FFT 后自然落在 `data[BIN0+r]`，接收端改为顺序读，180 个不同
cache 行降到 31 行；与导频频点零冲突，因数据 bin 全 ≡2 mod16、导频 bin 为 1/5/9/13）。

两项一起做（"求解下沉 + y 压连续"包）：

| 指标 | 改前（§2.3.2 末态） | 改后 |
|---|---:|---:|
| 阶段周期（CGRA） | 61,030 | **23,440**（−61.6%） |
| 其中阵列忙拍 | 1,056（12×88） | 1,692（12×141） |
| Array runs | 12 | **12**（每趟 88→141 拍，多 53 拍给 corr+solve） |
| 加速比（CPU/CGRA） | 0.52× | **0.93×**（基本打平） |
| `nr_mimo` 整链（CGRA） | 446,167 | **408,874**（20.71× → **22.43×**） |

**落点结论变了**：MMSE 阶段从"CPU 明显更快"变成"混合"。原因——阵列只承担符号提取
（1,692 拍），但喂它要 720 次 MMIO 写 y + 180 次 MMIO 读回打包比特，这部分 CPU 编排
（20,287 拍，占阶段 86.5%）和 CPU 路径的标量求解（21,759 拍）几乎抵消。所以求解下沉
省下的不是"阵列算得比 CPU 快"，而是"删掉了 §2.3.2 里那个被打散的 y 读取 + 标量求解"——
两项合力把阶段从 61,030 砍到 23,440，但 CGRA 与 CPU 在该阶段已基本打平。**整链**仍因去掉
约 37k 周期 CPU 编排而变快（20.71×→22.43×）。

**精度**：b 在阵列里先 `>>2` 再与 g 做 16 位乘；`max|b|≈86k`（17 位）、`g≤292`（9 位，不缩放），
`4·max|h|·max|y|` 受契约约束 ≤32767，保证 b 存进 16 位 leaf 不截断、各乘积与两项之差不回绕。
三档 25/15/10 dB 硬判 720/712/622，与浮点黄金落同一容差带（25 dB 逐位一致）；`mmse_runs=12`、
整程序零错误。

**验证**：`run-mimo` 三档 720/712/622；`run-proto-nr_mimo` 与 `run-proto-cpu-nr_mimo` 双跑均过判据；
`mmse_full` 编译器的 image 跨段串接自检 144 值 0 差异。被放弃的"描述符环 + 预取"路线仍记在
`docs/wip-mmse-ring-notes.md`。

---

## 3. 整链结果（17 个场景）

| 场景 | CGRA 路径 | CPU 路径 | CPU/CGRA | 备注 |
|---|---:|---:|---:|---|
| `nr_mimo` | 405,568 | 9,310,451 | **22.957×** | FFT 占 73%；MMSE 阶段仍 0.93×（见 §2.3.3） |
| `nr_csi` | 202,261 | 4,579,006 | **22.639×** | 链短，几乎全是 FFT |
| `nr_prach` | 109,938 | 1,196,790 | **10.886×** | ZC 相关走阵列 |
| `lte_prach` | 109,966 | 1,192,629 | **10.845×** | 同上 |
| `lte_pdsch` | 2,723,774 | 21,183,908 | **7.777×** | FFT + Turbo 阵列微码 |
| `nr_pdsch_harq` | 3,166,251 | 23,321,365 | **7.366×** | 两次传输的 FFT + LDPC |
| `lte_pusch` | 3,910,576 | 27,257,529 | **6.970×** | FFT + DFT200 + Turbo |
| `nr_ssb` | 3,172,409 | 21,015,018 | **6.624×** | 4 次 FFT + PSS/SSS + Polar |
| `nr_pusch_tp` | 2,855,361 | 18,791,149 | **6.581×** | 360 点 IDFT 已拆成显式 transform 节点 |
| `nr_pdsch` | 3,622,982 | 23,714,242 | **6.546×** | FFT + LDPC |
| `nr_pdcch` | 1,552,133 | 9,793,607 | **6.310×** | 编码留阵列、SC 换 CPU |
| `nr_pusch` | 2,530,930 | 14,196,968 | **5.609×** | FFT + LDPC |
| `nr_pdsch_16qam` | 2,770,790 | 14,306,397 | **5.163×** | 同上 |
| `nr_pdsch_64qam` | 2,865,602 | 14,418,738 | **5.032×** | 同上 |
| `nr_pucch` | 6,827 | 22,966 | **3.364×** | 2 次 ZC 相关 |
| `wifi_11be` | 1,097,205 | 1,283,541 | **1.170×** | DFT256 已下沉；LDPC 仍在 CPU |
| `lte_cell_search` | 1,822,652 | 446,877 | **0.245×** | 阵列版短相关反而慢 4.1× |

---

## 4. 逐阶段（真正换侧的阶段）

> 下表是 **v1 账本（2026-11-12）** 的逐阶段记录，用来说明「哪些阶段真的换了侧」。
> v2 之后 FFT/estimate/Bluestein 的周期都变了，当前值以 §3 与 `build/proto/compare_all.csv` 为准。

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
| nr_mimo | mimo_equalise（MMSE） | 23,440 | 21,759 | **0.93×** |
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
3. **Polar SC / 小区搜索：放 CPU。** 实测阵列分别慢 1.8×、4.1×，共同原因是每次只有
   几十到几百个 tap/RE，却要付一次阵列启动 + DMA。**MMSE 落点已变混合**：求解下沉后
   阶段 61,030→23,440，但 CPU 路径同步降到 21,759——阵列只省了标量求解、却要付 MMIO
   喂数，两条路径在该阶段基本打平；留 CPU 亦可，切阵列也不拖后腿。
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

17 个场景全部双跑；汇总数据在 `build/proto/compare_all.csv`（逐场景×阶段行），
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

---

---

## 9. 频率校正：CPU 实测只有 CGRA 的 1/4

前面所有 `CPU/CGRA` 都是**周期比**（Verilator 实测），隐含两边同频。用 `docs/08` §9 的
同库综合结果校正（45 nm Nangate45、500 MHz 目标、同一 ABC 配方、同一 OpenSTA）：

| | CGRA | CPU (CVA6 完整核) |
|---|---:|---:|
| pre-layout fmax | **1,053 MHz** | **264 MHz**（关键路径在 FPU FMA 级） |
| f_CGRA / f_CPU | — | **≈ 4.0×** |

时间比 = 周期比 × (f_CGRA / f_CPU) ≈ 周期比 × **4.0**。之前"两边都 ~1 GHz、周期比≈时间比"
的猜测是错的：CPU 的关键路径在 FPU 的 FMA 流水级（3.5 ns），而 CGRA 的 PE 数据通路只有
0.94 ns。校正后：

| 场景 | 周期比 | 频率校正后时间比 |
|---|---:|---:|
| `nr_mimo` 整链 | 22.957× | **≈ 92×** |
| `nr_csi` | 22.639× | ≈ 90× |
| `nr_ssb` | 6.624× | ≈ 26× |
| `wifi_11be` | 1.170× | ≈ 4.7× |
| `lte_cell_search` | 0.245× | ≈ 0.98×（打平） |
| `mimo_equalise` 阶段 | 0.93× | ≈ 3.7×（CGRA 明显更快） |

**结论：CGRA 的时间优势约为周期比的 4 倍**——`nr_mimo` 整链从"22.957×"变成"**≈92×**"。
`mimo_equalise` 阶段此前"CPU 略快 0.93×"完全是同频假设的产物，校正后 CGRA 快 3.7×。

> 局限：两个 fmax 都是 pre-layout（布线后约 0.5–0.65×，该系数对两者近似相同，比值受
> 影响小）。CPU 的 264 MHz 依赖手工缓冲树，真实 P&R 可能到 ~300 MHz（比值 3.5×，
> `nr_mimo` ~78×）；但量级不变：**CPU 频率显著低于 CGRA，纯周期比是时间比的低估**。

> 更进一步的对比：把锚点从 CPU 换成**最新研究中的 CGRA 架构通信处理器**
> （清华 JSSC'20 动态 CGRA 基带处理器、Michigan DAP JSSC'25 12nm 256 核、
> 海南大学 TCAS-I'26 16nm ASIP+动态 CGRA、Tampere JSPS'26 五波形统一 CGRA），
> 并下沉到**算子级加速差异**——见 `docs/24`（2026-11-13 重做版）。
