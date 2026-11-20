# backend/gpcp — GPCP 的 wlbench 后端

把一个 wlbench workload bundle（`ir.json` / `stimulus.bin` / `golden.json`）
落到 GPCP 的硬件上，跑出周期，并把 wlbench 要求的 `metrics.json` 交回去。

**这个目录是 GPCP 唯一需要为 wlbench 写的部分**：所有硬件知识（阵列几何、
落点选择、微码实现、频率）都在这里；wlbench 侧只认 `metrics.json`。

## 职责

| 步骤 | 输入 | 输出 |
|---|---|---|
| `verify-bundle` | bundle 的三个产物 | 与 `build/proto/<场景>/` 现有产物逐字节比对 |
| `plan_for`（lowering） | **bundle 的 `ir.json`** + `cgra/target.yaml` | 落点计划（`stages[]`：where / impl / array_runs） |
| `build_plan_h` | 上面的计划 + bundle 的 `stimulus.bin` | `plan.h`（运行器唯一的编译期输入） |
| `run` | bundle 目录 | 编 `proto_runner`、跑 Verilator、dump 落到 `sim/soc/build/` |
| `collect` | dump 文件 | `metrics.json`：`clock_hz` / `total_cycles` / `stages[]` / `correct` |
| `evaluate` | bundle + metrics | wlbench 的按时率 / 墙钟时间 / 差距表 |

关键点：`plan_for` 的输入只有 **bundle 的 IR** 和 GPCP 自己的 `cgra/target.yaml`，
**不再读 `build/proto/<场景>/plan-base.json`**，也不读 `proto/scenarios/*.py`。
所以任何 wlbench 新造的 workload（例如 `mmse_scalable`）都能被 lowering，
不依赖 GPCP 是否跑过那个场景。

## 执行一次

```bash
# CGRA 路径
python3 backend/gpcp/run.py --bundle /home/zzq/wlbench/build/bundles/nr_mimo \
        --backend gpcp-cgra --out backend/gpcp/build/metrics.json

# 同一 bundle 的 CVA6 全标量对照
python3 backend/gpcp/run.py --bundle /home/zzq/wlbench/build/bundles/nr_mimo \
        --backend cva6-scalar --out backend/gpcp/build/metrics_cpu.json
```

`run` 会在 `backend/gpcp/build/run/<场景>/` 下摆 `plan.h` + `stimulus.bin`，
用 `make -C sw/soc proto PROTO_DIR=<那个目录>` 编，再直接调
`sim/soc/build/Vgpcp_soc` 跑。**没有 `<场景>_ops.inc` 的 bundle 会以退出码 3
明确报"算子还没有硬件实现"**，而不是崩在某个 FileNotFoundError 上。

## 两种时钟，别混

- **仿真时钟 100 MHz**：`cgra/target.yaml` 的 `clock_hz`，只用于 `plan.h` 的
  `PROTO_CLOCK_HZ` 和预算周期，保证生成物与既有产物逐字节一致。
- **后端运行频率**（`clocks.json`）：CGRA 1,053 MHz / CVA6 264 MHz，来自
  `docs/performance/README.md` 的 pre-layout 综合结论，标注 `estimated-pre-layout`。
  `metrics.json` 用这个，时间判定才有物理意义。

Verilator dump 里的 `cycles` 是周期数，与频率相乘才得到时间；仿真时钟
只是台架设置，不能拿来算协议实时性。

## 命令

```bash
python3 backend/gpcp/adapter.py regress                     # 冻结回归
python3 backend/gpcp/adapter.py collect   --scenario nr_mimo --backend gpcp-cgra
python3 backend/gpcp/adapter.py evaluate  --scenario nr_mimo --backend gpcp-cgra
python3 backend/gpcp/run.py     --bundle <bundle> [--backend ...]
python3 backend/gpcp/adapter.py capabilities [--json] [--strict]
make -C backend/gpcp regress
make -C backend/gpcp ci        # regress + capabilities + 真跑一个 bundle + evaluate
```

## 回归口径

`regress` 现在做 **86 项冻结检查**：

- 17 场景 × 3 个 bundle 产物字节一致（51）；
- 17 个 `plan.h` 从 **bundle IR** 重新 lowering 后字节一致（17）；
- 17 个场景的 lowering 结果与旧 `plan-base.json` 的 `stages[]` **逐字段相同**（17）——
  这条保证 W1 的搬迁没有改变任何落点决定；
- 账本 `compare_all.csv` 的冻结 sha 未变（1）。

`compare_all.csv` 由 `tools/gen/gen_proto_ledger.py` 从当前 dump 生成，**backend 只读不写**。

## 已知限制

- **算子覆盖**：执行仍需要 `sw/soc/ops/<场景>_ops.inc`——把算子序列翻译成 MMIO/
  DMA/微码调用的 C 实现。wlbench 的 17 个冻结场景都有；`mmse_scalable`
  这类新 workload 目前只能 lowering，跑不了。
  `adapter.py capabilities` 会逐个 bundle 报出能否执行与原因（当前 17/18），
  `--strict` 在有不可执行项时返回 1。
- **多规模 MMSE**：`mmse_full` 现在按 2 层写死；`layers=4/8` 的 sweep 点
  需要参数化微码，属 W4。
- `plan.h` 的注释仍写着 `proto/emit.py`，是**刻意保留**的：它保证生成物与
  历史产物逐字节一致；改注释需要重跑 17 个场景并重刷账本。
