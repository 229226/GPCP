# cgracc — 架构无关的 CGRA 编译器骨架

目标：**新增一个架构，只提供 `arch/<name>/model.yaml` + `emitter.py`，编译器核心 0 行改动。**

当前状态是**骨架 + 一条可跑的端到端路径**（列表调度器 + 合法性检查器 + 参考发射器），
不是完整编译器。规则调度器、模调度、CP-SAT、真实微码编码都还没接。

## 快速开始

```bash
cd d:/Data/GPCP/cgracc

# 1) 看架构摘要
python3 -m cgracc info --arch gpcp_4x4

# 2) DFG -> 调度（生成 build/schedule.json）
python3 -m cgracc schedule --arch gpcp_4x4 --dfg examples/dot4.json -o build/schedule.json

# 3) 合法性检查（空输出 = 通过）
python3 -m cgracc check --arch gpcp_4x4 --dfg examples/dot4.json --schedule build/schedule.json

# 4) 发射（参考发射器，输出人读的 listing，不是真微码）
python3 -m cgracc emit --arch gpcp_4x4 --dfg examples/dot4.json --schedule build/schedule.json

# 5) 自测
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
    sched/list_sched.py # 参考实现：列表调度（够跑通，不追求 QoR）
    verify/legality.py  # 合法性检查器（规则全部来自 model）
    emit/base.py        # Emitter 接口
    cli.py              # info / schedule / check / emit
  arch/
    gpcp_4x4/           # 架构 0：GPCP 4x4（model.yaml + emitter.py）
  examples/dot4.json    # 最小 DFG
  docs/porting.md       # 接入新架构的 checklist
  tests/test_smoke.py   # 端到端 + 负例（必须被拒绝）
```

## 边界

- **编译器拥有**：IR、调度框架、合法性检查、发射框架、conformance。
- **架构方拥有**：`model.yaml`（五段契约）+ `emitter.py`（Schedule → 该机器的微码/位流）。
- 编译器核心**不允许出现任何架构名**（后续加 CI grep 检查）。

## 尚未实现（刻意留白）

- 模调度（II）/ CP-SAT / 代价模型与搜索；
- 真实 IMEM 编码（`emitter.py` 只是 listing）；
- DMA / 描述符环的调度（model 里有字段，检查器暂未用）；
- 多页分离检查、旋转表约束；
- conformance 的 4 正向 + 1 负向用例集（现在只有 1 个示例 + 1 个负例测试）。
