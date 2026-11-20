# cgracc — 架构无关的 CGRA 编译器骨架

目标：**新增一个架构，只提供 `arch/<name>/model.yaml` + `emitter.py`，编译器核心 0 行改动。**

当前状态是**骨架 + 一条可跑的端到端路径**（列表/模调度 + 合法性检查器 + 真实 IMEM 编码），
并且已经在 GPCP 4x4 的真实 RTL 上闭环：`make -C sim/soc run-cgracc-dot` 把编译器产物
逐字写进 IMEM，3 拍后 spad[0,1] = 75。**add2 与规则映射器的产物 64/64 字逐字一致**
（`python3 tools/compare_add2.py`）。

落点搜索已覆盖三个维度：`list/{拓扑序, 关键路径序}/{首次适配, 分量装箱}`、
`modulo/II=k`，以及**多 run 切分**——循环展开超过 `max_run_cycles` 时自动求
`trips_per_run`（必须整除 `trip_count`）并给出 `runs` 次数，代价按 run 启动开销累加。

## 快速开始

```bash
cd d:/Data/GPCP/cgracc

# 1) 看架构摘要
python3 -m cgracc info --arch gpcp_4x4

# 2) DFG -> 调度（生成 build/schedule.json）
python3 -m cgracc schedule --arch gpcp_4x4 --dfg examples/dot4.json -o build/schedule.json

# 3) 合法性检查（空输出 = 通过）
python3 -m cgracc check --arch gpcp_4x4 --dfg examples/dot4.json --schedule build/schedule.json

# 4) 发射：真实 IMEM 编码（GPCP 的 32-bit 指令字，输出 C 头文件）
python3 -m cgracc schedule --arch gpcp_4x4 --dfg examples/imem_dot.json -o build/imem_dot.json
python3 -m cgracc emit --arch gpcp_4x4 --dfg examples/imem_dot.json \
    --schedule build/imem_dot.json -o build/imem_dot_prog.h

# 5) 循环 kernel：modulo 调度（求启动间隔 II）
python3 -m cgracc schedule --arch gpcp_4x4 --scheduler modulo \
    --dfg examples/loop_dot.json -o build/loop.json
python3 -m cgracc check --arch gpcp_4x4 --dfg examples/loop_dot.json --schedule build/loop.json

# 6) 落点搜索：list/{topo,height}/{decl,pack} × modulo/II -> 检查 -> 代价 -> 选最优
python3 -m cgracc search --arch gpcp_4x4 --dfg examples/loop_dot.json -o build/best.json

# 7) 接入验收：6 正向 + 4 负向（新架构必须全绿）
python3 -m cgracc conformance --arch gpcp_4x4
python3 -m cgracc conformance --arch gpcp_8x8

# 8) 自测
python3 -m unittest discover -s tests -v
```

## 目录

```
cgracc/
  cgracc/
    ir/model.py         # 机器模型：语义 / 资源 / 时序 / 约束（加载 + 校验）
    ir/dfg.py           # DFG：节点 + 依赖
    ir/schedule.py      # Schedule：每个操作 (unit, cycle, dst, addr)
    sched/base.py       # Scheduler 接口
    sched/list_sched.py # 参考实现：列表调度（DAG，不追求 QoR）
    sched/modulo.py     # modulo 调度（求 II：MII = max(ResMII, RecMII)）
    cost.py             # 代价模型：总周期 = 计算周期 + run 启动开销
    search.py           # 落点搜索：候选 -> 检查 -> 代价 -> 最优
    verify/legality.py  # 合法性检查器（规则全部来自 model）
    emit/base.py        # Emitter 接口（通用 listing 在 emit/listing.py）
    conformance.py      # 接入验收：6 正向 + 4 负向
    cli.py              # info / conformance / schedule / search / check / emit
  arch/
    gpcp_4x4/           # 架构 0：GPCP 4x4（model.yaml + 真实 IMEM 编码的 emitter）
    gpcp_8x8/           # 架构 1：只给 model.yaml（接口验证，无 RTL）
  examples/dot4.json    # 最小 DFG（抽象操作数）
  examples/wide20.json  # 20 个独立算子：看阵列规模对调度的影响
  examples/loop_dot.json # 带自回边的循环（modulo 调度）
  examples/imem_dot.json # 可编码成真实 IMEM 的 4 算子 DFG
  examples/imem_loop.json # 可编码的循环（4 次迭代 → 展开 5 拍）
  docs/porting.md       # 接入新架构的 checklist
  tests/test_smoke.py   # 端到端 + 负例（必须被拒绝）
  tests/test_mutation.py # 检查器失效时，4 个负向用例必须全部失败
```

## 边界

- **编译器拥有**：IR、调度框架、合法性检查、发射框架、conformance。
- **架构方拥有**：`model.yaml`（五段契约）+ `emitter.py`（Schedule → 该机器的微码/位流）。
- 编译器核心**不允许出现任何架构名**（后续加 CI grep 检查）。

## 搜索维度与多 run 切分

| 维度 | 取值 | 由模型驱动 |
|---|---|---|
| 调度器 | `list`（DAG）/ `modulo`（有回边或 `trip_count > 1`） | — |
| 排序 | `topo` / `height` | — |
| 分量绑定 | `decl`（首次适配）/ `pack`（按分量大小 LPT 装箱） | `compute.private_registers` |
| 启动间隔 | `II ∈ [MII, MII + 4)` | `ops[].latency`、单元数 |
| run 切分 | `trips_per_run × runs = trip_count` | `time_model.max_run_cycles` |

`plan_trips()` 只在"展开后的跨度装不下一个 run"时启用：取不超过上限的最大
`trips_per_run`，且必须是 `trip_count` 的因子——否则最后一次 run 会多跑几次迭代，
对累加器是错的。`Schedule.total_cycles()` = `runs × per_run_cycles()`；代价模型把
`run_overhead_cycles` 按 `runs` 累加；emitter 导出 `_PROG_CYCLES`（单次 run 长度）、
`_PROG_TRIPS_PER_RUN`、`_PROG_RUNS`，寄存器/spad 跨 run 保持，主机重复启动
`RUNS` 次即可。检查器 R3 只校验单次 run 不超 `max_run_cycles`。

## 尚未实现（刻意留白）

- 代价模型只算周期（计算 + `run_overhead_cycles`），不含面积/能耗；
- 搜索还没有 tile 划分与银行落点（多 run 切分与分量装箱已实现）；
- 发射器只覆盖 GPCP 4x4 的 ISA（8x8 仍回退到 listing）；
- DMA / 描述符环参与调度（model 里有字段，检查器暂未用）；
- 多页分离检查、旋转表约束（R9 已覆盖页冲突）；
- conformance 目前是**结构级**（合法性 + 长度），数值级对拍要等 DFG 带算子语义并接黄金模型。
