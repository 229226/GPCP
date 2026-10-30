# GPCP — 面向基站的通用通信处理器（RISC-V + CGRA）

> 目标：用 **RISC-V CPU + CGRA 阵列**实现 gNB 侧完整无线协议（LTE + 5G NR 双模），
> 优先复用开源项目。当前阶段为**纯仿真原型**（科研性质）。

## 当前状态：M0 / B2 完成，B3 进行中

- **M0**：4×4 CGRA 阵列 RTL（int16 MAC / int32 累加，最近邻 1 周期路由）、全局定序器、
  AXI4-Lite 从口、Verilator 模型、Python 微码汇编器 + 黄金模型比对；
  已通过 `matmul4x4`、`fir16`。
- **B2**：真实 **CVA6（RV64GC）** 核在 Verilator 中启动裸机程序，通过 AXI4 驱动 CGRA，
  回读结果与黄金模型一致（2058 周期）。
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
# [info] exit_code=0x0000600d cycles=2058
# [ OK ] CVA6 -> CGRA matmul matches the golden model

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
sw/soc/          B2 裸机程序（start.S / link.ld / cgra_bm.c）
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
