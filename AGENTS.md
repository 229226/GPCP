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
| M0 CGRA 微架构 + 微码工具链（13 kernel） | ✅ |
| M1/B2 CVA6 驱动 CGRA（真 SoC + AXI4 突发） | ✅ |
| M2/B2.5 L1 数据面（spad 双页、DMA+描述符环、PLIC、接收链） | ✅ |
| M2.5 FFT 256/1024/4096 + slot 时基 | ✅ |
| B4-SSB 频域链（PSS/SSS/DMRS/QPSK/Polar/MIB） | ✅ |
| B4-SSB 采样级（4 符号 FFT → N_ID + MIB） | ✅ |
| B3 Linux（OpenSBI + 6.6 + BusyBox） | ⚠️ 内核起到 `late_init`，**未到 initramfs/用户态** |
| M3 规模扩展（8×8）+ 自动映射 + LDPC | ⬜ 未开始 |

---

## 2. 五条命令上手（以及每条该看到什么）

```bash
# 0) 环境：本机必须清掉 LD_LIBRARY_PATH（工具链带的是旧 libstdc++）
env -u LD_LIBRARY_PATH ...            # 所有 verilator / 模型 / make 调用都这样跑

# 1) 一条命令全量验收（推荐先跑这个，确认基线）
bash tools/verify.sh quick            # 实测 ~110 s：13 kernel + 4 个 SoC 目标
bash tools/verify.sh full             # 实测 ~230 s：全部 13 个 SoC 基准（14 步全 PASS）
bash tools/verify.sh full --linux     # 再加 Linux 启动（10^8 周期，很久）
make verify / make verify-full        # 同上两个的 make 包装

# 2) 只跑某个基准
env -u LD_LIBRARY_PATH make -C sim/soc run-ssbfft
```

验收指标（**期望输出里的关键数字**，改动后若变了要同步本文档）：

| 目标 | 关键期望 |
|---|---|
| `make test`（= `tools/cgra.py test --rebuild`） | `ALL TESTS PASSED`，13 个 kernel 逐位 |
| `run` | matmul 逐位，≈2059 周期 |
| `run-dma` / `run-irq` | 3 / 2 个 slot 的 H 值一致；中断数 4 |
| `run-ring` | 4 个描述符消费、head=4、**0.70 周期/字** |
| `run-rxchain` | 4 slot = **2057 周期**（阵列 1220 + ring 546） |
| `run-fft` | 256/1024/4096 = **4399 / 22956 / 116761** 周期，逐位一致 |
| `run-sync` / `run-sss` / `run-polar` | N_ID2 / N_ID1=200 / 32 bit 全对，partial 逐位 |
| `run-ssb` | 1459 runs，2,414,474 周期，MIB 逐位 |
| `run-ssbfft` | **N_ID2=1、N_ID1=200、MIB 32/32**，1907 runs，4,335,553 周期 |
| `run-fftprobe` | 4096 个 bin 与定点参考**逐位一致** |
| `run-slot` | 8 slot，抖动 0，零 overrun |

---

## 3. 代码地图

| 路径 | 内容 | 关键入口 |
|---|---|---|
| `rtl/cgra/` | CGRA：`cgra_pkg.sv`（ISA/寄存器/参数）、`cgra_pe.sv`、`cgra_array.sv`（spad+link）、`cgra_top.sv`（定序器/拷贝引擎/描述符环/中断）、`cgra_axi_lite.sv` | `cgra_top.sv` 的 `dma_next()` / `D_state` / `ring_tail_n` |
| `rtl/soc/` | `gpcp_soc.sv`：CVA6 + RAM + CGRA 窗口 + PLIC + CLINT + UART | `R_CGRA` 分支、64-bit store 拆分 |
| `sim/cgra/` | 纯 CGRA 台架（AXI 从机模型） | `tb_cgra.cpp`：`--prog/--dma-mem/--dma-ops/--ring-desc/--spad-dump` |
| `sim/soc/` | SoC 台架 + 所有 `run-*` 目标 | `Makefile`（每个目标 = 一条 load + dump + check） |
| `sw/include/` | 裸机 ABI `gpcp_cgra.h`（寄存器内联函数） | `gpcp_cgra_array_start` / `gpcp_cgra_dma_start` / `ring_*` |
| `sw/soc/` | 所有 SoC benchmark（每个 = 一个 C 程序） | `ssb_fft_bm.c`（完整采样级链）、`fft_bm.c`、`ring_bm.c`、`slot_rtos.c` |
| `sw/soc/ssb_fft.inc` | 4096 点 FFT 引擎（微码 + 描述符编排） | `fft4096()`、`rev4()` |
| `tools/` | 模型与检查 | `cgra.py`（微码生成器 + 13 kernel + 自检）、`fft_plan.py`（FFT 规划/黄金/LUT）、`ssb_plan.py`（SSB 黄金模型 + 时域样本）、`nr_seq.py`（38.211/38.212 序列）、`check_*.py`（每个基准的验收器）、`verify.sh` |
| `docs/` | 01 架构选型 / 02 CGRA ISA / 03 路线图 / 04 环境 / 05 SoC-B2 / 06 Linux-B3 / 07 L1 数据面（**缺陷史在 07 的 4.7/4.15**） | — |

---

## 4. 硬规矩（每一条都是踩过的坑，违反就会"悄无声息地错"）

1. **`env -u LD_LIBRARY_PATH`**；Verilator 固定 `--threads 1`（多线程会把这个设计编译错 → CVA6 随机挂死）。
2. **引擎写 RAM 不使 CPU D$ 失效**：任何"引擎写、CPU 读"的缓冲区**每轮必须换新地址、永不复用**
   （polar 节点用 2048 块池；SSS 每假设独立块）。否则读到旧缓存行 —— 症状是"流程成功、数据全错"。
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
14. **定点**：MAC 只有 int16 操作数（`sext16`）+ int32 累加；`|LLR| × 2^DEPTH` 要留在 int32；
    信道估计保留小数（`H_SCALE=16`）；整数除法一律**四舍五入**（截断曾让 4 个符号估成 15/16 混杂）。
15. **任何超时/溢出/池溢出都必须计数并上报**（静默超时是本项目最大隐患）；检查器**不许空转**
    （注入错误必须能失败）。

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
5. **失败先分类再动手**：数据错？时序挂死？性能差？—— 三类各有独立的检查表（见 §7）。

---

## 6. 当前缺口与下一步（按性价比，详见 `docs/03`）

**P0（最近）**
1. **B3 Linux 收尾**：`--max-cycles` 提到 1e8 级后台跑；`tools/check_linux.py` 解析串口日志
   （判 `=== GPCP Linux (CVA6 + CGRA) ===`、`uname -a`、`/bin/cgra_test`）；UART 输入 + 真 PLIC 中断。
2. **采样级链接入 slot 时基**：4 个符号按 tick 流水（复用 `slot_rtos.c`），SSS 假设按批分摊，
   使"每 SSB"落在 20 ms 预算内（当前 4.34 M 周期 @100 MHz ≈ 43 ms，差 ~2.2×）。

**P1（协议保真度）**
3. 38.211/38.212 真实化：PSS/SSS 的真实 k 映射（含 DC 空洞/保护带，现在 `subcarriers()=r→r` 是简化）、
   PBCH DMRS 的 `c_init`（含 `i_ssb`/`n_hf`）、速率匹配、CP 加入。
4. AWGN + SNR–BER 曲线（`ssb_plan.py` 注噪 → 硬件跑同一批样本 → `tools/sweep_snr.py`）。
5. CFO/TA 估计骨架（先用纯模型，再落到一个 kernel）。

**P2（M3 主体）**
6. 8×8 阵列（bank 32→64/页、spad 8→32 KiB）+ 各 kernel 映射重排；给 4×4 vs 8×8 周期对比。
7. IMEM burst 装载（现在 832 字逐写 ≈ 7k 周期）。
8. LDPC 全迭代（边消息分页 + 层调度，BG1/Z=64 一个码块），与新建 `tools/nr_ldpc.py` 逐位对齐。
9. 自动映射第一步：把 `Program.set` 当 IR，做"规整 kernel 的约束映射器"，**与手写微码逐位等价**；
   再谈 MLIR/CGRA-Flow（`docs/01` §2.3）。

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
