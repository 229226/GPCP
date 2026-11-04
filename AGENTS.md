# AGENTS.md — 接手手册（给下一个 AI / 下一位开发者）

> 目标：**零上下文接手**。读完这一页 + 跑一条命令，你就知道项目在哪、怎么验、哪里最容易踩。
> 更细的原理见 `docs/`，最新实测数字见本文件 §2（改动后请同步更新）。

---

## 1. 这是什么，现在到哪

RISC-V（CVA6 RV64GC）+ CGRA（4×4 PE，int16 MAC / int32 累加）的**基站基带处理器原型**，
LTE/NR 双模，目标是"通用通信处理器"。全部在 Verilator 里仿真，软件用交叉编译的裸机 C。

当前能力：**从时域样本开始，完整解出一整块 SSB（小区 ID + MIB）**，逐环节与 Python 黄金模型比对。
细节指标见 §2；路线与验收标准见 `docs/03-路线图.md`。

| 里程碑 | 状态 |
|---|---|
| M0 CGRA 微架构 + 微码工具链（14 kernel） | ✅ |
| M1/B2 CVA6 驱动 CGRA（真 SoC + AXI4 突发） | ✅ |
| M2/B2.5 L1 数据面（spad 双页、DMA+描述符环、PLIC、接收链） | ✅ |
| M2.5 FFT 256/1024/4096 + slot 时基 | ✅ |
| P2 第一步 PDSCH SISO（LDPC BG2，模型 + 硬件整链） | ✅ 模型：TB 80 bit → CRC24A → BG2(Zc=20) → 速率匹配/加扰/QPSK → AWGN/两径 → min-sum 译码 → CRC；−6 dB 起 4/4 全过（`docs/09`）。硬件：`run-pdsch`（50k 周期）+ **`run-pdsch-iter`：真实 BG2 一个码块 8 次迭代全在 CGRA 上，1040/1040 硬判决对账通过**（内核 1.59 M 周期 / 仿真 2.03 M）+ **`run-pdsch-full`：时域样本 → FFT → LS/均衡 → QPSK LLR → 解扰 → 解速率匹配 → 8 次 LDPC 迭代 → CRC24A → 80 bit TB**，20 dB / 0 dB 两点 **80/80 TB、1040/1040 硬判决**（3.06 M 周期）；保真度补强：**38.211 7.4.1.1 真 Gold DMRS（`c_init`=0x120002/0x180002）+ 38.212 5.4.2.2 比特交织**，20 dB 下信道估计与软 LLR 与浮点模型逐值对齐 |
| B4-SSB 频域链（PSS/SSS/DMRS/QPSK/Polar/MIB） | ✅ |
| B4-SSB 采样级（4 符号 FFT → N_ID + MIB） | ✅ |
| P1 真实映射（CP + DC 空置/保护带 + 38.212 速率匹配） | ✅ `run-ssb-fid`：32/32 MIB，4.4 M 周期 |
| PPA 综合（Nangate45 / Yosys+slang / iEDA-STA） | ✅ 逻辑 54,148 µm²、**Fmax 1.05 GHz**（0.911 ns pre-layout，1 GHz +0.05 ns slack）、23.8/28.7/32.1 mW @500/800/1000 MHz；IMEM 已重构为 512×512 宽字 1R1W；**spad 实测每 bank 需 16R/16W（~2.6 mm²，bank 化必须先改 kernel 数据布局）**（`docs/08` §3.2、§4.1） |
| B3 Linux（OpenSBI + 6.6 + BusyBox） | ⚠️ 内核起到 `late_init`，**未到 initramfs/用户态** |
| M3 规模扩展（8×8）+ 自动映射 + LDPC | ⚠️ 8×8 ADL（`cgra/arch/gpcp-8x8-mesh.xml`，每跳 1 拍）+ CGRA-ME 库驱动/自动切块（`cgra/tools/`）已打通：24 槽 BN 切成 18 片在 3×3 上 **18/18 mapped**（48 s）。**规则映射器**：BN 118 拍（v0）/ **155 拍（4 PE/行 link 版，1.61×）**；**CN 172 拍单程序跑完 20 行 → `run-pdsch-bg2` 631,360 → 19,097 周期（≈33×，M1 双页重叠后）**。微码改由 `gen_bn_prog.py` / `gen_cn_prog.py` 生成，bench 只 include 头文件。CGRA-ME 仍给不出可靠落点，退火不可用。见 `docs/10` §7.3~§13 |

---

## 2. 五条命令上手（以及每条该看到什么）

```bash
# 0) 环境：本机必须清掉 LD_LIBRARY_PATH（工具链带的是旧 libstdc++）
env -u LD_LIBRARY_PATH ...            # 所有 verilator / 模型 / make 调用都这样跑

# 1) 一条命令全量验收（推荐先跑这个，确认基线）
bash tools/verify.sh quick            # 实测 ~320 s（2026-11-04 复验：11 passed / 0 failed）：14 kernel + 8 个 SoC 目标（含 run-ssb-fid / run-ssb-slot）
bash tools/verify.sh full             # 实测 ~1230 s（run-pdsch-iter 优化后约少 4 分钟）：14 kernel + 21 个 SoC 基准（含 run-pdsch-full）
bash tools/verify.sh full --linux     # 再加 Linux 启动（10^8 周期，很久）
make verify / make verify-full        # 同上两个的 make 包装

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
| `make test`（= `tools/cgra.py test --rebuild`） | `ALL TESTS PASSED`，14 个 kernel 逐位（含 LDPC 的 `ldpc_cn` 52 拍 / `ldpc_bn` 19 拍） |
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
| `run-pdsch-iter` | **P1 整链**：BG2(Zc=20) 一个码块 **8 次迭代全在硬件上**（42 CN + 52 BN/迭代），`check_pdsch_iter.py` **1040/1040 硬判决**与纯 min-sum 一致；**752 次阵列运行 / 1556 次 DMA / 内核 1,585,169 周期**（≈198k/迭代：CN 152k、BN+搬运 589k、D$ 失效+重构 844k），仿真总周期 2,025,561（约 3 分钟，只在 `verify.sh full` 里）。CPU 侧瓶颈是 WT store（≈20 拍/8 字节），三步优化 1.96×（`docs/09` §5.3）；旋转挪不进硬件的原因见 §5.4 |
| `run-pdsch-full` | **PDSCH 整链（样本进 → TB 出）**：`tools/pdsch_full_plan.py` 生成 2 符号 CP+4096 IFFT 的时域样本（平坦增益 + AWGN），SoC 自己做 FFT → RE 提取 → **38.211 7.4.1.1 Gold DMRS 逐符号 LS 估计**（`c_init`=0x120002/0x180002）→ 均衡 → 720 个 QPSK 软 LLR → **38.212 5.4.2.2 反交织** → 38.211 Gold 解扰（`c_init` = n_RNTI<<15）→ 解速率匹配（720→1040，跳过 16 filler + 40 打孔）→ `>>8` 定标 → 复用 `run-pdsch-iter` 的 8 次纯 min-sum 迭代 → CRC24A → 80 bit TB。`check_pdsch_full.py` 硬门 = **DMRS `c_init` + LLR 符号逐位 + 高 SNR 幅值容差 + 1040/1040 硬判决 + CRC + 80/80 TB**；20 dB/seed 1 与 0 dB/seed 3 均通过，0 errors、**3,064,784 / 3,067,508 周期（20 / 0 dB）、976 次阵列运行、1556 次 DMA**。20 dB 下 `chan est` 硬件 `[32,16,31,15]` 与模型逐值相同、bin0 `yw=(2304,6320)` vs 模型 `(2305,6325)`。`make -C sim/soc run-pdsch-full SNR=<db> SEED=<n>` 可换 SNR 与种子；只在 `verify.sh full` 里 |
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
| `sw/include/` | 裸机 ABI `gpcp_cgra.h`（寄存器内联函数）+ **M1 运行时** `gpcp_cgra_rt.h`（双页 ping-pong、`rt_array_run`、`rt_dma_begin/wait`） | `gpcp_cgra_array_start` / `gpcp_cgra_dma_start` / `gpcp_cgra_imem_load` / `rt_*` / `ring_*` |
| `sw/soc/` | 所有 SoC benchmark（每个 = 一个 C 程序） | `ssb_fft_bm.c`（完整采样级链）、`ssb_fid_bm.c`（P1 真映射：CP/DC 空/速率匹配）、`fft_bm.c`、`ring_bm.c`、`slot_rtos.c` |
| `sw/soc/ssb_fft.inc` | 4096 点 FFT 引擎（微码 + 描述符编排） | `fft4096()`、`rev4()` |
| `tools/` | 模型与检查 | `cgra.py`（微码生成器 + 14 kernel + 自检）、`fft_plan.py`（FFT 规划/黄金/LUT）、`ssb_plan.py`（SSB 黄金模型 + 时域样本）、`ssb_plan_p1.py`（P1 真映射模型：CP/DC/速率匹配）、`nr_pbch.py`（38.211/38.212 PBCH 序列与速率匹配）、`gen_luts.py`（生成 `ssb_fid_luts.h`）、`check_*.py`（每个基准的验收器）、`verify.sh` |
| `cgra/` | CGRA 自动映射（CGRA-ME 在仓库外）：`arch/`（8×8 与 3×3 ADL）、`dfg/`（示例 DFG） | `tools/rule_map.py`（**DFG→微码 + ISA 自检**：`--bn`/`--bn-split`/`--cn`/`--dfg`）、`tools/gen_{bn,cn}_prog.py`（生成 `sw/soc/pdsch_*_prog.h`）、`tools/bn_chunks.py`、`tools/map.sh`、`tools/check_rule_{bn,cn}.py` |
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

## 6. 当前缺口与下一步（按性价比，详见 `docs/03`）

> 已完成项见 §1 里程碑表与 §2 验收指标；本节只列**还没做完**的。标 ✅ 的条目已关闭，保留编号以免 `docs/` 里的引用失效。

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
8. ✅ PDSCH 整链已跑通且协议保真：`run-pdsch-full` 从时域样本一路解到 80 bit TB，含真 Gold DMRS + 38.212 比特交织（20 dB / 0 dB 两点 1040/1040 硬判决、CRC、TB 全对，3.06 M 周期）；`run-pdsch-iter` 的 BG2 8 次迭代（内核 1.59 M 周期）也已逐值对账。**剩下** BG1 / Z=64（边消息分页、层调度），与新建 `tools/nr_ldpc.py` 逐位对齐（该文件尚未创建）。
9. 自动映射：**规则映射器已落地**（`cgra/tools/rule_map.py` 的 BN link 版 155 拍 /
   CN 172 拍单程序，台架逐值 0 差异，见 `docs/10` §11~§15）；**剩余**：把 `Program.set`
   当 IR 做"规整 kernel 的约束映射器"，CGRA-ME 仍给不出可靠落点（退火不可用）；
   再谈 MLIR/CGRA-Flow（`docs/01` §2.3）。
10. **定点 FFT 增益的真相（已定位，勿再误判）**：`ssb_fft.inc` 没有 AGC / 块浮点
    缩放，增益取决于**时域波形峰值**。step-1 的恒定 `+QPSK` 导频全同相 → PAPR
    高 → 触发额外缩放，实测幅值只有浮点模型的 0.2×（曾误判为引擎固有缺陷）。
    换成 38.211 真 Gold DMRS 后峰值下降，**20 dB 下信道估计逐值相同
    `[32,16,31,15]`、bin0 `yw=(2304,6320)` vs 模型 `(2305,6325)`、LLR 差 <1%**；
    0 dB 时噪声抬高峰值仍有幅值差，但软比特符号全对、LDPC 照常收敛。
    要彻底消除低 SNR 偏差，需给 FFT 加块浮点/AGC，或按峰值归一化 TD 样本。

**P3**：DL-SCH 一条链（LDPC→速率匹配→QAM→层映射→RE→OFDM）、QEMU 设备模型、free5GC 联调。

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

- 改 RTL/ABI → 至少 `bash tools/verify.sh full`；改 kernel/微码 → `make test` + 相关 `run-*`。
- 新增 benchmark：C 程序 + `tools/check_*.py` + `sim/soc/Makefile` 目标 + 顶层 `soc-run-*` + **§2 表格一行**。
- 本文件 §2 的数字与 `docs/07` 的实测数字要同步更新（它们是下一个接手的唯一可信来源）。
- 提交信息用英文短句 + 关键数字（例：`ssb: sample-level PBCH chain ... MIB 32/32, 4.33M cycles`）。
