# 项目、里程碑与开发流程

## 1. 项目目标

用 RISC-V CPU（CVA6）+ 4×4 CGRA 实现 gNB 侧无线协议栈原型（LTE + 5G NR），
优先复用开源组件，当前为纯仿真原型（科研性质）。验收标准是**与黄金模型逐值
一致**，不是「流程跑通」。

## 2. 里程碑状态

| 里程碑 | 状态 | 证据 |
|---|---|---|
| M0：4×4 CGRA RTL + 汇编器 + 黄金模型 | 完成 | `tools/cgra.py test` |
| B2：CVA6 + CGRA 裸机联调 | 完成 | `make -C sim/soc run` |
| L1 数据面：spad 双页 + DMA + 描述符环 + 中断 | 完成 | `run-dma`、`run-irq`、`run-ring`、`run-rxchain` |
| FFT 256/1024/4096 + IFFT/CP | 完成 | `run-fft`、`run-ifft` |
| NR 同步 + SSB 采样级链路 | 完成 | `run-sync`、`run-sss`、`run-ssb`、`run-ssbfft` |
| PDCCH / DCI 盲检 | 完成 | `run-pdcch`、`run-pdcch-loop` |
| PDSCH（QPSK/16QAM/64QAM/HARQ） | 完成 | `run-pdsch-*`、`run-proto-nr_pdsch*` |
| PUSCH / PUCCH / PRACH / RACH | 完成 | `run-pusch*`、`run-pucch`、`run-prach`、`run-rach` |
| SIB1（Type0-PDCCH → PDSCH） | 完成 | `run-sib1` |
| PPA 综合（Nangate45） | 完成 | `make -C syn sta` |
| B3：Linux（OpenSBI + 6.6 + BusyBox） | 进行中 | 内核到 `late_init`，未到用户态 |
| M3：8×8 扩展 + 自动映射 | 部分 | 8×8 ADL 与规则映射可用；自动映射不可靠 |

## 3. 计划视图

- **协议能力线**：P1 SIB1 → P2 随机接入 → P3 RRC/控制面 → P4 数据面（MCS/QAM）
  已铺到 P4；P5 上行精化、P6 L2、P7 核心网、P8 移动性仍在后面。
- **gNB 视角**：G1 发射链 → G2 自环 → G3 调度 → G4 L2 → G5 多用户
  → G6 核心网 → G7 移动性。当前重点是 G1/G2 的硬件闭环与性能。
- **性能线**：先保证逐值正确，再按 `decode → ofdm/transform → encode`
  的顺序优化（见 [../performance/README.md](../performance/README.md)）。

## 4. 目录地图

| 路径 | 内容 |
|---|---|
| `rtl/cgra/` | CGRA RTL：PE、阵列、spad、定序器、拷贝引擎、描述符环 |
| `rtl/soc/` | SoC：CVA6 + RAM + CGRA + PLIC + CLINT + UART |
| `sim/cgra/` | 纯 CGRA 台架与 ISA/旋转/描述符回归 |
| `sim/soc/` | SoC 台架与全部 `run-*` 门禁 |
| `sw/include/` | 裸机 ABI、运行时、描述符/环/布局头文件 |
| `sw/soc/` | 裸机基准、阶段实现（`*_stages.inc`）、协议运行器 |
| `cgra/kernels/` | kernel YAML（约 40 个） |
| `cgra/tools/` | 编译器/映射器/布局契约生成器 |
| `tools/` | 黄金模型、检查器、`verify.sh`、`devlog.py` |
| `proto/` | 协议描述层：HLL、lower、emit、models、scenarios |
| `syn/` | 综合/STA 脚本与报告 |
| `docs/` | 本套开发文档 |

## 5. 日常流程

```bash
# 构建
make -C sim/soc                 # Verilator 模型（CVA6 较大，首次较慢）
make -C sw/soc -j8 all          # 全部裸机程序

# 开发中的最小回归
make -C sim/soc run-<name>      # 单条门禁
make proto-check                # 协议描述层静态检查
make yaml-check                 # kernel/描述符契约生成物同步
make handprog-check             # 禁手写微码

# 提交前
bash tools/verify.sh smart      # 日常：按输入指纹跳过已通过项
bash tools/verify.sh slow --force   # 阶段末/交付证据

# 记账
python3 tools/devlog.py add --domain <domain> --kind <kind> --minutes N \
    --task "..." --commit <hash> --confidence measured --evidence "..."
make devcheck
```

规则：

- 每个功能/修复提交都要有工时记录；`make devcheck` 0 error。
- 新门禁必须进 `tools/verify.sh`。
- 所有 Verilator 调用前清 `LD_LIBRARY_PATH`（Makefile 已用 `env -u`）。
- 提交信息写「为什么」和验收数字，不写流水账。

## 6. 已知限制与风险

- `mmse_scalable`（N×N MMSE，layers/rx ∈ {2,4,8}）只有 lowering，没有微码，
  跑不出数。
- `lte_pdsch` 在 1 ms 时隙预算下仍超时，是真实性能结论。
- 早期 MMSE 描述符环方案放弃：引擎会静默漏做一段连续描述符，
  `RING_DONE` 仍报完成；当前改用 CPU 直写，该 RTL 行为仍是待查隐患。
- `run-pdcch` 在 2026-11-15 曾稳定错 5 位，2026-11-16 复核已 32/32 通过；
  根因未最终定位，保留为潜伏风险。
- B3 Linux 受 Verilator 速度限制，尚未到用户态。

## 7. 相关文档

- 架构与平台：[../architecture/README.md](../architecture/README.md)
- 验证与门禁：[../verification/README.md](../verification/README.md)
- 性能基线：[../performance/README.md](../performance/README.md)
