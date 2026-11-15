# wlbench 契约规范（v1）

本文件定义子项目与硬件后端之间**唯一**的接口。任何后端（CPU、CGRA、FPGA、
GPU、纯软件参考）都只依赖这里的内容。

## 1. 版本

| 字段 | 值 | 说明 |
|---|---|---|
| bundle schema | `wlbench-bundle/1` | `manifest.json` 的 `schema` |
| IR schema | `gpcp-proto-ir/2` | 节点词汇 v2（见 §1.1）；v1 的 17 个 IR 已作废 |
| metrics schema | `wlbench-metrics/1` | 后端回报格式 |
| evaluation schema | `wlbench-evaluation/1` | `evaluate()` 输出 |

**兼容承诺**：同一 `schema_version` 下 `ir.json` 的节点结构不变；任何会改变它的
改动必须提升 `schema_version` 并重建回归锁。

### 1.1 v1 → v2：节点粒度

v1 里一个节点可以同时包含"阵列核 + 标量收尾"两种实现，`where` 只能填一个值，
于是出现"标着 CPU、实际跑了 9.8×"的情况（详见 GPCP `docs/25`）。v2 的规则是
**一个节点 = 一个实现决策**，为此：

| v1 | v2 | 原因 |
|---|---|---|
| `equalise_demap(y, h, qm)` | `equalise(y, h, qm)` + `demap(eq, qm)` | 均衡是矩阵/匹配滤波，解调是逐 RE 软判，并行度不同 |
| `modulate`（内含 DFT 扩频） | `modulate` + `transform(form="dft", size=N, count=n)` | DFT 是块变换，加扰/映射是比特/符号级 |
| `estimate(y, ref)` 内含接收 FFT | `transform(form="fft", count=n)` + `estimate(spec, ref)` | 变换不该藏在估计里；`includes_fft` 参数随之取消 |
| `equalise_demap` 内含 IDFT | `equalise` + `transform(form="idft", ...)` + `demap` | 同上 |

`transform` 的 `length = 2 * size * count`，`count` 是链上独立变换的次数（每个
OFDM 符号一次），后端据此算出阵列运行次数，不需要再从图上猜。

**数值不变**：v2 重建后 17 个场景的 `stimulus.bin` 逐字节相同、`golden.bits`
与 `anchors` 逐项相同，只有 `ir.json` 与 `params_hash` 变化。

## 2. Workload bundle

```
<out>/<scenario>/
  ir.json         语义图（协议动词 + 张量 + 参数），与后端无关
  stimulus.bin    激励（协议输入，按场景自定义二进制格式）
  golden.json     判据锚点 + 期望比特（只用于校验，不得用于"预知答案"）
  manifest.json   元数据：cfg / derived / traffic / budget / overrides / 产物哈希
```

### 2.1 `manifest.json`

```json
{
  "schema": "wlbench-bundle/1",
  "scenario": "nr_mimo",
  "rat": "nr", "direction": "dl",
  "cfg": {"bw_mhz": 20, "layers": 2, "rx": 2},
  "derived": {"n_rb": 106, "n_fft_min": 2048, "cp_len": 144},
  "traffic": {"arrival": "periodic", "jobs_per_slot": 1, "slots": 40},
  "budget": {"slot_ms": 1.0},
  "overrides": {},
  "ir_sha16": "3e28d76ae34d5c97",
  "artifacts": {"ir.json": {"sha256": "...", "bytes": 1348}}
}
```

- **`budget` 只允许时间（ms）**，不得出现周期数；周期预算由后端频率派生。
- `overrides` 记录 sweep 注入的参数点；空表示原始场景。

### 2.2 `ir.json`

关键字段：`schema / scenario / rat / direction / cfg / derived / traffic /
nodes / golden / budget`。`nodes` 是 `{id, op, in, out, length, params}` 的有序列表，
`op` 取自冻结动词表（`transmit_block / received / encode / modulate / dmrs /
correlate / map_to_grid / extract / measure / ofdm_modulate / ofdm_demodulate /
channel / estimate / equalise_demap / mimo_equalise / decode / expect / budget`）。

**后端只应读取 `nodes` 的 `op / params / length` 来安排执行**，不得依赖
`golden` 里的期望值。

两条与"语义诚实"有关的约定（2026-11-14 起）：

- **`derived` 必须写真实波形。** `n_fft_min / cp_len` 是**实际采样**的点数与
  CP，包含过采样：20 MHz NR @15 kHz 最少 2048 点，`cfg(oversample=2)` 后
  `derived.n_fft_min = 4096`、`cp_len = 288`。不允许出现"derived 写 2048、
  激励却是 4096 点"这种不一致——后端会按 derived 分配缓冲。
- **`estimate.params.includes_fft`**：为 `true` 时表示该动词内部含一次接收
  FFT（输入是时域样本、前面没有 `ofdm_demodulate`）。由场景显式声明，后端
  不再从图上推断。
- **场景里不许写死波形点数**（4096/2048/1024/288/144）：`check_hll` 会拦，
  请用 `S.sym_len(nsym, extra=0)`，它按 `derived` 现算。

### 2.3 `golden.json`

```json
{
  "scenario": "nr_mimo",
  "anchors": {"nbits": 720, "n_re": 180},
  "bits": {"packed": [1, 2, 3]},
  "params_hash": "0x1a2b3c4d",
  "stimulus_words": 17536
}
```

`params_hash = sha256(ir.json + stimulus.bin)[:8]`，用于检测"旧二进制跑新激励"。

## 3. Backend metrics

```json
{
  "backend": "gpcp-cgra",
  "clock_hz": 1053000000,
  "clock_source": "estimated-pre-layout",
  "total_cycles": 403722,
  "correct": true,
  "stages": [{"id": 0, "op": "received", "cycles": 42, "correct": true}]
}
```

### 3.1 必填

| 字段 | 类型 | 含义 |
|---|---|---|
| `backend` | string | 后端标识 |
| `clock_hz` | 正整数 | **该后端的运行频率**（不是仿真时钟） |
| `total_cycles` | 非负整数 | 整链周期 |
| `correct` | bool | golden 对账是否全部通过 |

### 3.2 可选

- `clock_source`：`measured` / `estimated-pre-layout` / `simulation`。用于在报告里
  标注口径，避免把估算频率当实测。
- `stages[]`：逐阶段 `id / op / cycles / correct`，用于差距表。

### 3.3 禁止

键名中不得出现架构概念：`spad`、`imem`、`microcode`、`array_runs`、
`pe_array`、`bank_conflict`、`tiling`、`context_page`、`planned_runs`、
`dma_beats`、`proto_where`。`validate_metrics()` 会直接拒绝。

## 4. 时间判定

```
budget_cycles = round(slot_ms × clock_hz / 1000)
wall_ms       = total_cycles / clock_hz × 1000
on_time       = total_cycles <= budget_cycles
```

连续到达（`traffic.arrival != single`）时，把 `total_cycles` 作为单个作业代价
喂入 `load.profile()` / `load.simulate()`，得到 `on_time_rate / late_slots /
worst_backlog`；`primary_on_time` 取 `on_time_rate >= 1.0`。

**不允许**把 `budget_cycles` 写进 workload：那等于把某个后端频率偷偷固化进协议
规范，换后端后预算会错而 `ir_sha` 不变，极难排查。

## 5. 边界

`oversample` 属于 **wlbench**（协议参数），`n_fft_min` 是它的派生量；
"用哪套 FFT 镜像"属于后端。"波形点数写死成某个镜像的尺寸"是两者的混淆，
由 `check_hll` 的 `HW_LITERALS` 规则拦住。

| 属于 wlbench | 属于后端 | 禁止进 wlbench |
|---|---|---|
| 协议参数、时隙预算、业务模型、按时率、差距表、判定公式 | `clock_hz`、`cycles`、`correct`、执行方式 | 存储层次、并行单元、指令内存、微码、搬运、结果区偏移 |

判据：**频率与周期是任何计算装置都给得出的通用量**；`array_runs`、`bank 冲突`
只有特定架构才有。

## 6. 变更流程

1. 新增协议参数 → 加进 `cfg_schema.KNOWN`，不改变已冻结场景的输出；
2. 新增波形/制式 → 在 `numerology/` 加表，`derive()` 增加分支；
3. 新增动词 → 修订动词表并提升 `schema_version`，同时重建回归锁；
4. 修改 `ir.json` 任何字段 → 必须提升 `schema_version`，旧锁作废并记录迁移说明。
