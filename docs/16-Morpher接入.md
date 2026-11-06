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
