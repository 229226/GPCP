# 验证体系

## 1. 原则

- **逐值对账**：不接受「流程跑通」「看着对」；每个门禁都要与黄金模型逐位/逐值比较。
- **单一事实来源**：黄金模型在 `tools/` 或 `proto/models/`，硬件输出只作为被测对象。
- **失败必须可复现**：门禁失败时保留 dump、检查器输出和随机种子。
- **新增功能必须带门禁**：在 `sim/soc/Makefile` 加 `run-*`，在 `tools/verify.sh` 注册。

## 2. 三层验证

| 层 | 位置 | 内容 |
|---|---|---|
| 模型自检 | `tools/*.py`、`proto/models/` | 协议序列/调制/编码/译码的黄金实现，`selftest` 入口 |
| 纯 CGRA 台架 | `sim/cgra/` | `tb_cgra`、`rot_test.py`：ISA、旋转表、描述符、4 KiB 边界 |
| SoC 门禁 | `sim/soc/` | CVA6 + CGRA + DMA + 中断的整链 `run-*` |

补充：

- `make proto-check`：协议描述层的静态检查（硬件无关、换厨房、落点、生成物）。
- `make yaml-check`：kernel YAML 与物理坐标解耦、描述符契约生成物同步。
- `make handprog-check`：禁止手写微码。

## 3. 一条命令验收

```bash
bash tools/verify.sh quick      # kernels + 10 个 SoC 目标，约 2.5 分钟
bash tools/verify.sh full       # + 模型自检 + 29 个 SoC 基准，约 8–10 分钟
bash tools/verify.sh slow       # full + run-rach，约 10 分钟
bash tools/verify.sh changed    # 只跑 git diff 触及的门
bash tools/verify.sh smart      # 日常默认：47 个门，按输入指纹跳过已通过项
bash tools/verify.sh slow --force   # 交付证据：忽略缓存全量
```

规则：

- `smart` 的 SKIP 不算 PASS；交付/阶段末必须 `--force`。
- 并行度默认 `nproc`（本机 16 核即 16 路；`--jobs N` 或 `JOBS=N` 可覆盖）。
  84 个门在 16 路下实测 587 s（8 路为 761 s；剩余差距主要来自长尾门与串行预构建）。
- 所有 Verilator 调用必须 `env -u LD_LIBRARY_PATH`。
- 每个 `run-*` 写自己的 dump/TD，互不覆盖；共享 twiddle LUT 由预构建阶段生成一次。

## 4. 门禁分类

| 类别 | 例子 | 判据 |
|---|---|---|
| kernel/ISA | `rot-test`、`model-*` | 逐位一致 |
| 数据面 | `run`、`run-dma`、`run-irq`、`run-ring`、`run-rxchain` | 黄金值逐字 |
| 同步/SSB | `run-sync`、`run-sss`、`run-ssb`、`run-ssbfft`、`run-ssb-fid`、`run-ssb-tx` | MIB 32/32、N_ID 正确 |
| FFT/IFFT | `run-fft`、`run-ifft`、`run-fftprobe` | 频谱/样本逐值 0 差 |
| LDPC | `run-pdsch*`、`run-ldpc-enc`、`run-pusch` | 硬判 1040/1040、TB 80/80、CRC |
| PDCCH/RACH | `run-pdcch`、`run-pdcch-loop`、`run-prach`、`run-rach` | DCI 32/32、4 步过程 |
| 协议基准 | `run-proto-*` | 判据 + 按时率 + 阶段占比 |
| 性能/开销 | `run-overhead`、`run-slot`、`run-ssb-slot` | 回归/时基无 overrun |

## 5. 检查器约定

- 每个 `run-*` 目标 = 一条 load + dump + `python3 tools/checks/check_*.py`。
- 检查器输出 `[ OK ] ...` 或 `[FAIL] ...`，失败返回非零。
- 结果区通常以 magic 开头（如 `0xCAFEBABE`、`0x1DCC0001`、`0x50524F54`），
  检查器先校验 magic，再校验长度、锚点和逐值结果。
- 台架的 `--watch-writes` 用于描述符/输出窗口：仿真后对比 64 KiB DMA 内存，
  窗口外任何一个字变化都算失败。
- 失败时先看 `build/verify.log` 与对应 `sim/soc/build/*_dump.txt`。

## 6. 失败排查顺序

1. **构建是否真的更新**：`.inc`/头文件是否在依赖里；必要时 `make -C sw/soc clean`。
2. **D$ 一致性**：引擎写完 RAM、CPU 读之前是否 `rt_dcache_flush()`。
3. **posted write**：阵列启动/环提交是否确认 start 生效。
4. **布局**：`.bss`、环、结果区是否重叠（链接期 `ASSERT` 与 `WIN_STATUS`）。
5. **数据契约**：描述符字段是否用了 `gpcp_d64_set_*`，DMA 是否越 4 KiB。
6. **最后才怀疑 RTL**：先用 `sim/cgra` 的纯 RTL 用例隔离。

## 7. 加一个门禁

1. 在 `sim/soc/Makefile` 写 `run-<name>`：load 程序 + 激励 + `--results` +
   `--dump` + `python3 ../../tools/checks/check_<name>.py`；
2. 检查器逐值对账并打印 `[ OK ]`；
3. 在 `tools/verify.sh` 的对应档位注册门名；
4. 跑 `bash tools/verify.sh smart --force` 确认全绿。
