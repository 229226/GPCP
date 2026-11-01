# GPCP — 面向基站的通用通信处理器（RISC-V + CGRA）

> 目标：用 **RISC-V CPU + CGRA 阵列**实现 gNB 侧完整无线协议（LTE + 5G NR 双模），
> 优先复用开源项目。当前阶段为**纯仿真原型**（科研性质）。

## 当前状态：M0 / B2 完成，L1 数据面 + DMA 双缓冲完成，B3 进行中

- **M0**：4×4 CGRA 阵列 RTL（int16 MAC / int32 累加，最近邻 1 周期路由）、全局定序器、
  AXI4-Lite 从口、Verilator 模型、Python 微码汇编器 + 黄金模型比对；
  已通过 `matmul4x4`、`fir16`，以及 L1 数据面 `fft16`、`chnest16`、`mmse16`、`ldpc_cn`
  （scratchpad + `LD/ST` 扩展，见 `docs/07-l1-dataplane.md`）。
- **L1 数据面 + DMA**：scratchpad 扩到 64 banks / 8 KiB 双页，加描述符驱动拷贝引擎
  （RAM↔scratchpad、2D stride/row、页隔离），CVA6 可以「阵列算 page 0、DMA 填 page 1」；
  `make -C sim/soc run-dma` 跑 3 个 slot 的流水并逐位比对黄金模型。
- **完成中断 + PLIC**：CGRA 的阵列/拷贝完成事件经最小 PLIC（源 1）接到 CVA6 机器外部
  中断，CPU 用 `wfi` 等 slot 完成（`make -C sim/soc run-irq`）。
- **突发拷贝 + 描述符环**：拷贝引擎是**完整 AXI4 主口**（INCR 突发、`WLAST/RLAST`、B 响应，
  SoC 侧第 2 个 AXI 从机 + CPU 优先逐拍仲裁）；数据**流式**进出 scratchpad
  （一拍 2 字），突发长度 `DMA_BEATS`（默认 32 拍、4 KiB 裁剪，即写合并）：
  **1024 字线性拷贝 0.70 周期/字**（约 4 倍于逐字版）。描述符放 RAM，引擎自取、
  自写状态字、自推进 head（`make -C sim/soc run-ring`）。
- **L1 接收链**：`chnest → MMSE → 16-QAM max-log 软解调(LLR) → LDPC-CN` 融合成一段
  **127 拍/槽**的微码，四个阶段全部在片内 scratchpad 上传递（**解调没有除法**，
  用 `M(s)=|H|²s²−2Re{y·conj(H)}s` 的 max-log 度量）；输入/输出由**描述符环**驱动、
  阵列与 DMA 在**双页**上重叠：**4 个 slot 2057 周期（514 周期/slot）**
  （`make -C sim/soc run-rxchain`）。
- **FFT（256/1024/4096）**：`radix-4 DIF` 分级 + **融合 radix-16 尾段**（64 点块 kernel），
  旋转因子由 `tools/fft_plan.py` 离线生成成 ROM 镜像；完整级按 64 蝶形（256 点）分块，
  描述符环上每块一个连续窗口（in 组 → 阵列 → out 组 → drain），双页只隔离相邻块。
  **256 / 1024 / 4096 与黄金模型逐位一致**（17.2 / 22.4 / 28.5 周期/点）
  （`make -C sim/soc run-fft`）。
- **NR 同步 + Polar kernel（B4 骨架）**：`pss_corr` / `sss_corr`（127 点复相关，
  16 PE × 8 抽头，**52 拍**）、`polar_enc`（512 bit 蝶形编码，**43 拍**）、
  `polar_node`（min-sum f/g 节点更新，**22 拍**，`g` 不用 32 位乘法）；
  序列按 38.211/38.212 生成（`tools/nr_seq.py`），四个 kernel 与黄金模型**逐位一致**；
  **SoC 级小区搜索已闭环**：PSS 检测（3 候选、2688 周期）与 SSS 336 假设搜索
  （218,894 周期）都检出正确的小区 ID，partial 逐位一致
  （`make -C sim/soc run-sync` / `run-sss`）；**Polar SC 译码**走 CGRA 树遍历
  （N=64/K=32，79,025 周期，64/64 位正确，`make -C sim/soc run-polar`）。
- **slot 时基调度**：CLINT 定时器每 1200 拍打一次 tick，ISR 只记时/重装，
  主循环 `wfi` + 一个 slot 一次服务；**抖动 0、零 overrun、8 slot 逐位一致**
  （`make -C sim/soc run-slot`）。
- **B2**：真实 **CVA6（RV64GC）** 核在 Verilator 中启动裸机程序，通过 AXI4 驱动 CGRA，
  回读结果与黄金模型一致（2059 周期）。
- **B3（进行中）**：SoC 升级为 Linux 可用映射（CLINT / PLIC / 16550 UART / 64 MiB RAM，
  AXI 原子 + LR/SC 独占访问）。**OpenSBI v1.9 完整启动**并跳转 Linux 6.6 内核，
  内核已进入虚拟地址空间执行；受 Verilator 速度（约 70k cycles/s）限制，
  跑到 BusyBox 用户态需要长时间仿真。

```
# M0：CGRA 单元测试
python3 tools/cgra.py test
# [ OK ] matmul4x4 ...
# [ OK ] fir16 ...
# ALL TESTS PASSED

# B2：CVA6 + CGRA 端到端
make -C sim/soc run
# [info] exit_code=0x0000600d cycles=2059
# [ OK ] CVA6 -> CGRA matmul matches the golden model

# L1：CVA6 驱动 DMA + 双缓冲（3 个 slot）
make -C sim/soc run-dma
# [ OK ] DMA + double buffering: 3 slots of 32 H values match the golden model

# L1：中断驱动版（CGRA -> PLIC -> 机器外部中断 + wfi）
make -C sim/soc run-irq
# [ OK ] interrupt-driven DMA + double buffering: 2 slots, 5 completion interrupt(s) ...

# L1：描述符环 + 突发拷贝（真 AXI4 主口）
make -C sim/soc run-ring
# [ OK ] descriptor ring + burst copy: 4 descriptors consumed, ... 0.70 cycles/word

# L1：接收链 chnest -> MMSE -> LDPC-CN（融合微码 + 环 + 双缓冲）
make -C sim/soc run-rxchain
# [ OK ] L1 receive chain ...: 4 slots, ring drove 8 descriptors, results match
# [info] chain: 4 slots in 2057 cycles (514 cycles/slot, 4.05x the 127-cycle array run)

# FFT：256 / 1024 / 4096 点多级分解 + 旋转因子 ROM + 环/双页
make -C sim/soc run-fft
# [ OK ] FFT 256/1024/4096: ... 681 descriptors, 141 array runs

# slot 时基：CLINT 定时器 + 中断驱动的 slot 循环
make -C sim/soc run-slot
# [ OK ] slot scheduler: 8 timer-driven slots, 24 completion interrupts, ...

# B3：构建 Linux 栈并启动（耗时较长）
bash scripts/build_b3.sh
make -C sim/soc linux
```

## 目录结构

```
docs/            架构选型、CGRA ISA、路线图、B2 SoC 说明、环境搭建
rtl/cgra/        CGRA RTL（pkg / pe / array / top / axi_lite）
rtl/soc/         B2 SoC：CVA6 + 地址译码 + RAM + CGRA + 退出寄存器
sim/cgra/        CGRA Verilator 模型 + CPU 总线 BFM（M0）
sim/soc/         SoC Verilator 模型（CVA6 + CGRA，B2）
sw/include/      CGRA 软件 ABI（裸机 / Linux 驱动共用）
sw/examples/     裸机用例（ABI 编译检查）
sw/soc/          B2/B4 裸机程序（start.S / start_irq.S / link.ld / cgra_bm / dma_bm / irq_bm / ring_bm）
tools/           汇编器 + kernel + 自动测试 + SoC 结果比对
scripts/         fetch_cva6.py（GitHub 受限时的拉取脚本）、gen_flist.py、env.sh
third_party/cva6/ CVA6 源码（不纳入 git）
build/           各 kernel 的微码/预装载/样本/dump
```

## 快速开始

依赖（已在本机安装）：`verilator>=5.0`（oss-cad-suite）、`python3`、
`gcc-riscv64-unknown-elf`、`qemu-system-misc`、`clang/llvm`。

```bash
cd /home/zzq/GPCP

python3 tools/cgra.py build           # 只编译 CGRA 模型
python3 tools/cgra.py test            # 跑全部 CGRA kernel 测试
python3 tools/cgra.py test --kernel fir16

make soc                              # 编译 CVA6 + CGRA SoC 模型（约 35 s）
make soc-run                          # 编译裸机程序 + 跑端到端联调
make soc-run-dma                      # 编译裸机程序 + 跑 DMA 双缓冲联调
make soc-run-irq                      # 编译裸机程序 + 跑中断驱动联调
make soc-run-ring                     # 编译裸机程序 + 跑描述符环/突发拷贝联调
make soc-run-rxchain                  # 编译裸机程序 + 跑 L1 接收链（chnest→MMSE→LDPC）
make soc-run-fft                      # 编译裸机程序 + 跑 FFT 256/1024/4096 联调
make soc-run-slot                     # 编译裸机程序 + 跑 slot 时基调度联调
```

手工使用 Verilator/gtkwave 时先 `source scripts/env.sh`
（否则 IDE 注入的 `LD_LIBRARY_PATH` 会让 g++ 选错 libstdc++）。

交叉编译裸机示例（Ubuntu 的 riscv64-unknown-elf 工具链需要 picolibc specs）：

```bash
make sw-check     # 同时做 RV32 与 RV64 编译检查
# 等价于：
riscv64-unknown-elf-gcc --specs=/usr/lib/picolibc/riscv64-unknown-elf/picolibc.specs \
  -march=rv32im -mabi=ilp32 -O2 -Isw/include -c sw/examples/cgra_matmul.c -o /tmp/cgra_matmul.o
```

## 文档

- [docs/01-架构选型.md](docs/01-架构选型.md)：开源项目对比、引擎分工、带宽预算、许可策略
- [docs/02-cgra-isa.md](docs/02-cgra-isa.md)：指令编码、link 语义、寄存器映射、kernel 映射
- [docs/03-路线图.md](docs/03-路线图.md)：M0–M4 里程碑与验收标准
- [docs/04-环境搭建.md](docs/04-环境搭建.md)：工具链版本、安装命令与两个必踩的坑
- [docs/05-soc-b2.md](docs/05-soc-b2.md)：CVA6 + CGRA SoC 的地址映射、AXI 从机实现、复现步骤
- [docs/06-linux-b3.md](docs/06-linux-b3.md)：Linux 版 SoC、OpenSBI 补丁、AXI 原子/LR-SC、镜像布局

## 下一步

1. **B3 收尾**：以更小的内核/更快的仿真跑到 BusyBox 用户态，验证 `/dev/mem` 访问 CGRA；
2. **B4**：srsRAN 参考实现 + free5GC 端到端；
3. 硬件侧：CGRA 加 DMA 描述符环、scratchpad、8×8 扩展。
