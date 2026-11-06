# 16. 把 Morpher 接成项目的编译器（进行中）

> 目标已从"拿它做基准线"改为"用它当编译器"。本文记录已完成的部分、发现的硬约束、以及桥接方案。

## 1. 已经打通的：调度导出

Morpher 原本只输出 HyCUBE 专用位流，拿不到"哪个算子在第几拍、哪个 PE"。
补丁 `tools/morpher/patch_morpher_schedule.py` 在 `PathFinderMapper` 里加了一个
`dumpSchedule()`，把 `(t, x, y, dfg_idx, op)` 写成 `schedule.csv`。

实测（array_add，II=4）：

```
t,x,y,idx,op
0,2,0,14,LS        <- 地址计算（左移）
0,0,2,11,LOADB     <- 循环基址
1,0,2,8,ADD
2,0,1,20017,STOREB
3,0,0,5,STORE
3,0,1,10,LOAD
3,0,2,7,LOAD
...
```

编译：`make -j8`（容器内），通过，仅一条既有 narrowing 警告。

## 2. 三个硬约束（决定桥接怎么做）

### 2.1 映射器只映射"一个标量循环体"

array_add 的 II=4 里，**整个阵列只有一份数据通路**：1 个 LOAD、1 个 ADD、1 个 STORE，
即一次迭代只处理 **1 个元素**、只用到 1 个算力 PE。

要利用我们 16 个 PE，必须在 **C 层展开 16 次**（它们的 `*_unroll` 目录正是这么做的），
让 DFG 出现 16 份并行数据通路。代价是 DFG 规模 → 52 节点时映射已需 6 分钟以上（§见 docs/15）。

### 2.2 DFG 里混着控制流与地址计算

array_add 的 20 个节点里，只有 6 个是"数据"（2 个 LOAD、1 个 ADD、1 个 STORE、2 个地址 ADD），
其余是 `SELECT / CMP / CMERGE / MOVC / LOADB / STOREB` —— 循环归纳变量与谓词。

我们的阵列**没有分支、没有谓词**，所以这些节点必须被"剥掉"，由主机侧承担：
循环由主机重复启动，地址变成 spad 立即数（我们本来就用 `{bank,word}` 立即数寻址）。

### 2.3 位流是 HyCUBE 专用的

`printHyCUBEBinary()` 打的是它的 PE 配置格式，与我们 32 位编码无关。
所以后端（schedule → 我们的微码）必须自己写，这也是本项目的差异化部分。

## 3. 桥接架构

```
 kernel.c ──(Morpher 前端)──> DFG.xml ──(Morpher 映射器)──> schedule.csv
                                                              │
                                          ┌───────────────────┘
                                          ▼
                        tools/morpher/cc.py  （我们自己写）
                        ├─ 剥离控制节点，只留数据通路
                        ├─ 地址折叠成 spad 立即数
                        ├─ 8 寄存器分配（按 PE）
                        ├─ 邻近路由 or 插入 MOV 跳数
                        └─ 生成 Program（cycle × 16 PE）
                                          │
                                          ▼
                        rule_map.simulate() 对拍黄金模型
                                          │
                                          ▼
                        run-* 门（硬件）
```

**关键取舍**：Morpher 负责它最擅长的部分（DFG + 放置 + 路由 + II 搜索），
我们负责 ISA 编码、寄存器分配、控制流剥离——这三件它的模型里根本没有。

## 4. 下一步（按依赖顺序）

| # | 任务 | 验收 |
|---|---|---|
| 1 | 拉取并归类 array_add 的全部 20 个节点，标出哪些属于"数据通路上" | 得到可执行的 6 节点子图 |
| 2 | 写 `cc.py` 的第一个版本：只支持"每 PE 独立、无跨 PE 通信"的情形 | array_add 的 1 元素/迭代能编成 4 拍微码 |
| 3 | 用 `rule_map.simulate()` 跑通并与 C 语义对拍 | 逐位一致 |
| 4 | 支持跨 PE（邻居 link）通信与寄存器分配 | FIR 的 16 抽头版本 |
| 5 | 接入 Makefile + verify 门 | `run-morpher-array-add` |

## 5. 最大的风险

**规模**：展开 16 次后 DFG 约 100–200 节点，Morpher 的映射时间可能到几十分钟。
若如此，"Morpher 当编译器"只能用于离线编译（不是每次 make），
或者需要换它的 `mapping_method`（SimulatedAnnealing / LISA）并实测。
这一条要在第 1–2 步之间尽快量出来，否则后面都是白做。

## 6. 更正：ADL 才是正确的描述入口（2026-11-06 实测）

我在 §2 里写"算子集硬编码、加不了累加器"——**这个结论是错的**，只对了一半：

- `CGRA.cpp:469` 明确把 ADL 里 `FU.OPS` 读进 `FU::supportedOPs` 并 `insertGlobalOP`；
- `PE.cpp` 里那张 `getNonMEMIns()` 硬编码表只是**默认值**，不是唯一来源。

三次实测：

| 实验 | 结果 |
|---|---|
| 原 ADL 追加 `MAC/LACC/MACL/SHR/SHL/MAX/MIN` | 映射照常 **II=4，8 s** |
| 从 ADL 移除控制类算子（`SELECT/CMP/CMERGE/BR/...`） | 仍成功，**II=8，39 s** |
| **写我们自己的 ADL**：16 条指令 + `r0..r7` + `acc` | **Map Success，II=8，40 s** |

第三项的产物就是 `tools/morpher/gpcp_cgra.json`。

### 6.1 结论怎么改

不是"它的模型没有累加器"，而是"它的**仿真器**没有累加器"。**映射器只吃 ADL**，
而 ADL 能描述：模块（FU/RF/MU）、端口、连线、寄存器表、算子表（含延迟）。
我们 ISA 的特殊之处（累加器、8 寄存器、最近邻 link）在结构层面都能写进去。

于是接入路径变成干净的三段：

```
我们的 ADL（gpcp_cgra.json）
        ↓
Morpher 前端 + 映射器  →  schedule.csv
        ↓
我们的后端：编码成 32 位微码 + 8 寄存器分配 + link 语义补偿
```

### 6.2 仍然存在的三个真问题

1. **位流是我们的**：`printHyCUBEBinary` 打的是它的 PE 配置格式，与 32 位编码无关，
   所以 schedule 之后必须自己编码（已能导出 schedule，见 §1）。
2. **link 更新语义**：它的 PE 里 FU 结果总是写 `TREG` 并驱动四个方向；我们的 `MAC`
   不更新 link。ADL 是结构语言，表达不了"某条指令不写回"。后端补偿：需要路由的
   `MAC` 结果后面插一条 `MOV`。
3. **ADL 只能加不能减**：PE 级硬编码表仍会"放行"我们没声明的算子，所以把控制类算子
   从 ADL 删掉并不会让映射失败（实测 II 从 4 变 8 而已）。要真正限制需改 C++ 一处，
   当前不影响使用，但如果要保证"生成的微码一定是我们的指令集"，后端必须做白名单校验。

### 6.3 对工作量的影响

原先估"加算子要改 C++、2–3 周"，现在加算子只是改 JSON 的一行。
真正的工作量集中在**后端**（schedule → 我们的编码 + 寄存器分配 + link 补偿）和
**规模验证**（展开 16 次后的 DFG 映射时间）。
