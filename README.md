# GPCP — RISC-V + CGRA 通信处理器与编译工具链

本仓库汇总了 GPCP 科研原型的**编译器、硬件 RTL/SoC、系统级 benchmark 与技术文档**。
目标是探索用 RISC-V CPU + 静态调度 CGRA 阵列承载无线通信协议栈的数据面，并让
编译器自动完成 C 内核到微码的映射。

> 当前是**纯仿真原型**，不是流片级设计。主要开发与验证环境是 **WSL2（Ubuntu）**：
> Windows 侧只放源码，编译、单测、仿真都在 WSL 里跑。
> 路径映射：`D:\Data\GPCP` ↔ `/mnt/d/Data/GPCP`。

## 仓库结构

| 路径 | 类型 | 说明 |
|---|---|---|
| [`cgracc/`](cgracc/) | submodule | 架构无关的 CGRA 编译器：C → LLVM IR → DFG → 路由/调度 → 微码 + 主机桩 |
| [`GPCP/`](GPCP/) | submodule | 硬件项目：4×4 CGRA RTL、CVA6 SoC、Verilator 模型、target 插件与 SoC 运行时 |
| [`.archive/wlbench_20261119/`](.archive/wlbench_20261119/) | submodule | 旧版 wlbench 快照，供回归对照 |
| [`wlbench/`](wlbench/) | 目录 | 当前系统级 benchmark：MAC 调度面 + 真实物理层链路 + 结果字/评分 |
| [`docs/`](docs/) | 目录 | 跨项目技术评估文档 |
| `.workbuddy/` | 目录 | 项目工作记录，与构建无关 |

三个 submodule 的 URL 目前都指向本仓库（它们的历史提交对象已在其中）。
克隆时务必带上子模块：

```bash
git clone --recurse-submodules git@github.com:229226/GPCP.git
```

如果已经克隆过但没拉子模块：

```bash
git submodule update --init --recursive
```

## 组件关系

```
wlbench/  (硬件无关的 C benchmark，产出协议结果字)
    │  C 内核函数
    ▼
cgracc/   (clang IR → 定点 DFG → 路由/调度/切段 → 微码 + host stubs)
    │  target 插件：model.yaml + emitter.py + cgracc_target.c
    ▼
GPCP/     (CVA6 + 4×4 CGRA RTL + DMA/描述符环 + Verilator SoC)
```

编译器核心不含任何具体芯片信息；阵列规模、寄存器数、算子、spad 几何、互连和
运行时都由硬件项目以 target 插件形式提供。

## 环境依赖

在 WSL2 Ubuntu 中需要：

- `clang` / `llvm`（`cgracc compile-c` 的前端；无 clang 时可用 `--ll` 直接吃已有 IR）
- `python3 >= 3.10`、`PyYAML >= 6`
- `gcc`、`make`、`objcopy`
- `gcc-riscv64-unknown-elf`（GPCP 裸机程序交叉编译）
- Verilator（建议使用仓库中 `GPCP/third_party/eda/oss-cad-suite` 的版本）

## 快速验证

以下都在 WSL 里执行。

```bash
# 1) 编译器单测
cd /mnt/d/Data/GPCP/cgracc
python -m pip install -e .
python -m unittest discover -s tests -v
python tools/compare_add2.py

# 2) wlbench 主机版（纯 CPU golden）
cd /mnt/d/Data/GPCP/wlbench
make
./build/wlbench_host --scenario mixed --slots 500 --seed 1
make phy          # 物理层数值自检
make test         # testbench + 物理层自检

# 3) CGRA RTL kernel 测试
cd /mnt/d/Data/GPCP/GPCP
python3 tools/cgra.py test

# 4) CVA6 + CGRA SoC 端到端
make -C sim/soc run
```

更完整的 SoC 场景（`run-dma`、`run-irq`、`run-ring`、`run-rxchain`、`run-fft`、
`run-slot` 等）见 [`GPCP/README.md`](GPCP/README.md)。

## 典型闭环：把 wlbench 内核下沉到 CGRA

在 `cgracc/` 目录下扫描整个 wlbench C 文件，自动划分主机函数与内核，生成微码
和弱符号替换用的构建片段：

```bash
cd /mnt/d/Data/GPCP/cgracc

python -m cgracc compile-c --auto --arch gpcp_4x4 \
    --target-path ../GPCP/compiler/targets \
    -I ../wlbench/include -I ../wlbench/src \
    --src ../wlbench/src/wlbench.c \
    --emit-build build/wb_mixed

tools/run_wlbench_mixed.sh \
    /mnt/d/Data/GPCP/cgracc/build/wb_mixed 500 1
```

后者在 WSL 中编译混合镜像、把原内核降为弱符号、运行并与纯 CPU 版本逐字对账。
细节见 [`cgracc/docs/hybrid.md`](cgracc/docs/hybrid.md)。

## 开发约定

- **改动在哪个组件，就在哪个 submodule 里提交**。根仓库只记录每个子模块的 commit 指针。
- 子模块提交后，回到根目录更新指针：

  ```bash
  cd /mnt/d/Data/GPCP
  git add cgracc        # 或 GPCP / .archive/wlbench_20261119
  git commit -m "Update cgracc submodule"
  git push
  ```

- 不要在根仓库里直接把子模块目录当普通文件改；根仓库的 `git status` 里子模块显示为
  小写 `m` 表示其中有未提交内容，大写 `M` 或新 commit 表示指针需要更新。
- 根仓库的 `.gitignore` 覆盖 Python/构建/仿真/日志等通用垃圾；各 submodule 自带更细的规则。
- 编译器核心不允许出现具体芯片名或几何。新硬件只需提供
  `model.yaml + emitter.py + cgracc_target.c`。
- 改动后跑全量单测；`tools/compare_add2.py` 的黄金微码必须逐字一致。

## 文档入口

| 主题 | 位置 |
|---|---|
| 编译器安装、CLI、能力边界 | [`cgracc/README.md`](cgracc/README.md) |
| target 插件契约 | `cgracc/docs/targets.md` |
| 编译器架构与降低规则 | `cgracc/docs/architecture.md` |
| 混合编译与离线验收 | `cgracc/docs/hybrid.md` |
| 硬件现状、硬规矩、验收命令 | [`GPCP/AGENTS.md`](GPCP/AGENTS.md) |
| 硬件文档总入口 | [`GPCP/docs/README.md`](GPCP/docs/README.md) |
| benchmark 契约与评分 | [`wlbench/README.md`](wlbench/README.md) |
| 编译工具能力评估 | [`docs/CGRA编译工具评估.md`](docs/CGRA编译工具评估.md) |
