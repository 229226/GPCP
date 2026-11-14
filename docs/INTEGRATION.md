# 接入指南：让一个硬件后端跑 wlbench

目标：你的硬件项目只写"怎么执行 + 怎么记账"，协议、判据、按时率全部由
wlbench 提供。全部工作通常不超过一个文件。

## 0. 前置

```bash
pip install -e /path/to/wlbench
python3 -m wlbench.cli build --all        # 产出 build/bundles/<scenario>/
```

## 1. 消费 bundle，不要偷看 golden

后端只需要：

- `ir.json` 的 `nodes[]`：算子、参数、张量长度 → 决定要执行什么；
- `stimulus.bin`：输入数据；
- `manifest.json` 的 `budget.slot_ms`：协议时隙预算（**只有时间，没有周期**）。

`golden.json` 只用于最后的正确性对账，**不得**用来反推结果或跳过计算。

## 2. 执行并记账

在你的硬件/仿真器上跑 `nodes`，记录每个 `id` 的周期数与整链周期。
正确性判定：把算出的比特/锚点与 `golden.json` 比对，全部一致则 `correct=true`。

## 3. 回报 metrics.json

```python
import json, pathlib

def report(bundle_dir, clock_hz, per_stage, correct, backend="my-accel"):
    ir = json.loads((pathlib.Path(bundle_dir) / "ir.json").read_text())
    ops = {n["id"]: n["op"] for n in ir["nodes"]}
    stages = [{"id": i, "op": ops.get(i, "?"), "cycles": c, "correct": correct}
              for i, c in sorted(per_stage.items())]
    metrics = {
        "backend": backend,
        "clock_hz": clock_hz,                 # 你的运行频率，不是仿真时钟
        "clock_source": "measured",           # 或 estimated-pre-layout
        "total_cycles": sum(per_stage.values()),
        "correct": correct,
        "stages": stages,
    }
    (pathlib.Path(bundle_dir) / "metrics.json").write_text(
        json.dumps(metrics, indent=2))
    return metrics
```

## 4. 让 wlbench 判定

```python
from wlbench import metrics as M

rep = M.evaluate(bundle_dir, metrics)
print(rep["primary_on_time"], rep["wall_ms"], rep["timeline"])
```

或命令行：

```bash
python3 -m wlbench.cli evaluate --bundle build/bundles/nr_mimo \
    --metrics path/to/metrics.json
```

## 5. 跑一致性

```bash
python3 -m wlbench.cli conformance
python3 examples/reference_backend.py --scenario nr_mimo   # 参考实现
```

## 检查清单

- [ ] `metrics` 只含 `clock_hz / total_cycles / correct`（+ 可选 `stages`、`clock_source`）
- [ ] `clock_hz` 是**该后端运行频率**，不是 Verilator/仿真器时钟
- [ ] 没有把 `budget_cycles` 写进 workload 或场景文件
- [ ] 没有在 metrics 键里出现 `spad / imem / array_runs / microcode` 等词
- [ ] `correct` 来自与 `golden.json` 的逐值对账，不是"跑完就 true"
- [ ] 同一 bundle 换一个 `clock_hz`，`on_time` 判定应随之变化（不是写死的）

## 常见错误

| 症状 | 原因 | 修正 |
|---|---|---|
| 按时率恒为 100% 或 0% | 用了仿真时钟或无 `clock_hz` | 换后端真实频率，并标 `clock_source` |
| `evaluate` 抛 `architecture-specific metric key` | metrics 里带了架构字段 | 只回报通用量，架构细节留在自己的日志里 |
| 换后端后预算不对 | workload 里存了周期预算 | 只存 `slot_ms`，周期现算 |
| 换了参数但数据没变 | 扫描参数未贯穿 golden | 用 `sweep` 的 `STALE_GOLDEN` 检测，补参考模型 |
| `params_hash` 不一致 | 旧二进制跑新激励 | 重新构建，或在运行器里校验哈希 |

## 参考实现

`examples/reference_backend.py` 是一个 60 行的纯 Python 虚拟后端，演示最小接入：
它读 bundle、按节点数与比特数估算周期、产出 metrics、调用 `evaluate`。
注意它的周期是**虚拟的**，只用于演示接口，不是性能数据。
