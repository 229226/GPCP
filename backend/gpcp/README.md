# backend/gpcp — GPCP 的 wlbench 适配器

这是 GPCP 作为 wlbench 后端**唯一需要写的硬件相关层**。它不描述协议，也不改动
`proto/`、`sw/`、`sim/` 或任何现有 Makefile。

## 职责

| 步骤 | 输入 | 输出 |
|---|---|---|
| `verify-bundle` | wlbench bundle (`ir.json` / `stimulus.bin` / `golden.json`) | 与现有 `build/proto/<s>/` 产物逐字节比对 |
| `build_plan_h`（regress 内） | bundle 的 `ir.json` + GPCP 的 `plan-base.json`（目标几何/仿真时钟） | 重新生成 `plan.h`，与现有文件逐字节比对 |
| `collect` | `sim/soc/build/proto_<s>[_cpu]_dump.txt` | `metrics.json`：`clock_hz` / `cycles` / `correct` |
| `evaluate` | bundle + metrics | wlbench 的按时率 / 墙钟时间 / 差距表 |

## 两种时钟，别混

- **仿真时钟 100 MHz**：来自 `plan-base.json` 的 `target.clock_hz`，只用于
  `plan.h` 的 `PROTO_CLOCK_HZ` 和预算周期，保证生成物与既有产物一致。
- **后端运行频率**（`clocks.json`）：CGRA 1,053 MHz / CVA6 264 MHz，来自
  `docs/08 §9.4` 的 pre-layout 综合结论，标注 `estimated-pre-layout`。
  `metrics.json` 用这个，时间判定才有物理意义。

Verilator dump 里的 `cycles` 是周期数，与频率相乘才得到时间；仿真时钟
只是台架设置，不能拿来算协议实时性。

## 命令

```bash
python3 backend/gpcp/adapter.py regress
python3 backend/gpcp/adapter.py collect   --scenario nr_mimo --backend gpcp-cgra \
    --out backend/gpcp/build/metrics_cgra.json
python3 backend/gpcp/adapter.py evaluate  --scenario nr_mimo --backend gpcp-cgra \
    --out backend/gpcp/build/eval_cgra.json
make -C backend/gpcp regress
```

## 回归口径

`regress` 检查：17 场景 × 3 个 bundle 产物字节一致、17 个 `plan.h` 从 bundle
重新生成后字节一致、逐 stage 周期与 `compare_all.csv` 的 131 行逐值一致、
以及账本文件的冻结 sha 未变。**compare_all.csv 只读不写**，docs/22 / docs/24
的历史数字不受影响。

## 已知：冻结账本与当前 CGRA dump 不同步

`compare_all.csv` 冻结于 11-12 12:30，而多数 CGRA dump（11-14 11:39 起）
以及个别 CPU dump（如 `nr_pusch_tp` 11-13 16:28）是之后重跑的——GPCP 工作区
有未提交的 `proto/lower.py`、`sw/soc/*_stages.inc` 等改动，周期已变。
例如 `nr_mimo`：账本 408,874，当前 dump 403,722。stage 编号与数量完全对齐，
差异纯粹来自重跑。

`regress` 因此把结果分两类：

- **冻结检查（硬失败，69 项）**：bundle 字节（51）、plan.h 字节（17）、账本 sha（1）；
- **实时偏差（只报告）**：CGRA / CPU 两个 dump 与账本的逐 stage 周期差异条目数及示例。

`collect` 始终回报**当前 dump** 的周期，这才是"当前硬件"的真实 metrics。
若要让账本重新对上，需在 GPCP 侧重跑 `run-proto-*` 并重生成
`compare_all.csv`——那会改变 `docs/22` / `docs/24` 的历史数字，P4 不做。

## 前提

wlbench 需已 `pip install -e /home/zzq/wlbench`（`import wlbench` 可用）。
`clocks.json` 里的频率若因重新综合变化，只需改这一个文件。
