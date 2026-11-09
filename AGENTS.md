# AGENTS.md — 接手手册（给下一个 AI / 下一位开发者）

> 目标：**零上下文接手**。读完这一页 + 跑一条命令，你就知道项目在哪、怎么验、哪里最容易踩。
> 更细的原理见 `docs/`，最新实测数字见本文件 §2（改动后请同步更新）。

---

## 1. 这是什么，现在到哪

RISC-V（CVA6 RV64GC）+ CGRA（4×4 PE，int16 MAC / int32 累加）的**基站基带处理器原型**，
LTE/NR 双模，目标是"通用通信处理器"。全部在 Verilator 里仿真，软件用交叉编译的裸机 C。

当前能力：**从时域样本开始，完整解出 SSB（小区 ID + MIB）、PDSCH 80-bit 传输块、以及 PDCCH 32-bit DCI（含 RNTI 盲检）**，逐环节与 Python 黄金模型比对。
细节指标见 §2；计划与验收标准见 `docs/11-项目计划.md`。

| 里程碑 | 状态 |
|---|---|
| M0 CGRA 微架构 + 微码工具链（15 kernel） | ✅ |
| M1/B2 CVA6 驱动 CGRA（真 SoC + AXI4 突发） | ✅ |
| M2/B2.5 L1 数据面（spad 双页、DMA+描述符环、PLIC、接收链） | ✅ |
| M2.5 FFT 256/1024/4096 + slot 时基 | ✅ |
| P2 第一步 PDSCH SISO（LDPC BG2，模型 + 硬件整链） | ✅ 模型：TB 80 bit → CRC24A → BG2(Zc=20) → 速率匹配/加扰/QPSK → AWGN/两径 → min-sum 译码 → CRC；−6 dB 起 4/4 全过（`docs/09`）。硬件：`run-pdsch`（50k 周期）+ **`run-pdsch-iter`：真实 BG2 一个码块 8 次迭代全在 CGRA 上，1040/1040 硬判决对账通过**（内核 1.59 M 周期 / 仿真 2.03 M）+ **`run-pdsch-full`：时域样本 → FFT → LS/均衡 → QPSK LLR → 解扰 → 解速率匹配 → 8 次 LDPC 迭代 → CRC24A → 80 bit TB**，20 dB / 0 dB 两点 **80/80 TB、1040/1040 硬判决**（3.06 M 周期）；保真度补强：**38.211 7.4.1.1 真 Gold DMRS（`c_init`=0x120002/0x180002）+ 38.212 5.4.2.2 比特交织**，20 dB 下信道估计与软 LLR 与浮点模型逐值对齐 |
| B4-SSB 频域链（PSS/SSS/DMRS/QPSK/Polar/MIB） | ✅ |
| B4-SSB 采样级（4 符号 FFT → N_ID + MIB） | ✅ |
| L1 控制信道 PDCCH/DCI | ✅ **`run-pdcch` 已在硬件跑通 RNTI 盲检**（详见 §2）；上行仍为模型级（`nr_ul.py`） |
| P1 SIB1（Type0-PDCCH → PDSCH → SIB1） | ✅ **`run-sib1` 硬件通过**（SI-RNTI 0xFFFF 单候选；rv=0/2 两点 k0=0/492；0 errors、1040/1040 硬判、TB 80/80、SIB1 字段全对；4.43 M 周期） |
| P2 随机接入（PRACH → RAR → Msg3 → Msg4） | ✅ **`run-prach`（Msg1：ZC 循环相关落 CGRA，139/139 shift 逐位 → 前导 + TA）** + **`run-rach`（4 步全过程一次 SoC 运行：Msg1 → RA-RNTI → Msg2 RAR → Msg3 PUSCH → Msg4 竞争解决 → C-RNTI）**；`tools/nr_rach.py` 为过程级黄金模型（含 RAR/CCCH/CR-ID MAC CE）。详见 §2 两行与 `docs/11` P2 |
| P3 RRC 连接与控制面 | ✅ **`nr_rrc.py`（紧凑 64-bit RRC SDU → 80-bit MAC PDU，5 类消息 + Short BSR/PHR/TA cmd/C-RNTI CE + SRB0→SRB1 状态机）**、**`nr_conn.py`（连接建立过程：RRCSetup → SRB1 → SetupComplete → PUCCH SR → UL grant → BSR/PHR → RRCReconfiguration + TA CE → Complete；5 类注入错误各自如实失败）**、**`run-pucch`（PUCCH 格式 0 落 CGRA，复用 `prach_corr` 只跑 2 个候选 m_CS=0/6，64 个 PE 分部逐位对账）**；ACK=1 / ACK=0 / 0 dB seed 3 三点 `[OK]` |
| P4 下行数据面（MCS 驱动 + 16QAM） | ✅ **`nr_la.py`（38.214 MCS/CQI 表 + CSI 上报 + OLLA 收敛到 10% BLER）**、**`nr_qam.py`（QPSK/16QAM/64QAM Gray 映射 + 整数 max-log LLR，三阶功率归一化）**、**`nr_pdsch_amc.py`（MCS→Qm→PDSCH 全链，5 个 MCS × rv0/2 × 2 SNR 全对，3 类注入门）**、**`run-pdsch-16qam`（`pdsch_full_bm.c -DQAM16`：16QAM max-log 软解调落硬件；4 点 `[OK]`，rv=2 → k0=492，QPSK `run-pdsch-full` 回归不变）** |
| L1/L2 缺口补齐（本轮，模型级为主） | ✅ **HARQ/RV 软合并已上硬件**（`run-pdsch-harq`，rv=0/2 Chase 合并，4.19 M 周期）；`nr_pdcch.py`（DCI+CRC24C+Polar+RNTI 盲检）、`nr_ldpc.py`（Zc=64 分页 + 分层调度）、`nr_mimo.py`（2×2 层映射/预编码/MMSE）、`nr_mac.py`（MAC PDU + HARQ 实体）均**模型级自检通过，未落 CGRA**；剩余缺口见 §6 ；**DCI 驱动双向闭环 `nr_link.py` 已封口**（DL = PDCCH+PDSCH+PUCCH；UL = DCI reserved 10 bit UL grant 驱动的 PUSCH；−8 dB 双向 HARQ 各自救回）；**上行 PUSCH 硬件整链 `run-pusch` 已打通**（20 dB 0 errors、1040/1040 硬判、TB 80/80、3.07 M 周期） |
| P1 真实映射（CP + DC 空置/保护带 + 38.212 速率匹配） | ✅ `run-ssb-fid`：32/32 MIB，4.4 M 周期 |
| PPA 综合（Nangate45 / Yosys+slang / iEDA-STA） | ✅ 逻辑 54,148 µm²、**Fmax 1.05 GHz**（0.911 ns pre-layout，1 GHz +0.05 ns slack）、23.8/28.7/32.1 mW @500/800/1000 MHz；IMEM 已重构为 512×512 宽字 1R1W；**spad 实测每 bank 需 16R/16W（~2.6 mm²，bank 化必须先改 kernel 数据布局）**（`docs/08` §3.2、§4.1） |
| B3 Linux（OpenSBI + 6.6 + BusyBox） | ⚠️ 内核起到 `late_init`，**未到 initramfs/用户态** |
| M3 规模扩展（8×8）+ 自动映射 + LDPC | ⚠️ 8×8 ADL（`cgra/arch/gpcp-8x8-mesh.xml`，每跳 1 拍）+ CGRA-ME 库驱动/自动切块（`cgra/tools/`）已打通：24 槽 BN 切成 18 片在 3×3 上 **18/18 mapped**（48 s）。**规则映射器**：BN 118 拍（v0）/ **155 拍（4 PE/行 link 版，1.61×）**；**CN 172 拍单程序跑完 20 行 → `run-pdsch-bg2` 631,360 → 19,097 周期（≈33×，M1 双页重叠后）**。微码改由 `gen_bn_prog.py` / `gen_cn_prog.py` 生成，bench 只 include 头文件。CGRA-ME 仍给不出可靠落点，退火不可用。见 `docs/10` §7.3~§13 |

---

## 2. 五条命令上手（以及每条该看到什么）

```bash
# 0) 环境：本机必须清掉 LD_LIBRARY_PATH（工具链带的是旧 libstdc++）
env -u LD_LIBRARY_PATH ...            # 所有 verilator / 模型 / make 调用都这样跑

# 1) 一条命令全量验收（2026-11-05 起：分档 + 并行 -j6，16 核 VM）
bash tools/verify.sh quick            # 实测 146 s / 11 passed / 0 failed：kernels + 10 个 SoC 目标
bash tools/verify.sh full             # 全部非 RACH 门：kernels + 16 模型自检 + 31 个 SoC 基准（约 10 min）
bash tools/verify.sh slow             # 再加 run-rach；最近一次 50 门全量 787 s（2026-11-06 00:04，与 nightly 并行）
bash tools/verify.sh changed          # 只跑 git diff 触及的门（实测 P4 diff 6 门 / 129 s；空改动直接退出）
bash tools/verify.sh smart            # **日常默认**：覆盖 slow 档全部门，只重跑输入变化的门（实测全跳过 / 1 s）
bash tools/verify.sh slow --force     # **阶段末/提交前/发版**：忽略缓存全量，唯一可作交付证据
GATES='model-la run-ring' bash tools/verify.sh smart   # 临时人工指定门（不得当全量证据）
bash tools/verify.sh full --linux     # 再加 Linux 启动（10^8 周期，很久）
bash tools/verify.sh full --jobs 8    # 改并行度（默认 6，JOBS= 亦可）
make verify / verify-smart / verify-full / verify-slow / verify-changed   # make 包装
# 并行前提（tools/verify.sh + sim/soc/Makefile）：各 run-* 各写各的 dump/TD，但原本
# 13 个目标都会重写 build/fft/twiddle.bin（并发写会截断）→ 改成 `@test -f ... ||
# python3 fft_plan.py lut ...`，由 verify.sh 预构建阶段统一生成一次；run-prach 与
# run-rach 原本共用 build/rach/prach.td → run-rach 改用 $(RACH_TD).prach。kernels
# 仍在并行池外串行跑（会重建 CGRA bench）。输出按提交顺序折回 build/verify.log。
# 串行时代最后一次全绿 39 passed / 0 failed / 2704 s；并行后 47 门 / 0 failed / 601 s（现 48 门）
# （门数 +8、耗时降至 22%；按单门折算约 5.4×）。

# 3) B3 Linux（长任务；日志可反复评分，不必重跑）
make -C sim/soc linux LINUX_MAX_CYCLES=1e8 LINUX_REQUIRE=3   # 探针：看 init 是否跑起来
make -C sim/soc linux-status                                  # 只重新评分已有日志
make -C sim/soc linux LINUX_TRACE=2000000 LINUX_MAX_CYCLES=7e7 # 采 PC 样，定位"静默启动"
python3 tools/sym_pc.py build/linux_boot.log                   # 用 vmlinux 给 PC 命名

# 2) 只跑某个基准
env -u LD_LIBRARY_PATH make -C sim/soc run-ssbfft

# 4) PPA：综合 + STA/功耗（Nangate45，~2 分钟；结果进 build/syn/<design>-<freq>MHz）
env -u LD_LIBRARY_PATH make -C syn tools                 # 一次性：拉本项目自己的 yosys+slang / iEDA / Nangate45（~2.3 GB，gitignore）
env -u LD_LIBRARY_PATH make -C syn sta CLK_FREQ_MHZ=500
```

验收指标（**期望输出里的关键数字**，改动后若变了要同步本文档）：

| 目标 | 关键期望 |
|---|---|
| `make test`（= `tools/cgra.py test --rebuild`） | `ALL TESTS PASSED`，15 个 kernel 逐位（含 LDPC 的 `ldpc_cn` 52 拍 / `ldpc_bn` 19 拍、PRACH 的 `prach_corr` 128 拍） |
| `run` | matmul 逐位，≈2059 周期 |
| `run-dma` / `run-irq` | 3 / 2 个 slot 的 H 值一致；中断数 4 |
| `run-ring` | 4 个描述符消费、head=4、**0.70 周期/字** |
| `run-rxchain` | 4 slot = **2057 周期**（阵列 1220 + ring 546） |
| `run-fft` | 256/1024/4096 = **4399 / 22956 / 116761** 周期，逐位一致 |
| `run-sync` / `run-sss` / `run-polar` | N_ID2 / N_ID1=200 / 32 bit 全对，partial 逐位。`run-polar` 的 `--nwords` 原是 14（程序写到下标 134，检查器要 135 字）→ 一直报 `dump too short`，已改成 135 |
| `run-ssb` | 1459 runs，2,411,649 周期，H=(32,15)，MIB 逐位（导频 28 个、真实 DMRS `c_init`） |
| `run-ssbfft` | **N_ID2=1、N_ID1=200、MIB 32/32，H=(16,0) 四个符号**，1907 runs，4,378,711 周期（SSB 占 bin 480..719，避开 DC） |
| `run-ssb-fid` | **N_ID2=1、N_ID1=200、i_SSB=3、n_hf=0、DMRS `c_init`=0x12e101、MIB 32/32**，1907 runs，4,411,511 周期（真 38.211 布局：PSS 符号 0 / SSS 符号 2、CP=288、DC 空在 RE 24、72 个导频、38.212 速率匹配 720→512） |
| `run-ssb-slot` | N_ID2=1、N_ID1=200、MIB 32/32；SSS 按 tick 分摊（14 tick）、无 overrun、计时对照 20 ms 预算 |
| `run-fftprobe` | 4096 个 bin 与定点参考**逐位一致** |
| `run-pdsch-bn1` | 单个变量块过 BN 微码：24 个消息槽 + LLR + 硬判决共 **520 个值**逐值等于黄金模型（**1 次阵列运行 / 155 拍 / 2 次 DMA**，仿真总周期 **11,916**）。微码由 `cgra/tools/gen_bn_prog.py`（规则映射器 link 版）生成，**IMEM 走拷贝引擎批量装载**（`gpcp_cgra_imem_load`，逐字写时总周期 25,558，2.15×） |
| `run-pdsch-gather` | 真实块的 CN→重构→gather→**BN→硬判决** 全链逐值对齐（含 spad 对照）；修掉 `V % Zc` 下溢与 `emit` 目标数组两个 bug |
| `run-pdsch-bg2` | **真实 BG2 块**的检查节点全遍历（42 行 × 20 提升行），DMA 分块流式进出：`min1/min2/sign` 与黄金模型逐位一致，**42 次阵列运行 / 84 次 DMA / 19,097 周期**（M1 运行时双页 ping-pong 重叠后；微码由 `gen_cn_prog.py` 生成 + IMEM 批量装载，此前 84 次运行 / 631,360 周期） |
| `run-pdsch-zc64` | **Zc=64 边消息分页 + 分层调度（BG2）**：一整个码块（42 检查节点）的边消息按 **4 页 × 16 提升值**过 16 PE 阵列（CN 复用 `run-pdsch-bg2` 的 86 拍半镜像），CN 结果经 spad 主机窗口回 CPU 后**就地分层更新** `total += c2v_new - c2v_old`。σ=5 时1 次迭代残留 725 个 syndrome、2 次 203、4 次归零，门禁默认 4 次：**672 次阵列运行 / 672 DMA / 4,064,839 周期**，每页 min1/min2/sign 逐位、总消息与 `tools/nr_ldpc.py` 的分层重放逐值一致、syndrome 0、384/384 信息位。`SIGMA=`/`ITERS=`/`SNR=` 可换点 |
| `run-pdsch-bg1-zc64` | **BG1（46×68，Kb=22）Zc=64 分页 + 分层**：官方 38.212 移位表入库后（`tools/data/38.212-bg1-shifts.csv`，先验证 bg2.csv 逐格复现 `BG2_A5` 才采信），CN 微码参数化成 20 个边 bank（19 真边 + 哨兵）、结果到 20/21/22、半镜像 166 拍。σ=5 时 BG1 收敛台阶 1 次 226 个 syndrome / 2 次 3 个 / 4 次 0，门禁默认 4 次：**736 次阵列运行 / 736 DMA / 7,191,459 周期**，每页 min1/min2/sign 逐位、总消息与`tools/nr_ldpc.py` 重放逐值一致、syndrome 0、**1408/1408 信息位**。`SIGMA=`/`ITERS=` 可换点 |
| `run-pdsch-iter` | **P1 整链**：BG2(Zc=20) 一个码块 **8 次迭代全在硬件上**（42 CN + 52 BN/迭代），`check_pdsch_iter.py` **1040/1040 硬判决**与纯 min-sum 一致；**752 次阵列运行 / 1556 次 DMA / 内核 1,585,169 周期**（≈198k/迭代：CN 152k、BN+搬运 589k、D$ 失效+重构 844k），仿真总周期 2,025,561（约 3 分钟，只在 `verify.sh full` 里）。CPU 侧瓶颈是 WT store（≈20 拍/8 字节），三步优化 1.96×（`docs/09` §5.3）；旋转挪不进硬件的原因见 §5.4 |
| `run-pdsch-full` | **PDSCH 整链（样本进 → TB 出）**：`tools/pdsch_full_plan.py` 生成 2 符号 CP+4096 IFFT 的时域样本（平坦增益 + AWGN），SoC 自己做 FFT → RE 提取 → **38.211 7.4.1.1 Gold DMRS 逐符号 LS 估计**（`c_init`=0x120002/0x180002）→ 均衡 → 720 个 QPSK 软 LLR → **38.212 5.4.2.2 反交织** → 38.211 Gold 解扰（`c_init` = n_RNTI<<15）→ 解速率匹配（720→1040，跳过 16 filler + 40 打孔）→ `>>8` 定标 → 复用 `run-pdsch-iter` 的 8 次纯 min-sum 迭代 → CRC24A → 80 bit TB。`check_pdsch_full.py` 硬门 = **DMRS `c_init` + LLR 符号逐位 + 高 SNR 幅值容差 + 1040/1040 硬判决 + CRC + 80/80 TB**；20 dB/seed 1 与 0 dB/seed 3 均通过，0 errors、**3,064,784 / 3,067,508 周期（20 / 0 dB）、976 次阵列运行、1556 次 DMA**。20 dB 下 `chan est` 硬件 `[32,16,31,15]` 与模型逐值相同、bin0 `yw=(2304,6320)` vs 模型 `(2305,6325)`。`make -C sim/soc run-pdsch-full SNR=<db> SEED=<n>` 可换 SNR 与种子；只在 `verify.sh full` 里 |
| `run-pdcch` | **PDCCH/DCI 盲检（硬件）**：1 符号 TD → 4096 FFT → 216 RE / 432 LLR → 38.211 Gold 解扰（`c_init`=N_ID=200）→ **E=432 逆速率匹配 LUT**（`pdcch_luts.h`）→ **CGRA Polar SC（K=56，复用 22 拍 f/g 节点微码）** → 5 个 RNTI 候选 CRC24C 去掩码盲检 → 32 bit DCI（MCS/RV/NDI/HARQ PID/RB/TPC）。12 dB 与 8 dB 两点均 `[OK]`，0 errors、**1,327,171 周期 / 1232 次阵列运行**，LLR 与浮点模型误差 <1%、56/56 信息位、32/32 DCI。`SNR=`/`SEED=` 可换点；只在 `verify.sh full` 里 |
| `run-pdsch-harq` | **HARQ 两次传输**：同一 `pdsch_full_bm.c` 用 `-DHARQ_TX=2` 编译，rv=0/2 对应 k0=0/492；两传输各自 FFT→LS→LLR→反交织→解扰后按循环缓冲坐标 **Chase 合并**，再喂 LDPC。20 dB 下 0 errors、**4,187,691 周期 / 1200 次阵列运行 / 1556 DMA**，解速率匹配 LLR 从单次的 ±612 变 ±1229，1040/1040 硬判决、80/80 TB；单传输 `run-pdsch-full` 回归不变。`SNR=`/`SEED=` 可换点；只在 `verify.sh full` 里 |
| `run-pusch` | **PUSCH 整链（上行，样本进 → TB 出）**：`tools/nr_pusch.py` 生成 2 符号 CP+4096 IFFT 时域样本（38.211 6.4.1.1 PUSCH Gold DMRS，`c_init`=`0x48d22468`/`0x11a42468`，`n_ID`=n_RNTI），SoC 自己做 FFT → RE → DMRS LS → 均衡 → 720 QPSK LLR → 反交织 → 解扰 → 解速率匹配 → 8 次 min-sum LDPC → CRC24A → 80 bit TB。20 dB/seed 1：0 errors、**3,070,613 周期 / 976 次阵列运行 / 1556 DMA**、1040/1040 硬判决、CRC ok、TB 80/80、DMRS `c_init` 与模型逐值一致。默认无变换预编码（CP-OFDM），TD 布局与 `run-pdsch-full` 同构；结果区在 `0x80070000`（`.bss` 尾部到 0x8001_1638，沿用 0x8001_0000 会压到 FFT 程序尾，属侥幸可用，故主动上移）；`SNR=`/`SEED=` 可换点；只在 `verify.sh full` 里 |
| `run-pusch-tp` | **PUSCH DFT-s-OFDM 落硬件**：`nr_pusch.py dump --tp`（发射端 360 点 DFT 扩展 + 1/√M 功率归一化）→ SoC 用 `-DTP` 编 `pusch_bm.c`：每数据 RE 匹配滤波得 360 点平坦向量 → **Q20 定点 360 点 IDFT**（`pusch_idft_tab.h`，`sin=cos(x−90°)`，int64 累加，误差 <0.2 LSB）→ QPSK LLR → 反交织 / 解扰 / 解速率匹配 / 8 次 LDPC / CRC24A。默认 20 dB / seed 1：**0 errors、7,995,677 周期 / 976 阵列 / 1556 DMA**、1040/1040 硬判、CRC ok、TB 80/80、`out[7]=1` 标志 TP 构建；定点 FFT 无 AGC 使信道估计比模型低 ~10%（幅值门改为 0.6–1.6 比例，符号 / 硬判 / CRC 才是硬门）。**支持 `RV=`**（TD 尾部字驱动 `k0 = rv×(USABLE/4)`：rv=0/2 均 `[OK]`，k0=0/492；`-DHARQ_TX=2` 变体仍走 `rv_k0[tx]`）。只在 `verify.sh full` 里 |

| `run-sib1` | **SIB1（Type0-PDCCH + PDSCH）**：`tools/nr_sib1.py` 生成 1 符号 PDCCH（DCI 1_0，SI-RNTI 0xFFFF）+ 2 符号 PDSCH（80-bit 紧凑 SIB1）时域样本；SoC 复用 `link_e2e_bm.c` 整链（`-DSIB1`：单候选 SI-RNTI），解出 DCI → PDSCH → 1040/1040 硬判 → CRC24A → 80-bit SIB1 → 字段。rv=0/2 两点均 `[OK]`，k0=0/492；0 errors、**4.43 M 周期 / 2208 次阵列运行**；SIB1 字段（cell_identity/tac/prach_config_index/msg1_fdm/msg1_freq_start/zero_corr_zone/scs）与配置逐值一致。`RV=`/`SNR=`/`SEED=` 可换点；只在 `verify.sh full` 里 |
| `run-prach` | **Msg1 PRACH 检测落 CGRA**：`prach_corr` 微码（**128 拍 / 16 PE**，由 `tools/gen_prach_prog.py` 生成 `prach_prog.h`）做 38.211 6.3.3 Zadoff-Chu（`L_RA`=139、root u=25、127 tap）的**循环移位相关搜索**：每个 shift 一次阵列运行，16 个 PE 各出一个 int32 部分和、阵列内 butterfly 归约成 (re,im)，CPU 算 \|C\|² 取峰 → delay → 前导号 + TA。6 dB/seed 1：**139/139 shift 与整数模型逐位一致**，峰 d=39 → **preamble 2 / TA 9**（\|C\|²=2.952e17 对次峰 d=38 的 6.401e15，**16.6 dB** 裕度）、**0 errors / 139 次阵列运行 / 279 次 DMA / 4448 个「部分和 vs 阵列归约」交叉校验 / 128 拍程序**。`PRE=`/`TA=`/`SNR=`/`SEED=` 可换点；只在 `verify.sh full` 里 |
| `run-rach` | **4 步随机接入全过程 = 一次 SoC 运行**（`sw/soc/rach_bm.c`，复用 `prach_corr` + Polar SC + LDPC CN/BN 三类微码）：同一程序里 Msg1 ZC 相关（同上）→ **SoC 自己按 occasion 算 RA-RNTI**（38.321 6.1.5）→ Msg2 单候选 Polar 盲检（复用 `run-pdcch` 的 SC + E=432 LUT）→ DCI 的 **rv 驱动 PDSCH k0** → CRC24A → **RAR MAC CE 解析**（BI/RAPID/TA command/27-bit UL grant/TC-RNTI，38.321 6.2.3）→ Msg3 **PUSCH**（FFT 窗口 = **Msg1 实测 TA**、DMRS 6.4.1.1、数据用**解码出的 TC-RNTI** 解扰）→ CRC24A → CCCH SDU → Msg4 DCI 用**同一个 TC-RNTI** 做 CRC 掩码盲检 → PDSCH → **UE Contention Resolution Identity CE vs 程序自己塞进 Msg3 的 SDU**（38.321 5.1.5.3）→ 竞争解决、TC-RNTI 升级为 C-RNTI。默认点（p=2 / TA=9 / OCC=1 / 20 dB / seed 1）：**0 errors、11,826,693 周期、5531 次阵列运行、4947 次 DMA**；Msg1 139/139 相关逐位、RA-RNTI 0x003d、DCI 32/32、Msg2/3/4 各 **1040/1040 硬判决** + CRC24A + TB 全对、TA 命令 9、RAR grant 四字段（rb_start 1 / rb_len 5 / tda 2 / mcs 10）、TC-RNTI=C-RNTI 0x07002、`resolved=1`。第二点 `MSG4_RV=2` 亦 `[OK]`（硬件解出 rv=2 → **k0=492**，11,830,616 周期）。`PRE=`/`TA=`/`OCC=`（换 occasion ⇒ 换 RA-RNTI，二进制按 OCC 命名，不会误用旧构建）/`MSG4_RV=2`（硬编码 k0 过不了）/`SEED=` 可换点；**全套最慢的基准（~9 min）**，为把默认 `full` 压到 ~10 min，已移到 `verify.sh slow`（或 `full --slow`）档 |
| `tools/nr_*.py` 自检 | `nr_pdcch.py`（DCI 32 bit + CRC24C + Polar K=56 + 5 个 RNTI 盲检，8~15 dB 4/4 命中）、`nr_ldpc.py`（Zc=64 分 4 页×16 PE、分层 min-sum，syndrome=0）、`nr_mimo.py`（2×2 MMSE，14 dB 起 0/720 误码）、`nr_mac.py`（MAC PDU 往返 + RV 0→2→3→1 / NDI 翻转）；均在 `verify.sh full` 的模型自检段 ；`nr_link.py`（**DCI 驱动双向闭环**：DL = MAC→PDCCH→PDSCH→CRC→PUCCH→MAC；UL = DCI reserved 10 bit UL grant→PUSCH→CRC，`ul_rv` 真实驱动 PUSCH k0；−8 dB 双向各 2 次重传经 Chase 合并救回，12/0 dB 双向零重传；支持 `--tp` DFT-s-OFDM 上行）；`nr_sib1.py`（**SIB1**：紧凑 80-bit profile + Type0-PDCCH(SI-RNTI)+PDSCH 全链，rv=0/2 × 20/6 dB 全对）；`nr_la.py`/`nr_qam.py`/`nr_pdsch_amc.py`（**P4**：MCS/CQI 表 + OLLA、三阶 QAM、MCS 驱动 PDSCH）；`nr_pdcch_reg.py`（**38.211 7.3.2.3-4 REG/CCE**：bundle 交织 `f(j)=(r·C+c+n_shift) mod N_bundle`、逆映射、CCE 铺满校验、`n_shift` 旋转；补上 PDCCH 门一直跳过的物理映射）；`nr_pc.py`（**上行闭环功控 38.213 7.1.1**：`P0+α·PL+f(TPC)`，80/100/120 dB 路损均在 16–24 步内收敛到目标 ±0.5 dB、无极限环、f() 饱和 ±40 dB、TPC 反号必发散） |
| `run-pdsch-16qam` | **P4 MCS 驱动下行（16QAM 落硬件）**：`tools/nr_pdsch_amc.py` 生成 TD（末尾附 1 个 word 存 rv），SoC 用 `pdsch_full_bm.c -DQAM16` 做 16QAM Gray max-log 软解调（`ref = (\|h\|²/16)×894`、`LLR = (d₁−d₀)/(2·ref)`，与 `nr_qam.py` 逐位一致），只取前 180 个数据 RE（E=720 固定、Qm=4）。默认点 MCS 13 / 20 dB / seed 1：**0 errors、3,178,732 周期 / 976 阵列 / 1556 DMA**、信道估计 `[32,16,31,15]` 与模型逐值相同、1040/1040 硬判、CRC24A、TB 80/80。`RV=2` → **k0=492**、`MCS=10 SNR=10`、`MCS=16 SNR=10 SEED=3` 三点亦 `[OK]`；QPSK `run-pdsch-full` 回归不变。`MCS=`/`RV=`/`SNR=`/`SEED=` 可换点；**rv 由 TD 尾部字在运行时读入**（单传输版 `rv_k0` 硬编码 0、又无 PDCCH 解码，故不用重编译，也避免旧构建服务新激励）；结果区 `0x80070000`；只在 `verify.sh full` 里 |
| `run-pdsch-64qam` | **64QAM（Qm=6）落硬件**：同一 `pdsch_full_bm.c` 用 `-DQAM64` 编译；`tools/nr_pdsch_amc.py` 的 MCS 17–28 走 Qm=6，E=720 只用 **120 个符号 / 前 120 个数据 RE**；C 侧 64QAM Gray max-log（电平 ±1/±3/±5/±7、`QAM_A`=436、`LLR=(d₁−d₀)/(2·ref)`），与 `nr_qam.constellation(6)` 逐位一致。默认 MCS 20 / 25 dB / seed 1：**0 errors、3,270,885 周期 / 976 阵列 / 1556 DMA**、信道估计 `[32,16,31,15]` 与模型逐值相同、1040/1040 硬判、CRC24A、TB 80/80；`RV=2` → **k0=492**、`MCS=24 SNR=25 SEED=3` 亦 `[OK]`。⚠️ 坑：Chase 块的 `#ifdef QAM16` 若不加 `|| defined(QAM64)`，rv≠0 会静默走 `rv_k0[0]=0`（rv=0 能过、rv=2 硬判 509/1040）。`MCS=`/`RV=`/`SNR=`/`SEED=` 可换点；结果区 `0x80070000`；只在 `verify.sh full` 里 |


| `tools/nr_ul.py` 自检 | **上行模型级**：PRACH Zadoff-Chu（L_RA=139）循环相关定时估计 TA=0/5/12/23 全对、PUCCH 格式 0 用 m_CS=0/6 传 1 bit ACK/NACK（0~6 dB 8/8 全对）；未落 CGRA |
| `tools/nr_pusch.py` 自检 | **上行 PUSCH 黄金模型（阶段 A）**：TB→CRC24A→LDPC BG2→速率匹配→QPSK→(可选 DFT-s-OFDM 变换预编码)→OFDM→TD；接收链镜像 PDSCH（含 38.211 6.4.1.1 真 Gold DMRS、6.3.1.1 加扰）；无变换预编码 / DFT-s-OFDM 在 20/0/−6 dB 与 HARQ rv=0/2 均 CRC ok、TB 80/80、0 误码；TD 格式与 PDSCH dump 同构（供阶段 B `run-pusch` 复用） |
| `tools/nr_rach.py` 自检 | **4-step 随机接入过程模型**：SIB1 的 `prach_config_index`→`N_CS`、ZC 相关（8 个 Msg1 点：TA/N_CS 组合，峰-次峰裕度）、RA-RNTI（38.321 6.1.5）、RAR/CCCH/CR-ID MAC CE、Msg3 定时窗口、Msg4 竞争解决；3 次接入全部在**一次 Msg4 内解决**，注入错误（错 TA / 错 TC-RNTI / 错竞争分辨率 ID）分别让对应门**如实失败**。`dump` 子命令产出 `run-prach`/`run-rach` 的时域镜像 |
| `run-pucch` | **PUCCH 格式 0（1-bit SR / HARQ-ACK）落 CGRA**：`tools/nr_pucch.py` 生成定点（`CHIP_SCALE`=2048）时域镜像（139 个接收 (re,im) + 2 个 127-tap 参考），SoC 复用已验证的 **`prach_corr` 微码**（128 拍 / 16 PE）只跑 **2 个候选 m_CS=0/6**，阵列内 butterfly 归约，CPU 比 \|C\|² 判决。默认点（ACK=1 / 6 dB / seed 1）：**0 errors / 2 次阵列运行 / 5 次 DMA / 64 个 PE 分部逐位对账 / 25.9 dB 裕度**；C[0]=(536606450,−4842875) vs C[6]=(3249896,27044604)，与 `nr_ul.py` 浮点模型一致。`ACK=0`（胜者 m_CS=6、bit=0、24.7 dB）、`SNR=0 SEED=3`（14.7 dB）两点亦 `[OK]`。`ACK=`/`SNR=`/`SEED=` 可换点；只在 `verify.sh full` 里 |
| `run-csi` | **CSI-RS + 宽带 CQI 上报落硬件**：`tools/nr_csi.py` 生成 1 符号 CSI-RS（38.211 7.4.1.5 Gold，`c_init`=0xC01，密度 3 → 80 个 RE）→ SoC FFT → 80 点 LS → **相邻差分噪声估计** `Pn=Σ|A[i]−A[i−1]|²/(2(N−1))`（对前端增益不敏感）→ `snr_q8`（分步除法避免 int64 溢出）→ 38.214 门限表选 CQI（`csi_cqi_tab.h`，无需 log）。默认 20 dB：**0 errors、476,691 周期**、SNR 20.59 vs 模型 20.56 dB、CQI 14 一致；0 dB（CQI 3）/ 10 dB（CQI 8）/ 20 dB seed 3 三点 `[OK]`。⚠️ 两个坑：① SNR 参考必须先除以有源 RE 数、再计入 |h|²（否则分别偏 4.8 dB / 7 dB）；② 整数除法不能过早做（`Pn` 会被量化成 0/1）。只在 `verify.sh full` 里 |
| `run-mimo-ce` | **2×2 MIMO 信道估计落硬件**：`tools/nr_mimo_ce.py` 两发射层共享一个符号，60 个导频 RE 按层 FDM 正交（层 0 取偶数、层 1 取奇数，各 30 个，Gold `c_init` 0xC01/0xC02）；2 个接收天线各一次 FFT → 每（天线, 层）30 点 LS → 4 个 `H_SCALE` 定点系数。默认 25 dB：**0 errors、1,015,472 周期**，hw `[14+0j, 5−6j; 5+8j, 12+1j]` vs 模型 `[14+0j, 5−7j; 5+8j, 12+1j]`（仅一处 1 LSB 量化差）；15/5 dB 亦在量化门内。⚠️ LS 是整数输出，量化底 ≈ 1/(|h|·16) ≈ 7%，门限不能按浮点 1% 定。只在 `verify.sh full` 里 |
| `run-mimo` | **2×2 MMSE 接收落硬件**：复用 `run-mimo-ce` 的双天线 TD 与 4 个定点系数，逐数据 RE 解 `(H^H H + λI) s = H^H y`（λ=(g00+g11)/64，一次复数除法，num·conj(det) ~3e16 仍在 int64 内），输出 720 个硬判（每 RE 4 bit）。默认 25 dB：**0 errors、1,246,524 周期**、**720/720 与浮点 MMSE 逐位一致**；15 dB 711/720、10 dB 618/720（判决边界分歧，容差按 SNR 分层 4/16/120）、25 dB seed 3 720/720。checker 同时校验 4 个系数（40% 量化门）、λ>0、数据 RE 数与比特数。只在 `verify.sh full` 里 |



| `tools/nr_rrc.py` / `nr_conn.py` / `nr_pucch.py` 自检 | **P3 模型三件套**：`nr_rrc.py`（5 类紧凑 64-bit RRC SDU + Short BSR/PHR/TA cmd/C-RNTI CE + SRB0→SRB1 状态机，SDU/PDU/TB 逐层往返；错 C-RNTI 被拒）；`nr_conn.py`（连接建立全过程跑真实 PDCCH/PDSCH/PUSCH/PUCCH：RRCSetup→SRB1→SetupComplete→SR→UL grant→BSR/PHR→RRCReconfiguration+TA CE→Complete；**5 类注入错误**（错 C-RNTI / 无 SR / 错 BSR / 错 TA CE / 跳过 SRB1）各自让对应门如实失败，DFT-s-OFDM 上行与 seed 2/3 亦 `[OK]`）；`nr_pucch.py`（4 个 SNR × 16 点全部与 `nr_ul.py` 浮点模型一致，整数相关逐值） |
| `run-pdsch` | LDPC 迭代闭环（CN 50 + BN 12 拍 × 8 次）：`[-1,-1,-1,-1,-1,-1,0,0]` 与黄金模型逐位一致，**50,721 周期**（2 个判决真的被迭代翻转） |
| `run-slot` | 8 slot，抖动 0，零 overrun |
| `tools/sym_pc.py` | 把 `--trace-pc` 的样本按 `vmlinux`（addr2line）命成函数，并报最长停留段 |
| `make snr-sweep` / `make snr-sweep-hw` | SNR 瀑布：模型 −8/−9 dB 0 误比特、−10 dB 1.5、−12 dB 5.4、−14 dB 7.3（8 seed）；硬件 −8/−10 dB 0、−12 dB 7.2（6 seed）、−14 dB 10.2（6 seed）、−16 dB 15.7（3 seed），周期 4,378,71x；`--csv` 可分批累计（`docs/07` §4.16） |
| `make -C sim/soc linux-status` | 分级评分：stage 0..8（OpenSBI→kernel→initramfs→/init→uname→/proc→CGRA 用户态→干净退出），并要求 16 个累加器值正确 |
| `make -C syn sta` | **逻辑面积 54,148 µm²**（5,072 个 flop 占 49.6%）、**Fmax 1.05 GHz**（0.911 ns，pre-layout；1 GHz 下 +0.05 ns slack）、**23.8 mW@500 MHz**（28.7@800、32.1@1000）；存储另算：IMEM 0.138 mm²（512×512 1R1W）、spad ~2.6（现状 16R16W 交叉开关）⇒ 合计 ~2.8 mm²；0.27~0.35 mm² 需先重排 kernel 数据布局，`docs/08` §3 |
| `bash cgra/tools/map.sh cgra/arch/gpcp-8x8-mesh.xml cgra/dfg/fir4.dot 1 ilp 60` | `[map] mapped=yes II=1`（约 3.4 s，cost 279，7 个算子落在 5 个 PE）；`fir8` 会超时，见 `docs/10` §7.4 |
| `python3 cgra/tools/bn_chunks.py --arch cgra/arch/gpcp-3x3-mesh.xml --out build/bn_chunks3` | 24 槽 BN 切成 **18 片全部 mapped**（3×3、II=1，合计 48 s），见 `docs/10` §10 |
| `python3 cgra/tools/rule_map.py --bn` + `python3 cgra/tools/check_rule_bn.py` | 规则映射器自动生成 BN 微码 **118 拍 × 16 PE**，Verilator 台架 **384 个值 0 差异**（手写是 125 拍）；18 个切块也全部模型自检通过，见 `docs/10` §11 |
| `python3 cgra/tools/rule_map.py --bn-split` + `check_rule_bn.py --split` | **link 版**：4 PE/行、消息常驻寄存器，**31 拍/4 行**；Zc=20 行 = 5×31 = **155 拍**（手写是 250，1.61×），台架 500 个值 0 差异，见 `docs/10` §12 |
| `python3 cgra/tools/rule_map.py --cn` + `check_rule_cn.py` | CN（min1/min2/sign）：单程序 **172 拍一次跑完 20 行**，台架 60 个值 0 差异；`run-pdsch-bg2` 从 84 次运行 / 631,360 周期降到 **42 次 / 19,097 周期**（M1 运行时双页重叠后），见 `docs/10` §13~§15 |

---

## 3. 代码地图

| 路径 | 内容 | 关键入口 |
|---|---|---|
| `rtl/cgra/` | CGRA：`cgra_pkg.sv`（ISA/寄存器/参数）、`cgra_pe.sv`、`cgra_array.sv`（spad+link）、`cgra_top.sv`（定序器/拷贝引擎/描述符环/中断）、`cgra_axi_lite.sv` | `cgra_top.sv` 的 `dma_next()` / `D_state` / `ring_tail_n` |
| `rtl/soc/` | `gpcp_soc.sv`：CVA6 + RAM + CGRA 窗口 + PLIC + CLINT + UART | `R_CGRA` 分支、64-bit store 拆分 |
| `sim/cgra/` | 纯 CGRA 台架（AXI 从机模型） | `tb_cgra.cpp`：`--prog/--dma-mem/--dma-ops/--ring-desc/--spad-dump` |
| `sim/soc/` | SoC 台架 + 所有 `run-*` 目标 | `Makefile`（每个目标 = 一条 load + dump + check） |
| `sw/include/` | `gpcp_layout.h`（**编译器生成的布局契约**：几何/位宽/命名区域）、`ldpc_enc_tables.h`（**B1**：4 个 (BG,i_LS) 编码表 + A_V/B_V/RB）、裸机 ABI `gpcp_cgra.h`（spad 算术引用契约）+ **M1 运行时** `gpcp_cgra_rt.h`（双页 ping-pong、`rt_array_run`、`rt_dma_begin/wait`） | `gpcp_cgra_array_start` / `gpcp_cgra_dma_start` / `gpcp_cgra_imem_load` / `rt_*` / `ring_*` |
| `sw/soc/` | 所有 SoC benchmark（每个 = 一个 C 程序） | `ssb_fft_bm.c`（完整采样级链）、`ssb_fid_bm.c`（P1 真映射：CP/DC 空/速率匹配）、`prach_bm.c`（Msg1 ZC 相关）、`rach_bm.c`（**P2：4 步全过程一次运行**，头注释是结果区布局表）、`fft_bm.c`、`ring_bm.c`、`slot_rtos.c`、`pbch_pack_bm.c`（**A2**：纯 CVA6 载荷装配）、`ifft_bm.c`（**A3**：共轭 IFFT + CP）、`ssb_tx_bm.c`（**A4**：4 符号 SSB 发射）、`overhead_bm.c`（**T0**：阵列 run 开销台架）、`ldpc_enc_bm.c`（**B1**：CVA6 LDPC 编码器） |
| `sw/soc/ssb_fft.inc` | 4096 点 FFT 引擎（微码 + 描述符编排） | `fft4096()`、`rev4()` |
| `tools/` | 模型与检查 | `cgra.py`（ISA 编码与阵列几何**由 `cgra/target.yaml` 派生**，对外提供 `enc()`/`OPS`/`SRC` + **15 个手写 kernel（旧路径，按 §6.19 冻结）** + 自检）、`fft_plan.py`（FFT 规划/黄金/LUT）、`ssb_plan.py`（SSB 黄金模型 + 时域样本）、`ssb_plan_p1.py`（P1 真映射模型：CP/DC/速率匹配）、`nr_pbch.py`（38.211/38.212 PBCH 序列与速率匹配 + **A2 发射装配黄金模型** `pbch_pack()` / `dump-pack`，`selftest-pack` 与 `ssb_plan`/`nr_pdcch` 交叉对账）、`gen_luts.py`（生成 `ssb_fid_luts.h`）、`nr_rach.py`（**4-step 随机接入过程模型** + `dump` 产出 `run-prach`/`run-rach` 时域镜像）、`check_*.py`（每个基准的验收器）、`check_pbch_pack.py`（A2 逐段对账）、`nr_ofdm_tx.py`（**A3/A4** 发射波形黄金模型：单/4 符号共轭 TD、`selftest-ssb-tx`）、`check_ifft.py`（A3 逐值对账）、`check_ssb_tx.py`（A4 逐值对账 + 导出自环 TD）、`check_overhead.py`（T0 线性回归）、`gen_ldpc_enc_tables.py`（B1 表生成）、`check_ldpc_enc.py`（B1 逐位对账）、`check_yaml_coupling.py`（**YAML/C 解耦门禁**）、`check_handprog.py`（**微码来源门禁**：禁手写微码）、`cgra/tools/image_spec.py`（kind: image 多子图串接）、`cgra/tools/fft_image.py`（kind: fft_image 蝶形专用调度器）、`verify.sh` |
| `cgra/` | CGRA 自动映射（CGRA-ME 在仓库外）：`arch/`（8×8 与 3×3 ADL）、`dfg/`（示例 DFG） | `tools/rule_map.py`（**DFG→微码 + ISA 自检**：`--bn`/`--bn-split`/`--cn`/`--dfg`）、`tools/gen_{bn,cn}_prog.py`（生成 `sw/soc/pdsch_*_prog.h`）、`tools/bn_chunks.py`、`tools/map.sh`、`tools/check_rule_{bn,cn}.py`；`target.yaml`（硬件真相：几何/ISA/命名区域）与 `tools/target.py`（逻辑地址 → flat 的唯一换算层）、`tools/image_spec.py`、`tools/fft_image.py` |
| `syn/` | PPA 流程（Yosys+slang → Nangate45 → iEDA STA/功耗） | `Makefile`（`make syn` / `make sta CLK_FREQ_MHZ=…`）、`cgra_syn.tcl`、`cgra_top.sdc`；数字与局限见 `docs/08-ppa.md` |
| `docs/` | 01 架构选型 / 02 CGRA ISA / 03 路线图 / 04 环境 / 05 SoC-B2 / 06 Linux-B3 / 07 L1 数据面（**缺陷史在 07 的 4.7/4.15**）/ 08 PPA 综合结果与局限 | — |

---

## 4. 硬规矩（每一条都是踩过的坑，违反就会"悄无声息地错"）

1. **`env -u LD_LIBRARY_PATH`**；Verilator 固定 `--threads 1`（多线程会把这个设计编译错 → CVA6 随机挂死）。
2. **引擎写 RAM 不使 CPU D$ 失效**：任何"引擎写、CPU 读"的缓冲区，两条路任选其一：
   ① **每轮换新地址、永不复用**（polar 节点用 2048 块池；SSS 每假设独立块；`pdsch_iter` 的 `bout` 每块独占一段）；
   ② 读完之前调 **`rt_dcache_flush()`**（`CSR_DCACHE` 0x7c1 置 0 再置 1 → WT missunit 走 FLUSH，
   顺带排空写缓冲，约 4k 拍）。**不要**用"扫大缓冲把旧行挤掉"：D$ 替换是 **LFSR 随机**，不可靠。
   症状是"流程成功、数据全错"，且错的行窗口正好等于另一块缓冲的 cache 足迹。
3. **CPU 写后立刻 DMA 读**：`fence iorw, iorw` + 读回一个字排空写缓冲，否则引擎读到旧值。
4. **CVA6 会把相邻 32-bit store 合并成 64-bit AXI 写**：一次 store 必须表达完整动作
   （用 `NUM_CYCLES` 的 `cycles|page<<16|start<<31`），否则分次写会神秘丢失。
5. **描述符（32 B、8 B 对齐）**：`d[0]=src`、`d[1]=spad|page<<16|out<<17|irq<<18`、
   `d[2]=len`、`d[3]=stride|row<<16`；写完 RAM 要 fence 再推 `RING_TAIL`。引擎自写状态字
   `0xC0DE_0000|len`、`RING_DONE++`、head 前进。
6. **环的 tail** 可以写 `ring_size`（"最后一个的下一个" ≡ 槽 0），RTL 按 `tail mod size` 比较；
   软件等**进度计数器**（`RING_DONE`），不要数中断；W1C 要在 PLIC claim **之后**清。
7. **2D 走位的源必须线性**：`stride/row` 只决定**目标**在 spad 的行布局
   （`row=16, stride=32` → 行 r 落在 bank `8+r` 的 word 0..15）。`row=0` 表示线性。
   行块起点 = 上一行末字 + `stride - row + 1`。
8. **FFT：输出是基-4 位反序** `X[k] = data[rev4(k)]`（**不是** `bitrev12`）；定点引擎归一化 ÷4096，
   输入按 `TD_SCALE` 预放大、输出 ×`(4096/TD_SCALE)` 还原（当前 `TD_SCALE=256` → `<<4`）。
9. **FFT 微码会覆盖 IMEM**：任何"FFT 之后再跑阵列 kernel"的程序必须先重载微码。
10. **阵列是静态调度**：一次最多 512 拍（`IMEM 8192 = 512×16`），无分支/无数据依赖暂停；
    CPU 只负责装微码 + 启动 + 等待。超过 512 拍就拆成多次运行（FFT 4096 = 112 次）。
11. **页分离**：`BUF_PAGE`（阵列）与 `DMA_PAGE`（引擎）同一时刻必须不同页（双缓冲）。
12. **AXI**：valid 拉高保持到 ready；只在 `valid && ready` 计数；引擎单 outstanding、
    突发 ≤ `DMA_BEATS=32` 拍、不跨 4 KiB；`addr[2]` 决定 64 位拍的高低半。
13. **内存**：SoC 有 64 MiB，但裸机 `link.ld` 只声明 512 KiB（栈顶 `0x8008_0000`）。
    大缓冲（SSS 参考 172 KiB、polar 块池 256 KiB、时域样本 128 KiB）放在 `≥0x8010_0000`
    的高区，**不要**放进 `.bss`（会撞栈/描述符区）。
    **`.bss` 还必须停在 `0x8001_0000` 以下**（`run-ssb` 的结果窗口就在那里）：新增静态数组后
    用 `riscv64-unknown-linux-gnu-nm -n build/sw/ssb_bm.elf | tail` 看 `_bss_end`。
    溢出时结果字与 `.bss` 变量互相覆盖，症状是"PSS/SSS 相关值莫名缩小、判错，但 y 网格明明是对的"
    （把第二条 Gold 序列放 0x8006_0000 才修好，见 `docs/07` §4.18）。
14. **定点**：MAC 只有 int16 操作数（`sext16`）+ int32 累加；`|LLR| × 2^DEPTH` 要留在 int32；
    信道估计保留小数（`H_SCALE=16`）；整数除法一律**四舍五入**（截断曾让 4 个符号估成 15/16 混杂）。
15. **任何超时/溢出/池溢出都必须计数并上报**（静默超时是本项目最大隐患）；检查器**不许空转**
    （注入错误必须能失败）。
16. **综合（`syn/`）用 slang 前端**：Yosys 自带前端不认 `import cgra_pkg::*;`，必须
    `yosys -m slang -c syn/cgra_syn.tcl`；`proc` 与 Tcl 内建同名，脚本里要写 `yosys proc`；
    选存储用类型选择 `delete {t:$mem_v2}`（`c:` 是名字选择，删不掉）。推断存储先删掉再综合，
    否则 8192×32 的 IMEM 会展开成 30 万 flop + 兆级读多路选择（>10 分钟综合、>8 GB）。
17. **P1 真映射（`run-ssb-fid`）三件事**：
    ① 时域每个符号是 `[CP=288 | 4096]` 复样本，FFT 必须跑在 **CP 之后的窗口**（`spec[s]`），
    且 in-place 输入/输出用同一指针——写成 `td + s*2*FFT_N` 会同时错窗口和错步长
    （症状：PSS 随机、SSS 判错，但 y 网格"看起来有值"）；
    ② SSB 的 RE r 对应 bin `NFFT/2 - 24 + r`（`re_bin()`），**PSS/SSS/网格三处都要用**，
    只给网格加偏移会让 PSS/SSS 落在噪声上；
    ③ PBCH 扰码要 **E = 720 bit**（速率匹配后），Gold 序列必须按 `RM_LEN` 生成；
    按 `NPOL=512` 只填 512 个会让后 208 个 bit 恒为 0，两个重复副本被反向解扰 → LLR 相消，
    症状是"32 位里只错 2 位、但一半 LLR 掉到 ±64"。

18. **微码只许编译器生成，不许手写**（2026-11-08 立规）。唯一合法路径：

    cgra/kernels/*.yaml  ->  cgra/tools/gen_kernel_prog.py  ->  sw/soc/*_prog.h（带生成横幅）

    `gen_bn_prog.py` / `gen_cn_prog.py` 是它的兼容薄壳，同属合法路径。
    **`sw/` 里出现 `gpcp_cgra_inst()` 就是违规**：`tools/check_handprog.py` 每次 `verify`
    都会跑，且不可被输入指纹缓存跳过；存量 18 项白名单按 P1–P4 分期清退，**只减不增**。
    要新算法就写 YAML，不要写 C —— 手写微码绕过了调度器、绕过了逐值自检，是这类项目
    腐烂最快的地方（同一个 polar node 程序曾被复制 7 份，手写 22 拍 / 编译器 25 拍，
    两条路一起维护）。写 YAML 的成本低于写 C：`--all` 一次编 23 个镜像、每个都自带对拍。

19. **kernel YAML 不许直接描述硬件**（2026-11-08 立规）。硬件真相只在
    `cgra/target.yaml`：阵列规模（4×4 → 16 PE）、spad 几何（32 bank × 32 word）、
    每 PE 寄存器数、ISA 编码表、IMEM 深度、同步 ST/组合 LD 时序，以及命名区域
    （`IN` / `REF` / `OUT` / `LLR` / `MIN1` / `HARD` / `SENTINEL` ...）。
    `cgra/tools/target.py` 是唯一的换算层；kernel YAML 只准写逻辑地址与逻辑并行度：

        leaves: {yr: {at: {region: IN, slot: 0, pe_per_bank: 1}}}
        parallelism: full                    # 不写 instances: 16
        variants: [{name: ..., shift: 16}]  # 字位平移用 slot 里的 SHIFT

    `at.region` / `at.slot` 是逻辑表达式（可用 `k` / `SHIFT` / `W` / `NPES` 与区域名），
    `pe_per_bank` = 一个 bank 承载几个 PE 的数据块。编译器按 target 对每个 PE 求值、
    仿射化成 `(base, stride)` 再调度 + 逐值自检。**YAML 里出现 `addr: {base,stride}`、
    `instances: 16`、`bank: 16` 或 `base_offset` 一律违规**：`tools/check_yaml_coupling.py`
    在每档 verify 的预检里执行（指纹缓存跳过不了），`make yaml-check` 可单独跑。
    换阵列规模只改 target；`tools/cgra.py` / `rule_map.py` / `bank_alloc.py` /
    `gen_kernel_prog.py` / `image_spec.py` 的几何常量全部由它派生，不许再写第二套数字。
    **C 侧同样解耦（同日续）**：`cgra/tools/layout_contract.py` 额外下发
    `sw/include/gpcp_layout.h`（几何 / 位宽 / 命名区域）与
    `sw/soc/<name>_layout.h`（每个 leaf/sink 的 flat 索引宏）。`gpcp_cgra.h` 的
    `GPCP_SPAD_IDX/ADDR/PAGE` 由契约位宽驱动，另加 `GPCP_SPAD_FLAT_ADDR(flat)`；
    C 里不得再写 `bank*32`、裸 `GPCP_SPAD_ADDR(23,7)` 或本地
    `SENTINEL_BANK 30`。`make yaml-check` 同时检查 YAML 与 C 两侧。
    **剩余边界**：`fft_image.py` 的蝶形基址 / 28 拍轮间隔仍在编译器内部；
    RTL 的 spad 几何不在 C 契约里，真正换硬件还要同步 `rtl/cgra/`。

---

## 5. 调试方法论（顺序照做，能省几天）

1. **先在纯 RTL 台架定论，再改 RTL**（`sim/cgra/tb_cgra.cpp` 能直接放图样、跑描述符、回读 spad）。
   历史上"2D 走位 RTL 有 bug"追了三轮，台架逐字回读证明 RTL 一直是对的 —— 真因在软件打包。
2. **验收指标用"逐元素对账"，不要用"决策正确"**：逐 bin（`run-fftprobe`）、逐 PE（16 个累加器
   用 16 位 MAC 重算）、逐行（spad bank 8..23）、逐字（锁存描述符回读）。
   本轮 3 个"成功但错"的 bug 都是这样才暴露的。
3. **黄金模型要异构**：定点参考（`fft_plan.reference_fft`，逐位）＋ 浮点/独立算法（SNR）。
   只用同源模型会互相背书（`rev4` 那个 bug 就是这样躲过了 `run-fft`）。
4. **现象"改了代码结果一字不变"→ 先查构建**：make 依赖/mtime、旧 `.bin`、`--rebuild`、`touch`。
   已知案例：`build/ssb/td.bin` 曾不依赖 `tools/ssb_plan.py`，改模型后硬件仍在跑旧样本
   （表现为"频谱抽取全读到噪声"）。新增数据镜像规则时，**务必把生成它的脚本列为依赖**。
5. **失败先分类再动手**：数据错？时序挂死？性能差？—— 三类各有独立的检查表（见 §7）。
6. **检查器报 `dump too short` / 结果全 0 → 先对"结果区窗口"**：`--nwords` 必须覆盖程序实际写到的最低下标
   （`run-polar` 就因为窗口只有 14 字、程序写到 134 而长期假失败）；同理结果区不能与 `.bss` 叠。

---

## 6. 当前缺口与下一步（按性价比，详见 `docs/11`）

> 已完成项见 §1 里程碑表与 §2 验收指标；本节只列**还没做完**的。标 ✅ 的条目已关闭，保留编号以免 `docs/` 里的引用失效。
>
> **当前主线是 `docs/19` 的 G1 发射侧**（本节 P0–P2 是 P1–P4 时代遗留清单）：
> A1 `run-pbch-tx`（polar 变换落 CGRA）✅、A2 `run-pbch-pack`（MIB→[CRC24C]→frozen→polar→速率匹配 E=720→加扰→360 QPSK，纯 CVA6，双模式逐值对账）✅；
> A3 `run-ifft` ✅、A4 `run-ssb-tx`（4 符号硬件发射 → P1 接收链自环，MIB 32/32）✅、
> T0 `run-overhead` ✅（G1 收口）；B1 `run-ldpc-enc`（BG1/BG2 × Zc=20/64 逐位 + syndrome 0）✅。
> **下一步 G2/B2**：360 符号下行调制映射（`run-dl-mod`）。

**P0（最近）**
1. **B3 Linux — 已冻结（时间盒到期）**：`tools/check_linux.py`（stage 0..8 + 16 个累加器）、
   `make -C sim/soc linux[-status]`、`LINUX_TRACE` + `tools/sym_pc.py` 都已就绪。
   实测结论：**内核能启动、打印、跑 initcall，但在 `arch_initcall` 之后进入 idle，
   `/init` 从未执行**（100 M 周期仍停在 stage 2；PC 采样显示 38–46 M、50–64 M 都在
   `arch_cpu_idle`，中间有一次 `scheduler_tick`）。细节与四个实验见 `docs/06` §10。
   时间盒内 8 个变体（A/B/C/D/E/F/G/G2）全部停在 1.5–2.1 s 内核时间、`/init` 之前；
   已排除 8250 驱动注册、UART 中断冲突、deferred-probe 超时、定时器。**冻结**，解冻动作见
   `docs/06` §10 末尾（内置 initramfs + dyndbg，约 20 分钟编译 + 40 分钟仿真）。
   剩余：UART 输入、真 PLIC 中断、把 stage 7 纳入 `verify.sh --linux`。
2. **采样级链 20 ms 预算**：时基接入**已完成**（`run-ssb-slot`：CLINT slot tick 驱动、
   SSS 336 假设按 14 个 tick 分摊、无 overrun、MIB 32/32，见 §2）。**剩余**只是算力缺口：
   单 SSB 约 4.34 M 周期 @100 MHz ≈ 43 ms，超 20 ms 预算 ~2.2×，
   `tools/check_ssb_slot.py` 会打 `[warn]`；需把计算压进预算（或提高时钟），不必再接线。

**P1（协议保真度）— 主体已完成**
3. ✅ 38.211/38.212 真实化：`run-ssb-fid`（PSS 符号 0 / SSS 符号 2、CP=288、DC 空在 RE 24、
   PBCH DMRS `c_init` 含 `i_SSB`/`n_hf`、38.212 速率匹配 720→512），32/32 MIB、4.41 M 周期。
4. ✅ AWGN + SNR–BER 曲线：`ssb_plan_p1.py` 频域注噪 → `make snr-sweep` / `snr-sweep-hw`
   → `tools/sweep_snr.py`，模型与硬件瀑布已对齐（数字见 §2）。
5. **CFO/TA 估计骨架**：**未做**（先用纯模型，再落到一个 kernel）；仓库里尚无相关文件。

**P2（M3 主体）**
6. 8×8 阵列（bank 32→64/页、spad 8→32 KiB）+ 各 kernel 映射重排；给 4×4 vs 8×8 周期对比。
   **前置**：spad 实测每 bank 需 16R/16W（~2.6 mm²），必须先重排 kernel 数据布局（`docs/08` §3.2）。
7. ✅ IMEM 批量装载 + 运行时重叠已落地：`gpcp_cgra_imem_load` 走拷贝引擎（DMA_CTRL[3]），
   `run-pdsch-bn1` 25,558 → 11,916 周期、`run-pdsch-bg2` 180,191 → 165,088 周期；
   `sw/include/gpcp_cgra_rt.h` 的 M1 双页 ping-pong 再把 bg2 loop 22,562 → 19,097 周期。
8. ✅ PDSCH 整链已跑通且协议保真：`run-pdsch-full` 从时域样本一路解到 80 bit TB，含真 Gold DMRS + 38.212 比特交织（20 dB / 0 dB 两点 1040/1040 硬判决、CRC、TB 全对，3.06 M 周期）；`run-pdsch-iter` 的 BG2 8 次迭代（内核 1.59 M 周期）也已逐值对账。**Z=64 已落硬件**：`run-pdsch-zc64` 把 BG2 一个码块的边消息按 4 页 × 16 提升值分页过阵列（CN 复用 86 拍半镜像），CN 回来后就地做分层 VN 更新；σ=5 时 4 次迭代收敛，每页 min1/min2/sign 逐位 + 总消息与 `tools/nr_ldpc.py` 重放逐值一致（docs/18 §14）。**BG1 也落硬件**：`tools/data/38.212-bg{1,2}-shifts.csv` 把官方移位表录进来（先验证 bg2.csv 能逐格复现 `ldpc5g.BG2_A5` 的 2184 格，再用于 BG1），`run-pdsch-bg1-zc64` 跑 46×68 的 BG1（19 条边 + 哨兵补到 20，结果 bank 20/21/22）：**736 阵列 / 7.19 M 周期 / 1408-1408 信息位**（docs/18 §15）。顺带修正：Zc=64 属 i_LS=0，旧 `run-pdsch-zc64` 用的是 Zc=20 的 i_LS=2 表，现已改成 set 0。
9. 自动映射：**规则映射器已落地**（`cgra/tools/rule_map.py` 的 BN link 版 155 拍 /
   CN 172 拍单程序，台架逐值 0 差异，见 `docs/10` §11~§15）；**剩余**：把 `Program.set`
   当 IR 做"规整 kernel 的约束映射器"，CGRA-ME 仍给不出可靠落点（退火不可用）；
   再谈 MLIR/CGRA-Flow（`docs/01` §2.3）。
   **2026-11-08 更新**：L1/L2 编译器已覆盖协议全部 CGRA kernel（BN/CN/FFT 蝶形/
   chnest/MMSE/PRACH/BPSK/Polar 节点 + 9 级 Polar 编码），`gen_kernel_prog.py --all`
   一次编 20 个、全部 0 差异；PRACH 编译器微码在 `run-prach-cgra` 上 **139/139 逐位一致**
   （129 拍 = 16 PE × 8 tap 部分和 + CPU 归约）。仍缺：QAM 解调落 CGRA、多级 FFT 编排。
10. **定点 FFT 增益的真相（已定位，勿再误判）**：`ssb_fft.inc` 没有 AGC / 块浮点
    缩放，增益取决于**时域波形峰值**。step-1 的恒定 `+QPSK` 导频全同相 → PAPR
    高 → 触发额外缩放，实测幅值只有浮点模型的 0.2×（曾误判为引擎固有缺陷）。
    换成 38.211 真 Gold DMRS 后峰值下降，**20 dB 下信道估计逐值相同
    `[32,16,31,15]`、bin0 `yw=(2304,6320)` vs 模型 `(2305,6325)`、LLR 差 <1%**；
    0 dB 时噪声抬高峰值仍有幅值差，但软比特符号全对、LDPC 照常收敛。
    要彻底消除低 SNR 偏差，需给 FFT 加块浮点/AGC，或按峰值归一化 TD 样本。
11. ✅ **本轮补齐（深度标注）**：HARQ/RV 软合并**已落硬件**（`run-pdsch-harq`）；
    PDCCH/DCI、Zc=64 分页 + 分层 LDPC 调度、2×2 MIMO 层映射/预编码、MAC PDU +
    HARQ 实体均为**模型级**（`tools/nr_*.py`，各自 selftest 通过），未落 CGRA；
    **2×2 MMSE 的向量相关 `b = H^H y` 已落 CGRA**（`run-mimo` 用编译器生成的
    `mmse_2x1` tile，25 dB 720/720 硬判、`mmse_runs=24`，见 `docs/18` §11.5），
    标量 1/det 与 2×2 求解仍在 CVA6。各文件头注释列了刻意保留的简化（Polar 可靠性序列用自然序、
    无 REG/CCE 交织与 PC bit、BG1 官方移位表未内置、H 假设接收端已知）。
12. **仍未做**：BG1 官方 Table 5.3.2-2 移位表（+ 层调度微码）、上行
    CGRA 基准（PUSCH 的 **DFT-s-OFDM 变换预编码变体**尚未落硬件）、RLC/PDCP/SDAP/RRC、QEMU 设备模型与 free5GC 联调（端到端 ping/iperf）。
    （注：PDCCH SoC 基准 `run-pdcch`、上行 PRACH/PUCCH 模型 `nr_ul.py`、**PUSCH 黄金模型 `tools/nr_pusch.py`（阶段 A，含变换预编码自检）**、以及 **PUSCH 硬件整链 `run-pusch`（阶段 B：20 dB 0 errors、1040/1040 硬判、TB 80/80、3.07 M 周期）** 已完成；
     PUSCH 的 DFT-s-OFDM 变换预编码变体与 PRACH/PUCCH 尚未落 CGRA。）

13. ✅ **L1 控制/上行（本轮）**：PDCCH/DCI 盲检**已上硬件**（`run-pdcch`，E=432 逆速率匹配
    LUT + CGRA Polar SC K=56 + 5 个 RNTI 盲检，12/8 dB 两点 `[OK]`，1.33 M 周期）；上行
    PRACH 定时估计与 PUCCH 格式 0 为**模型级**（`tools/nr_ul.py`，selftest 通过）。
    仍未落硬件：REG/CCE 交织、PC bit、PDCCH DMRS、上行 PUSCH 与上行 CGRA 基准。

14. ✅ **DCI 驱动双向闭环（本轮封口）**：`tools/nr_link.py` 把 MAC/HARQ 实体、PDCCH 盲检、PDSCH
    数据面、PUCCH 反馈串成一条受控链路——DCI 字段**真实驱动** rv（k0=0/492）、RNTI（38.211
    加扰）、NDI/ACK-NACK。−8 dB 首传 CRC 失败→NACK→rv=2 重传 Chase 合并救回，12/0 dB 零重传；
    模型自检 `[OK]`。两个硬件半环（run-pdcch / run-pdsch-full）已各自验证，硬件端到端基准
    `run-link-e2e` 已实现（`sw/soc/link_e2e_bm.c`：一次 load 双 TD，SoC 解出 DCI 的 rv 并驱动 PDSCH 循环缓冲 k0，CRC24A + 80-bit TB 验收通过；**rv=0 与 rv=2 均硬件 `[OK]`**，k0 分别 =0/492，证明 DCI→rv→k0 耦合在硬件上成立）。坑：该基准 .bss 含完整 LDPC 工作集（msg/res/llr/g_all 等约 46 KB），结果区须放在 `0x80070000`——沿用 `0x80010000` 会落入 .bss，早期写出的 DCI 字节被后续 PDSCH 大数组覆盖（表象：首尾结果正常、中段垃圾）；`tools/check_link_e2e.py` 的 LLR 符号探针与信道估计探针因 C 与黄金模型 RE 排序不同已改为非致命说明，核心验收以 1040/1040 逐位硬判 + CRC24A + 80-bit TB 为准。**阶段 C（双向）**：`nr_link.py` 增加 UL 方向——gNB 在 DCI 的 10 个 reserved 比特里下发 UL grant（`ul_flag`/`ul_ndi`/`ul_rv`/`ul_mcs`/`ul_rb_start`/`ul_rb_len`），UE 按 grant 发 PUSCH（`tools/nr_pusch.py`），gNB 侧 FFT→6.4.1.1 DMRS LS→LDPC→CRC24A；`ul_rv` 真实驱动 PUSCH k0（0/492），−8 dB 双向各 2 次重传均 Chase 合并救回、12/0 dB 双向零重传；`--tp`（DFT-s-OFDM 上行）亦 `[OK]`。DCI 32 位布局未改（UL grant 走 reserved），run-pdcch / run-link-e2e 回归不受影响。

15. ✅ **P1 SIB1（本轮）**：`tools/nr_sib1.py`（紧凑 80-bit SIB1 profile）+ `run-sib1` 硬件整链。SI-RNTI 0xFFFF 单候选（Type0 CSS，无需盲检），DCI 1_0 → PDSCH → 1040/1040 → CRC24A → 80-bit SIB1 → 字段（cell_identity/tac/prach_config_index/msg1_fdm/msg1_freq_start/zero_corr_zone/scs）；rv=0/2 两点均 `[OK]`（k0=0/492）、0 errors、4.43 M 周期 / 2208 次阵列运行。

16. ✅ **P2 随机接入（本轮，三阶段全交付）**：
    * **阶段 A（模型）**`tools/nr_rach.py`：4-step 过程状态机 + RAR / CCCH / CR-ID MAC CE（38.321 6.2.3 / 5.1.5.3），自检 3 次接入均一次 Msg4 解决，三类注入错误各自让对应门失败。
    * **阶段 B（Msg1 落硬件）**`run-prach` + 第 15 个 kernel `prach_corr`（128 拍，`tools/gen_prach_prog.py` 生成微码）：139/139 shift 逐位、preamble 2 + TA 9、16.6 dB 裕度、0 errors。
    * **阶段 C（过程级）**`run-rach`（`sw/soc/rach_bm.c`）：Msg1 → SoC 自算 RA-RNTI → Msg2 RAR → Msg3 PUSCH（窗口用 Msg1 实测 TA、解扰用解码出的 TC-RNTI）→ Msg4 竞争解决 → C-RNTI，**一次 SoC 运行**；默认点 0 errors、**11,826,693 周期 / 5531 次阵列运行 / 4947 次 DMA**，三条腿各 1040/1040 硬判决；**`MSG4_RV=2` 第二点也已 `[OK]`**（硬件解出 rv=2 → k0=492，11,830,616 周期）。`OCC=` 换 occasion ⇒ 换 RA-RNTI，二进制按 OCC 命名，避免"旧构建服务新激励"。
    * **踩到的三个坑**（前两个属"流程成功、数据错"，只有逐值对账能抓）：① RAR 的 27-bit UL grant 是 **MSB-first 打包、`rsv`(1 bit) 落在 bit 0**，C 侧解析整体高了一拍（`rb_start` 应从 >>20 而非 >>21）——看"译码成不成功"永远发现不了；② 结果区 Msg1 prologue 用的是**绝对下标 13..19**，检查器按 `R_MSG1 + 13..19` 读，读到的是相关值；③ 检查器 `Report.eq/bits/bytes_` 靠**模块级 `vals`** 取数，`main()` 里那句 `global vals` 不是冗余，删掉验收器直接崩。
    * **仍未做**：2-step RACH、Msg3 的**变换预编码（DFT-s-OFDM）变体**（PUCCH 与 RRC 已于第 17 条交付）。

17. ✅ **P3 RRC 连接与控制面（本轮，三阶段全交付）**：
    * **阶段 A（模型）**`tools/nr_rrc.py`：紧凑 64-bit RRC SDU（5 类消息）→ 80-bit MAC PDU，Short BSR / PHR / TA Command / C-RNTI CE，SRB0→SRB1 状态机；`tools/nr_conn.py` 把 P2 的 C-RNTI 接进**真实 PDCCH/PDSCH/PUSCH/PUCCH 链**：RRCSetup → SRB1 → RRCSetupComplete → PUCCH SR → DCI reserved 里的 UL grant → BSR/PHR → RRCReconfiguration + TA CE → Complete，TA 9→11 维护；**5 类注入错误**（错 C-RNTI / 无 SR / 错 BSR / 错 TA CE / 跳过 SRB1）各自让对应门如实失败，DFT-s-OFDM 上行与 seed 2/3 亦 `[OK]`。
    * **阶段 B（PUCCH 落硬件）**`run-pucch` + `tools/nr_pucch.py`：复用 `prach_corr` 微码只跑 2 个候选 m_CS=0/6（不新增微码）；ACK=1 / ACK=0 / 0 dB seed 3 三点 `[OK]`，0 errors、2 次阵列、5 次 DMA、64 个 PE 分部逐位、与 `nr_ul.py` 浮点模型一致。**`ACK=`/`SNR=`/`SEED=` 三个参数直通检查器**，避免"换点却验收旧激励"。
    * **踩到的坑**：新增 Makefile 规则时把 `pucch_bm.elf` 插进了 `sib1_bm.elf` 规则与配方之间，导致 pucch 抢走 `-DSIB1 link_e2e_bm.c` 的配方、编译出 35160 字节的错误二进制而 make 还报 up-to-date；**症状是"编译成功但长度/内容不对"**，靠 `rm -f .bin .elf` 强制重建 + 对比二进制大小才发现。新增带配方的显式规则务必整块粘贴、并核对 `.bin` 大小。
    * **刻意保留的简化**：RRC 用紧凑定长位域而非 38.331 ASN.1 PER（与 P1 SIB1、P2 Msg3 同口径；PER 留给 `docs/11` P6）；单 TTI、无 RLC 分段/PDCP 加密、无 RRC 定时器、无 CSI 上报；PUCCH 沿用 `nr_ul.py` 的 139 点短 ZC / 单 root / 127 tap / AWGN 口径。
    * **仍未做**：P5 上行精化（功控、SRS、TP 硬件），P6 L2 用户面，P7 核心网/端到端，P8 移动性。

18. ✅ **P4 下行数据面（本轮，MCS 驱动 + 16QAM 上硬件）**：
    * **阶段 A（链路自适应）**`tools/nr_la.py`：38.214 Table 5.1.3.1-1 的 29 个 MCS、Table 5.2.2.1-2 的 15 个 CQI、SINR→CQI 上报、CQI→MCS 选择、解析 BLER 瀑布（Shannon 反演 + 实现间隙）、**OLLA 外环**（ACK +0.1 dB / NACK −0.9 dB，稳态 BLER 收敛到 10%）。注意 38.214 表在 MCS 16→17 处频谱效率有 0.4% 回退（16QAM→64QAM 跳变），故单调门只能要求"回退 <1%"；高 SNR 下 MCS 28 饱和属链路能力富余，不能强求 BLER=10%。
    * **阶段 B（高阶 QAM）**`tools/nr_qam.py`：QPSK/16QAM/64QAM 的 38.211 Gray 映射 + 整数 max-log 软解调；电平尺度 2000 / 894 / 436（=2000/√5、2000/√21），**三阶平均功率相等**，`add_channel` 的 SNR 定义因此一致。`ref = (|h|²/H_SCALE)×a`，`LLR = (min_dist(bit=1) − min_dist(bit=0)) / (2·ref)`，截断除法（C 语义）保证与硬件逐位一致；除 `2·ref` 把平方距离拉回线性量级，避免 >>8 后撞 `SENTINEL`。
    * **阶段 C（MCS 驱动 PDSCH 模型 + 16QAM 硬件）**`tools/nr_pdsch_amc.py`：MCS→Qm→38.212 交织（Qm 列）→QAM→RE 网格；E=720 固定，16QAM 只用前 180 个数据 RE（E/Qm=180 符号），LDPC/速率匹配完全不动。`run-pdsch-16qam` = `pdsch_full_bm.c -DQAM16`，C 侧 16QAM 软解调与 Python 逐位一致；**默认点 + RV=2(k0=492) + MCS=10/10 dB + MCS=16/10 dB/seed3 四点 `[OK]`**，信道估计 `[32,16,31,15]` 与模型逐值相同、1040/1040 硬判、TB 80/80；QPSK `run-pdsch-full` 回归 3,065,604 周期 `[OK]` 不变。
    * **踩到的三个坑**：① 星座构造若按"排序后位置"取幅度，跨原点处不是 Gray（16QAM 960/960 只解出 729 位）；必须**从 bit 本身解幅度**（b0=符号、其余为 Gray 幅度）。② min 距离 LLR 若只除 `ref` 而不除 `2·ref`，量级放大到 1e10 会冲垮 `SENTINEL`。③ 单传输版 `rv_k0[0]` 硬编码 0 且无 PDCCH 解码，`RV=2` 必然失败；改为**TD 镜像尾部附 1 个 word 存 rv、SoC 运行时算 k0**，不重编译、也避免旧二进制服务新激励。另有协议层面的坑：**CRC24A 位本身是 LDPC 信息位**，LDPC 收敛到任意合法码字都会让 CRC 通过（把 16QAM 波形当 QPSK 解调时 CRC 仍 ok、TB 仍对，但 534/720 个 LLR 不同）——所以硬件验收必须逐值对硬判/LLR，不能只信 CRC。
    * **刻意保留的简化**：64QAM 仅模型（硬件待 P4 后续）；无 BG1 之外的基图、无 REG/CCE/PC/PDCCH-DMRS、无 CSI-RS 测量（用理想 SINR 上报）、无子带 CQI/PMI/RI、无 256QAM（MCS table 3）；OLLA 的 BLER 用解析近似而非实测 LDPC 瀑布；16QAM 的 RE 利用率只有一半（真实 gNB 会提高 E 或缩减分配）。
    * **仍未做**：P5–P8。`run-mimo` / `run-csi` / Zc=64 / BG1 均已落。

19. **手写微码清退（2026-11-08 完成）**：规则见 §4.18，`tools/check_handprog.py`
    在每档验收的预检里执行。起点 **18 个文件 / 90 处 `gpcp_cgra_inst()`**，
    现为 **0 / 0**，白名单 `ALLOW` 已清空、`PIN = 0`；`cgra.py` 的 14 个手写
    kernel 冻结为模块自测，`gen_prach_prog.py` 与 `prach_prog.h` 已删除，
    `sw/soc/*_prog.h` 全部带生成器横幅。以后新写一条 `gpcp_cgra_inst()` 直接失败。
    * **已清退**：polar node ×7、单抽头复乘 ×4、`cgra_matmul` 示例、
      PSS/SSS 相关 ×5、LDPC CN/VN、PRACH 三门禁、`pdsch_gather` 的 10 边 CN /
      24 槽 BN、`rxchain` 四阶段融合、`fft_bm.c` / `ssb_fft.inc` 的 FFT 引擎。
    * **编译器新增能力**：`variants`（一个 YAML 多个地址变体，
      `shift` + slot 表达式里的 `SHIFT` 平移字位，用于 Zc=20 的 word_base 0/16 两半）；
      `kind: image`（`cgra/tools/image_spec.py`）——多个已编译 DFG 子图按
      `at` / `pe_map` 拼成一张镜像，按 at 分组做跨阶段 spad 串接自检；
      `kind: fft_image`（`cgra/tools/fft_image.py`）——radix-4 蝶形专用调度器，
      与既有 `schedule_cn` / `schedule_bn_split` 同类，输出 448 拍 stage /
      336 拍 block，带独立标量黄金模型逐值自检。FFT 的累加器常驻寄存器约定
      在 RTL 的「同步 ST / 组合 LD」时序下不可重排，通用 DFG 调度器会改变
      结果，因此该 kernel 必须有专用调度器。
    * **全量证据**：`make verify-smart` 58 门 0 failed；`run-fft` 三种尺寸
      逐位匹配黄金模型；`make devcheck` 0 error 0 warn。

20. **YAML 与硬件解耦（2026-11-08）**：31 份 kernel YAML 的 127 处物理 flat 地址、
    26 处物理 bank 号、27 处 `instances: 16`、4 处 `base_offset` 全部迁到
    `at` 逻辑地址 / 命名区域 / `parallelism` / `SHIFT`；新增 `cgra/target.yaml`
    与 `cgra/tools/target.py`，五个编译器模块的硬件常量改由 target 派生。
    验收口径是**34 个 `*_prog.h` 逐字不变** + 每份 YAML 逐项反算地址等价，
    `make yaml-check` 绿（规则见 §4.19）。
    * **C 侧布局契约（同日续）**：`cgra/tools/layout_contract.py` 生成 1 份全局
      `gpcp_layout.h` + 33 份 `<name>_layout.h`；ABI 宏、22 处裸 spad 坐标、
      83 处 DMA `bank*32` 基址与本地布局宏全部改引用契约。全量重编 39 个
      `.bin`、`verify-full` 58 门 0 failed，程序行为不变。

21. **G1/A3 OFDM 发射波形 IFFT + CP（2026-11-09）**：`run-ifft` 落地，
    **零新 kernel / 零 RTL / 零编译器改动**。硬件 FFT 是 Q15、每级 `>>2`、
    N=4096 六级 → 输出 `DFT/4096` 且 base-4 位反序，因此
    `IDFT(X)[n] = conj(FFT_hw(conj(X))[rev4(n)])`；共轭只是 CVA6 的数据准备层。
    * `tools/nr_ofdm_tx.py`：真实 SSB 频域网格 → 共轭 TD → 定点 `planned_fft`
      → rev4 / ×`TD_SCALE` / CP；`selftest-ifft` 把 TX 样本送回接收 FFT 并 `<<4`，
      239 个有效 RE 的 QPSK 符号 **0 错**（幅度 rms 误差 32.9 / 2000）。
    * `sw/soc/ifft_bm.c` 复用 `ssb_fft.inc` 的 `fft4096()`；`check_ifft.py` 对
      **8192 raw 频谱字 + 8768 TX 样本字逐值 0 差**，217098 周期 / 112 次阵列运行。
    * 电平口径：`TD_SCALE=256`，接收侧 `<<4`；定点 IFFT 相对浮点参考 SNR 20.9 dB。
      1938 周期/run 里约 **1456 周期**是 DMA 描述符环 / 页编排；
      T0 台架实测（§6.22）纯阵列 run 固定开销只有 **32 周期**，修正了这里的口径。
    * **后续均已交付**：A4 四符号自环与 T0 台架见 §6.22。

22. **T0 开销台架 + G1/A4 自环（2026-11-09，G1 收口）**：
    * **T0**：`run-overhead` 不改 `rt_array_run`，用 `mtime` 包住它，对
      c={1,8,32,128,448} 各 64 次，拟合 **`T(c)=32.0+1.0045c`（R²=0.99966）**。
      **纯阵列 run 的固定开销只有 32 周期**；之前估的「1.5k 固定开销」几乎全是
      DMA 描述符环 / 页编排（`run-ifft` 1938 周期/run 里约 1456 属此类）。
      优化方向因此从「融合 kernel 减 run 数」修正为**批量化 DMA 描述符**。
    * **A4**：`nr_ofdm_tx.py` 扩到 4 符号；`ssb_tx_bm.c` 复用 `fft4096` 生成
      4×(CP288+4096) 复样本（**35072 字逐值 0 差**，2267434 周期 / 448 runs）；
      `check_ssb_tx.py` 把硬件结果原样导出成 P1 TD，同一门禁接着跑
      `ssb_fid_bm.bin` → **MIB 32/32**、N_ID1=200 / N_ID2=1 / i_SSB=3 /
      DMRS c_init 全对。单门禁两步仿真，避开 verify 并行时的文件竞态。
    * **两个坑**：① 4 次 FFT 复用同一个 CPU 写过的工作区时，D$ 旧行挡住引擎
      写回的结果（符号 1/2/3 共 681 字错）；改为 `--load` 的 4 个独立 RAM 窗口
      原地变换 + 每次 `rt_dcache_flush()` 后归零。② PSS/SSS 是 BPSK（虚部恒 0），
      回环自检不能按 QPSK 二维硬判，否则噪声会误报 61/127 个符号错。

23. **G2/B1 LDPC 编码器（2026-11-09）**：`run-ldpc-enc` 覆盖
    BG1/BG2 × Zc=20/64 四个组合，全部与 `ldpc_nr.encode` **逐位一致、syndrome 0**。
    * `tools/gen_ldpc_enc_tables.py` 生成 `sw/include/ldpc_enc_tables.h`：4 个
      (BG, i_LS) 变体 + 双对角核心参数 `A_V/B_V/RB`，符号带前缀以便共存
      （现有译码头 `ldpc_bg*.h` 未动，避免 `BG2_ROWS`/`bg2_col` 重定义）。
    * `sw/soc/ldpc_enc_bm.c` 纯 CVA6：系统位 → 4 行核心闭式解 p0..p3 →
      逐行移位累加 → syndrome 自检。无阵列、无微码、无 DMA。
    * **实测周期**：BG2/Zc20 97k、BG2/Zc64 270k、BG1/Zc20 152k、**BG1/Zc64 427k**。
      此前估的「≈50 µs」偏小 8 倍（实际 4.27 ms @100 MHz）——编码是
      `46×68×度数×Zc` 的移位累加，不是 5k 次标量操作。20 ms 时隙仍装得下
      单个 CB，但多 CB 要按 T0 结论优化内存搬运，而不是推给阵列。
    * 不落阵列的理由不变：串行依赖强、并行度低，且一次 run 的 DMA 编排 ~1.5k 周期。

**后续（`docs/11` P5–P8）**：上行精化、L2 用户面、QEMU 设备模型、free5GC 联调。

---

## 7. 故障排查表（症状 → 第一件事）

| 症状 | 先查 |
|---|---|
| 结果是 0 / 常数，流程却"成功" | IMEM 是否被上一个 kernel 覆盖；D$ 是否复用旧结果块；微码是否真的装载（读回 IMEM） |
| 结果"一半正确一半错" | 源的布局假设（线性 vs 打包）/**2D stride-row**；页号（`BUF_PAGE` vs `DMA_PAGE`） |
| 参考/系数看起来对，但相关没相干性 | 索引映射（`rev4` vs `bitrev`）、RE 扫描序、正负号表 |
| 程序挂死（等阵列/等描述符） | 是否数中断而不是等进度；W1C 竞态；`RING_DONE`/tail 是否推进；描述符 fence |
| 数据错位一字/两字 | 64 位拍的高低半（`addr[2]`）、`dma_beat` 未归零、突发续接 |
| 结果只在奇/偶 bank | 2D 走位参数、bank/word 映射（`bank*32+word`） |
| Verilator 报错/模型跑不动 | `env -u LD_LIBRARY_PATH` 是否漏了；`--threads` 是否 >1 |
| 改了 RTL 但结果不变 | `tools/cgra.py test --rebuild`；make 依赖/mtime；旧 `.bin` |
| Linux 挂死 | `--threads 1`；`GPCP_DBG` 打印；`--trace-pc` 看 PC |

---

## 8. 改动纪律

- 日常迭代一律 `make verify-smart`（见 §9）；**阶段末 / 提交前 / 发版**必须 `bash tools/verify.sh slow --force` 全量留档。改 RTL/ABI 时缓存自动全失效，无需人工挑选测试。
- 新增 `run-*` 目标时，并行化要求：不得与其它目标共写同一 TD/dump；需要 twiddle 时复用带 `test -f` 守卫的那一行。
- 新增 benchmark：C 程序 + `tools/check_*.py` + `sim/soc/Makefile` 目标 + 顶层 `soc-run-*` + **§2 表格一行**。
- 本文件 §2 的数字与 `docs/07` 的实测数字要同步更新（它们是下一个接手的唯一可信来源）。
- 提交信息用英文短句 + 关键数字（例：`ssb: sample-level PBCH chain ... MIB 32/32, 4.33M cycles`）。

---

## 9. 回归规则（输入指纹驱动，2026-11-05 定）

**核心判据**：一个门的结果是它全部输入的纯函数。**输入逐字节未变 ⇒ 结果必然不变 ⇒ 不必重跑**。
不按"测试名字"手工分类（会漏、会漂移），而由 `tools/verify_cache.py` 从
`sim/soc/Makefile` 目标配方里的 `tools/*.py`、它们的本地 import 闭包、对应 `sw/soc/*.c`
与二进制、以及架构层文件自动发现输入并算 SHA-256 指纹。缓存**只在 `rc=0` 时写入**。

### 依赖分层（决定哪些门必须重跑）

| 层 | 变更文件 | 影响范围 | 处理 |
|---|---|---|---|
| **L0 架构 / ABI / 工具链** | `rtl/**`、`sim/**` 源、`cgra/**`、`tools/cgra.py`、`sw/soc/*_prog.h`、`pdcch_luts.h`、`link.ld`、`start.S`、`sw/soc/Makefile`、`sim/soc/Makefile`、顶层 `Makefile`、`tools/verify.sh`、`sim/soc/build/Vgpcp_soc`、`build/fft/twiddle.bin` | **全部门** | 指纹自动全失效 → 全量，不可跳过 |
| **L1 裸机基准** | `sw/soc/*_bm.c` | 由它编译的门（含 `-D` 变体） | 自动 |
| **L2 激励 / 黄金模型** | `tools/nr_*.py`、`dump_*.py`、公共 `pdsch_plan.py` / `ldpc5g.py` 等 | 引用它的门（含 import 闭包） | 自动 |
| **L3 验收器** | `tools/check_*.py` | 对应 `run-*` | 自动 |
| **L4 纯模型自检** | 仅被 `model-*` 引用的脚本 | 对应 `model-*` | 自动 |
| **L5 文档** | `AGENTS.md`、`docs/**` | 0 门 | 全部 SKIP |
| **L6 无变化** | — | 0 门 | 全部 SKIP |

三个**隐性架构变更**（最易漏，已纳入 L0）：① CGRA 微码 / 查表头等价于改架构；
② `link.ld` / `start.S` / 编译选项变化会让所有裸机程序重链接（历史上踩过 `.bss`
顶到结果区的坑）；③ 仿真器 ABI（`--load` / `--results` 布局）。

### 三条硬规则

1. **日常**：`make verify-smart` —— 声明覆盖 slow 档全部门，只重跑输入变化的门。
   输出里 `SKIP` 必须显式打印并单独计数，**绝不冒充 `PASS`**；汇总形如
   `X passed, Y skipped (cached), Z failed`。
2. **留档**：阶段末 / 提交前 / 发版必须 `bash tools/verify.sh slow --force`
   （忽略缓存、全量）。这是**唯一**可作为"基线完好"证据的运行；
   缓存结果不得写进交付结论。
3. **可审计**：缓存只记 `rc=0`，失败门下次必跑；`--force` 永远绕过缓存；
   `GATES=...` 与 `changed` 属临时人工手段，不算全量证据。缓存在
   `build/verify-cache/`（gitignored，纯本机开发态），
   `python3 tools/verify_cache.py show` 可看每个门的最后 PASS 时间与 `valid/STALE`。

**为什么不用"按测试名分类省跑"**：分类表靠人工维护，新增目标 / 新依赖极易漏；
输入指纹从 Makefile 配方与 import 闭包自动派生，新增依赖会自动进入闭包。
`smart` 依旧声明 slow 全部门覆盖，只是跳过未变的 —— 比手工挑几个测试跑更安全。

实测：3 门首次 1 s；二次 `0 passed, 3 skipped, 0 s`；改 `tools/nr_la.py` 只让
`model-la` 重跑（其余含 `run-rach` 全 SKIP），恢复文件后又回到 SKIP；
改 `sim/soc/Makefile` 则四门全部失效。全量预热 `smart --force`：
**47 passed / 0 failed / 562 s**；紧接着第二次 `smart`：
**0 passed / 47 skipped / 1 s**（47 条缓存全部 `valid`，29 个全局输入文件）。

## 10. 开发工时台账（2026-11-06 定）

<!-- devlog-rule:v1 -->
### 强制规则：开发工时台账

每次开发都必须记录**这项任务花了多久**与**它属于哪个领域**：写 RTL、做综合、验证测试、
写软件（裸机 C / 驱动）、写微码、写协议模型、搭基建、写文档 —— 都算开发。

单一事实来源是 `docs/dev-log.csv`，工具是 `tools/devlog.py`。**不要手写 CSV**，用工具。

```bash
python3 tools/devlog.py start <domain> "<任务>"        # 开工立即记
python3 tools/devlog.py stop  --commit <hash> --evidence "<周期数 / 门数 / [OK] 行>"
python3 tools/devlog.py add --domain <domain> --kind <kind> --minutes N \
        --task "<任务>" --commit <hash> --confidence measured|estimate|backfill
python3 tools/devlog.py report                         # 按领域 / 类型 / 天 / 周汇总
make devcheck                                          # 提交前必须 0 error
```

`domain`（写什么）：`arch` `rtl` `microcode` `model` `sw` `verif` `synth` `pd` `infra` `doc` `wait`
`kind`（为什么花时间）：`feature` `fix` `infra` `experiment` `doc` `wait`

**四条不可协商：**
1. 功能 / 修复 / 基建提交都必须有台账行，`commit` 填提交哈希（多个用 `;` 分隔）。
2. 必须有 `evidence`（周期数、门数、`[OK]`、PASS 汇总至少一项），否则按**未完成**处理。
3. 精度不得伪装：`estimate`（事后估算 ±25%）不许写成 `measured`（有明确起止）。
4. 负结果也要记（`kind=experiment`，可以没有 commit）——它比成功的实验更能防止重复踩坑。

**口径细节**（wall-clock 到验收通过；纯等待单列 `domain=wait`；并行按投入比例不重复计；
单条 ≤3 h 否则拆分；跨领域必须拆成多条）：见 `AGENTS.md` §10。

### 完成定义（DoD）

一个任务要同时满足三条才算完成，缺一条不算：
1. 相关门绿 —— `make verify-smart`，或对应的 `make -C sim/soc run-*`；
2. `make devcheck` **0 error**；
3. 文档与实测数字同步（`AGENTS.md` §2 的数字、`docs/11` 的基线与变更记录）。
<!-- /devlog-rule -->


### 为什么记

估算下一个任务靠的是同类任务的均值，不是感觉；返工黑洞（`fix` 与 `experiment` 占比）
只有记账才看得见；阶段复盘时"这周到底干了什么"要能一句话回答，且每个数字都能追到
提交与日志。

### 两个正交维度

只记"领域"不够 —— "写 RTL 花 3 h"和"修 RTL 的 bug 花 3 h"对下次估算意义完全相反。

| 维度 | 取值 | 回答的问题 |
|---|---|---|
| `domain` 领域 | 见上块（`arch` `rtl` `microcode` `model` `sw` `verif` `synth` `pd` `infra` `doc` `wait`） | **写什么** |
| `kind` 类型 | `feature` `fix` `infra` `experiment` `doc` `wait` | **为什么花时间** |

一条记录只有一个主 `domain`。跨领域就**拆成多条**（模型 3 h + 硬件 4 h + checker 1 h
是三条，不是一条 8 h）—— 这个拆分正是本规则的价值所在。

### 计时口径（不按这个记，数据不可比）

1. **wall-clock**：从动手到**验收通过**，含调试、返工、等二进制的零碎时间。
2. **不含纯等待**：后台回归/夜间长跑期间若在做别的任务，时间记到那个任务；若只是干等，
   单独记 `domain=wait, kind=wait`。总时长因此不会虚高。
3. **并行按投入比例**，不重复计数。总和可以小于墙钟，不可以大于墙钟。
4. **精度必须标注**：`measured` / `estimate`（±25%）/ `backfill`（追溯基线）。
   `estimate` 不得写成 `measured`，`backfill` 不得被引用为实测。
5. **单条不超过 3 h**，超了说明中途穿插了别的活，必须拆。

### `check` 校验什么（门禁，不是装饰）

枚举合法 · `minutes` 与起止自洽（±5 min）· 每日合计 ≤ 24 h · 最近 15 个非文档提交
都有归属（`devlog:` / `docs:` 开头豁免）· **六个入口文档的规则块字节一致**。

### 首个基线（2026-11-05 21:40 → 11-06 01:45，19 条）

| 领域 | 时长 | 个均 | 占比 |
|---|---|---|---|
| `sw` 裸机 C | 1.4 h / 6 条 | 14 min | 34.7% |
| `infra` 工具链与仿真 | 1.3 h / 3 条 | **26 min** | 31.8% |
| `model` 协议模型 | 1.0 h / 8 条 | 8 min | 25.3% |
| `wait` 等待 | 0.2 h | 13 min | 5.3% |
| `verif` 验证 | 0.1 h | 7 min | 2.9% |

结论：`sw` 与 `model` 个均只有 8–14 min，`infra` 却要 26 min —— 单门硬件的边际成本已
很低，真正的开销在工具链改造上，后续排期据此加权。（本批全为 `estimate`，仅作基线。）

### 入口文档同步机制

上面那份规则块是**同一份字节**，内联在下面 6 个文件里，覆盖主流 AI 工具的读取入口：

| 文件 | 谁读它 |
|---|---|
| `AGENTS.md` | Codex / 通用 agent（**唯一事实来源**） |
| `README.md` | 人 + 会读 README 的 agent |
| `CLAUDE.md` | Claude Code |
| `GEMINI.md` | Gemini CLI |
| `.cursorrules` | Cursor |
| `.github/copilot-instructions.md` | GitHub Copilot |

`devcheck` 会比对六处的规则块：**任何一处改了口径没同步 → error**；文件缺失 → warn。
改规则时**只改 `AGENTS.md` 里那一块**，然后：

```bash
python3 tools/gen_ai_docs.py sync    # 传播到其余五个入口（幂等）
make devcheck                        # 复核六处一致
```

清单在 `tools/devlog.py` 的 `ENTRY_DOCS`；新增 AI 工具时加一行即可。
