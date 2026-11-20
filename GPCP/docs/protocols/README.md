# 协议链与落点

## 1. 分层

```
scenario（场景）        proto/scenarios/nr_pdsch.py
   │  用 HLL 动词描述算法
   ▼
IR / plan               proto/hll.py → proto/emit.py
   │  plan.h + stimulus.bin + golden.json
   ▼
proto_runner.c          通用运行器，只认阶段表
   │  -DPROTO_OPS_INC="<scenario>_ops.inc"
   ▼
*_stages.inc            阶段实现（与手写 benchmark 共用同一份代码）
   │
   ▼
CGRA / CPU / DMA        按 target.yaml 的落点执行
```

关键点：

- 场景层不出现芯片概念（存储块、页、并行单元、拍数、搬运）；
  `make proto-check` 会做语法树禁用词检查与「换厨房测试」（同一场景换 target
  后源码不变、生成物不同、都能过自检）。
- 阶段实现只写一次：手写基准（`*_loop_bm.c`）与协议运行器
  （`proto_runner.c`）共用 `*_stages.inc`，所以「与手写基准逐值一致」是有意义
  的等价门。
- 落点由 `plan.h` 决定：每个阶段标注 `control-core` 或 `array`，以及计划拍数。

## 2. HLL 动词

`proto/hll.py` 提供的协议动词（按阶段顺序使用）：

`transport_block`、`encode`、`modulate`、`dmrs`、`correlate`、
`map_to_grid`、`extract`、`measure`、`ofdm_modulate`、`ofdm_demodulate`、
`channel`、`estimate`、`equalise`、`transform`、`demap`、`mimo_equalise`、
`decode`。

每个动词只描述「算什么」，不描述「在哪算」。`lower.py` 负责把动词落到 target
的 kernel/阶段，`emit.py` 生成运行器需要的三件产物。

## 3. 场景

`proto/models/` 下有 17 个场景模型：

| 制式 | 场景 |
|---|---|
| NR 下行 | `nr_pdcch`、`nr_pdsch`、`nr_pdsch_16qam`、`nr_pdsch_64qam`、`nr_pdsch_harq`、`nr_ssb`、`nr_csi` |
| NR 上行 | `nr_pusch`、`nr_pusch_tp`、`nr_pucch`、`nr_prach` |
| NR MIMO | `nr_mimo` |
| LTE | `lte_pdsch`、`lte_pusch`、`lte_prach`、`lte_cell_search` |
| WiFi | `wifi_11be` |

每个场景包含：激励生成、黄金结果、阶段判据、锚点块。运行器把结果写到统一
结果区，`proto/proto_eval.py` 做判据、按时率、阶段占比和与手写基准的等价对账。

## 4. 结果区契约

`proto_runner.c` 的固定布局（`PROTO_MAGIC = 0x50524F54`）：

| 偏移 | 内容 |
|---|---|
| 0 | magic |
| 1 | errors |
| 2 | cycles |
| 3 | array_runs |
| 4 | dma |
| 5 | busy（阵列忙拍数） |
| 6 | irq |
| 7 | overrun |
| 8 | params_hash |
| 9 | 结束标记 `0x600D` |
| 10 … | 每阶段 `(id, cycles, array_runs_delta)` |
| 之后 | 场景锚点块（校验器逐值对账） |

台架读数：`--results <addr> --nwords N`。不同场景的 N 不同，见
`sim/soc/Makefile`。

## 5. 负载模型

- 目标时钟来自 `cgra/target.yaml` 的 `clock_hz`（100 MHz）。
- `proto/load.py` 按 1 ms 时隙生成时间轴：每格一个传输块，单服务器队列，
  输出按时率、积压深度、单格超时倍数。
- 报告由 `proto_eval.py` 写出，包含阶段占比与差距表。

## 6. 命令

```bash
make proto-check                    # 静态检查：硬件无关 + 换厨房 + 落点
make proto PROTO=nr_pdsch           # 生成 build/proto/nr_pdsch/*
make -C sim/soc run-proto-nr_pdsch  # 跑仿真 + 判据 + 报告
```

## 7. 落点与扩展

- 默认落点偏保守：新 kernel 先有实现，只有显式 `PROTO_NEW_ARRAY=1` 才切到阵列，
  避免「一上线就变慢」。
- 加场景：在 `proto/models/` 写激励/黄金/判据，在 `proto/scenarios/` 用 HLL 动词
  写算法，在 `sw/soc/ops/<scenario>_ops.inc` 注册 `proto_ops[]` 与锚点块。
- 性能数字统一在 [../performance/README.md](../performance/README.md) 维护。
