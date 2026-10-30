# GPCP — 面向基站的通用通信处理器（RISC-V + CGRA）

> 目标：用 **RISC-V CPU + CGRA 阵列**实现 gNB 侧完整无线协议（LTE + 5G NR 双模），
> 优先复用开源项目。当前阶段为**纯仿真原型**（科研性质）。

## 当前状态：M0 已完成 ✅

- 4×4 CGRA 阵列 RTL（int16 MAC / int32 累加，最近邻 1 周期路由）
- 全局定序器 + 指令存储器 + **AXI4-Lite 从口**（后续直接挂 RISC-V 核）
- Verilator 仿真模型（M0 由 C++ 总线 BFM 扮演 CPU）
- Python 微码汇编器 + kernel 库 + 黄金模型自动比对
- 已通过：`matmul4x4`、`fir16`（滑动相关，PSS 检测的雏形）

```
python3 tools/cgra.py test
# [ OK ] matmul4x4: 6 cycles, 96 microcode words, 128 preload writes, 0 samples
# [ OK ] fir16: 16 cycles, 256 microcode words, 16 preload writes, 16 samples
# ALL TESTS PASSED
```

## 目录结构

```
docs/            架构选型、CGRA ISA、路线图
rtl/cgra/        CGRA RTL（pkg / pe / array / top / axi_lite）
rtl/soc/         M1：Ibex SoC（预留）
sim/cgra/        Verilator 模型 + CPU 总线 BFM
sw/include/      CGRA 软件 ABI（裸机 / Linux 驱动共用）
sw/examples/     裸机用例
tools/cgra.py    汇编器 + kernel + 自动测试入口
build/kernels/   每个 kernel 的微码/预装载/样本/dump
scripts/env.sh   环境（Verilator PATH、清理 LD_LIBRARY_PATH）
```

## 快速开始

依赖（已在本机安装）：`verilator>=5.0`（oss-cad-suite）、`python3`、
`gcc-riscv64-unknown-elf`、`qemu-system-misc`、`clang/llvm`。

```bash
cd /home/zzq/GPCP

python3 tools/cgra.py build           # 只编译 Verilator 模型
python3 tools/cgra.py test            # 跑全部 kernel 测试
python3 tools/cgra.py test --kernel fir16
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

## 下一步（M1）

1. 引入 **Ibex**（Apache-2.0）最小 SoC，CPU 通过 AXI4-Lite 真正驱动 CGRA；
2. 驱动 `/dev/gpcp-cgra`、任务结构、中断完成；
3. Verilator 端到端：UART 输入参数 → 装载 kernel → 运行 → 输出与黄金模型一致；
4. 之后（M2）换 CVA6 + Linux，接入 srsRAN 参考实现与 free5GC。
