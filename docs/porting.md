# 接入一个新架构（porting checklist）

目标：**只加 `arch/<name>/` 下的文件，编译器核心 0 行改动。**

## 1. 目录

```
arch/<name>/
  model.yaml      # 五段契约
  emitter.py      # 可选：class XxxEmitter(cgracc.emit.base.Emitter)
```

**没有 `emitter.py` 时会自动回退到通用 `ListingEmitter`**——同族架构（算子集、
时序模型相同）因此只需要 `model.yaml`，这就是 `arch/gpcp_8x8/` 演示的路径。
`__init__.py` 也不是必需的（Python 命名空间包）。

## 2. model.yaml 必须写清的五段

| 段 | 字段 | 用途 | 缺了会怎样 |
|---|---|---|---|
| `time_model` | `kind`, `max_run_cycles`, `run_overhead_cycles` | 编译器决定一次运行的长度上限与启动开销 | 生成超长调度；代价模型少算固定开销 |
| `compute` | `units`, `issue_per_unit_per_cycle`, `registers`, `acc`, `link`, `private_registers`, `ops{inputs,latency,writes}` | 绑定与资源约束 | 生成不存在的算子 / 资源冲突 / 跨 PE 的错误依赖 |
| `store` | `banks`, `words_per_bank`, `pages`, `read_ports_per_bank`, `write_ports_per_bank`, `read_timing`, `write_timing`, `broadcast` | 检查 bank 冲突与端口数 | 生成"同拍两写同一 bank"的调度 |
| `move` | `beat_bits`, `max_burst_beats`, `overlap_compute`, `targets`, `sync` | 搬运调度的边界（当前只建模，未参与求解） | 无法判断 run 间能否重叠 |

字段义务：**要么被生成器查询，要么被检查器验证**。做不到的字段先不要写。

## 3. emitter.py

```python
from cgracc.emit.base import Emitter

class MyEmitter(Emitter):
    def emit(self, schedule, dfg, model) -> str:
        ...
```

发射器只做"Schedule -> 该机器的产物"（微码 / 位流 / 汇编）。
它不允许改变调度，也不允许读 DFG 以外的隐藏状态。

## 4. 自检

```bash
python3 -m cgracc info    --arch <name>
python3 -m cgracc schedule --arch <name> --dfg examples/dot4.json -o build/s.json
python3 -m cgracc check    --arch <name> --dfg examples/dot4.json --schedule build/s.json
python3 -m cgracc emit     --arch <name> --dfg examples/dot4.json --schedule build/s.json
```

## 5. 验收标准

```bash
python3 -m cgracc conformance --arch <name>
```

- 六个正向用例（`vec_add` / `dot4` / `fft8` / `matmul4` / `bank_split` / `loop_dot`）**全绿**；
- 四个负向用例必须被拒绝：
  `neg_bank`（同拍同 bank 两次读 → R6）、`neg_run`（超长 run → R3）、
  `neg_reg`（`r<registers>` 越界 → R5）、`neg_page`（array 与 dma 同页 → R9）；
- 编译器核心无改动（`git diff --stat` 只应出现 `arch/<name>/`）。

`bank_split` 是"正向负例"：addr 0 与 addr `words_per_bank` 同拍必须**不报** R6——
用来卡住 bank 宽度是否真的来自模型。依赖倒置（R7）由 `tests/test_smoke.py` 覆盖。

## 6. 什么时候需要写调度器插件

| 架构特征 | 动作 |
|---|---|
| 静态同步拍、mesh、集中存储 | 用现有 `list`，只给 model |
| 需要求最小 II / 有回边 | 用现有 `modulo`（DFG 里写 `back_edges` + `trip_count`） |
| 异构单元、绑定有类型约束 | 新增 `sched/list_typed.py` |
| 需要精确最优（小规模） | 新增 `sched/cpsat.py` |

调度器只依赖 `Scheduler` 接口；不许在 emitter 或 checker 里出现架构名。
