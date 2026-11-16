# proto/ 分类

协议描述层：把「算法做什么」与「硬件怎么落」分开。

| 文件/目录 | 作用 |
|---|---|
| `hll.py` | HLL 动词 API（`transport_block`、`encode`、`modulate`、`channel`、`estimate`、`equalise`、`transform`、`demap`、`decode` 等） |
| `check_hll.py` | 硬件无关检查：禁用词、无循环/自定义函数、换厨房测试 |
| `lower.py` | 动词 → target 落点（`stages[]`：where / impl / array_runs） |
| `emit.py` | 生成 `plan.h` + `stimulus.bin` + `golden.json` |
| `compile.py` | 场景 → bundle 的一键入口 |
| `proto_eval.py` | 判据、按时率、阶段占比、差距表、与手写基准的等价对账 |
| `load.py` | 1 ms 时隙时间轴与单服务器队列 |
| `models/` | 17 个场景的激励/黄金/SPEC |
| `scenarios/` | 17 个场景的 HLL 描述 |
| `targets/` | 换厨房用的目标配置（如 `whatif-8x8.yaml`） |

## 场景

NR：`nr_pdcch`、`nr_pdsch`、`nr_pdsch_16qam`、`nr_pdsch_64qam`、
`nr_pdsch_harq`、`nr_ssb`、`nr_csi`、`nr_pusch`、`nr_pusch_tp`、`nr_pucch`、
`nr_prach`、`nr_mimo`。

LTE/WiFi：`lte_pdsch`、`lte_pusch`、`lte_prach`、`lte_cell_search`、`wifi_11be`。

## 规则

- 场景层只写算法，不出现存储/页/拍数/搬运等硬件概念。
- 落点决定只在 `lower.py` + `cgra/target.yaml`。
- 新增场景：`models/` 写激励与黄金，`scenarios/` 写 HLL，`sw/soc/ops/<场景>_ops.inc`
  注册算子，最后加门禁。

## 命令

```bash
make proto-check
make proto PROTO=nr_pdsch
make -C sim/soc run-proto-nr_pdsch
```
