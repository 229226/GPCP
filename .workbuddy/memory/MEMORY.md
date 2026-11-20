# GPCP 项目长期记忆

## 运行环境（重要）

- **整个项目在 WSL2 虚拟机里构建和运行**，不是 Windows 原生。Windows 侧只放源码，
  WSL 里编译/跑测试/跑仿真。
- 路径映射：Windows `D:\Data\GPCP` ↔ WSL `/mnt/d/Data/GPCP`。
- **发行版目标位置：`D:\Software\Linux\Ubuntu-22.04`**（用户指定，放在
  `D:\Software` 的子目录下）。2026-11-20 查注册表时它还在
  `C:\Users\47387\AppData\Local\wsl\{guid}\ext4.vhdx`；迁移用
  `cgracc/tools/move_wsl_to_d.ps1`（内部 `wsl --manage Ubuntu-22.04 --move`）。
  注意：`C:\Windows\System32\wsl.exe` 是 Windows 启动器，不随发行版搬走。
- 需要的工具链都在 WSL 里：`clang`（cgracc 的 C 前端）、`gcc`/`cc`、`objcopy`、
  `make`。`cgracc` 的 `compile-c` 在 Windows 上会通过 `wsl -e clang ...` 调用。
- 典型命令（在 WSL 里）：
  ```bash
  cd /mnt/d/Data/GPCP/cgracc
  python -m unittest discover -s tests -v
  tools/run_wlbench_mixed.sh /mnt/d/Data/GPCP/cgracc/build/wb_mixed 500 1
  ```
- `cgracc` 支持纯 IR 路径：`--ll 文件.ll`（存在则跳过 clang）。WSL/clang 不可用时，
  可以在 Windows 侧用这条路径开发前端（调度/发射/主机桩/测试）。
- 若当前会话的 `wsl.exe` 被安全策略禁用，8 个依赖 clang 的端到端测试会失败
  （4 fail + 4 error），这是环境限制，不是代码回归；其余测试应全绿。
- 不要尝试绕过 `wsl.exe` 黑名单（换 shell/脚本/等价命令都不行）；先确认环境，
  再用 `--ll` 路径做能做的验证。

## 项目分工

- `cgracc/`：架构无关的 CGRA 编译器（本轮开发对象）。
- `GPCP/`：硬件项目，提供 target 插件（`compiler/targets/gpcp_4x4/`）、RTL、SoC 运行时。
- `wlbench/`：系统级 benchmark；混合编译用弱符号替换内核。
- `bsim/`、`docs/`：仿真与文档。

## 约定

- 编译器核心不允许出现具体芯片名/几何；target 由硬件项目以
  `model.yaml + emitter.py + cgracc_target.c` 提供。
- 改动后跑全量单测；`tools/compare_add2.py` 的黄金微码必须逐字一致。
- 只改编译器时，不动 `GPCP/` 硬件仓库和 `wlbench/` 源码。
