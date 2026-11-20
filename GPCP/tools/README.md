# tools/ 文件分类

本目录是 Python 黄金模型、生成器、检查器与基础设施。按文件名前缀分类：

| 前缀/文件 | 数量 | 作用 | 入口 |
|---|---:|---|---|
| `check_*.py` | 51 | 每个门禁的检查器：读 dump、校验 magic/长度/锚点、逐值对账 | 被 `sim/soc/Makefile` 的 `run-*` 调用 |
| `nr_*.py` | 25 | 5G NR 黄金模型（序列、编码、调制、LDPC、PDSCH/PDCCH/PUSCH…） | 各 `check_*` 与 `proto/models/` import |
| `ssb_*.py` | 2 | SSB 相关模型（`ssb_plan.py`、`ssb_plan_p1.py`） | SSB 链路 |
| `wifi_*.py` | 1 | WiFi 11be 模型 | `proto/models/wifi_11be.py` |
| `gen_*.py` | 8 | 生成器：LUT、编码表、账本、PDCCH 表等 | `make` 目标或手动 |
| `pdsch_plan.py`、`pdsch_full_plan.py` | 2 | PDSCH 收发计划/成本模型 | 协议与性能分析 |
| `ldpc5g.py`、`ldpc_nr.py` | 2 | LDPC 移位表/编码参考 | 模型与硬件对账 |
| `sym_pc.py`、`sweep_snr.py` | 2 | 符号级 PC 模型、SNR 扫描 | 实验 |
| `proto_compare.py` | 1 | 手写基准与协议运行器的等价对账 | `make proto` 流程 |
| `cgra.py` | 1 | ISA 编码、15 个冻结 kernel、自检 | `python3 tools/cgra.py test` |
| `checklib.py` | 1 | 检查器共享的解析/打印库 | 所有 `check_*.py` |
| `fft_plan.py` | 1 | FFT 规划、twiddle LUT、黄金模型 | `run-fft` / `run-ifft` |
| `verify.sh`、`verify_cache.py` | 2 | 门禁编排与输入指纹缓存 | `bash tools/verify.sh smart` |
| `devlog.py` | 1 | 工时台账录入/审计 | `make devcheck` |
| `cgra_coverage.py`、`cgra_utilization.py` | 2 | 覆盖率/利用率统计 | 性能分析 |
| `perf_report.py` | 1 | 性能报告生成 | 手动 |
| `dup_audit.py` | 1 | 重复实现审计 | 手动 |
| `dft_bluestein_tab.py`、`dump_link_e2e.py` | 2 | 表生成、链路 dump 工具 | 手动/调试 |
| `data/`、`kernels/`、`morpher/` | — | 数据文件、CGRA-ME/Morpher 实验脚本 | 各自 README/脚本 |

## 约定

- 新门禁 = `check_<gate>.py` + `sim/soc/Makefile` 的 `run-<gate>` +
  `tools/verify.sh` 注册。
- 检查器只读 dump/模型，不写硬件；共享的解析逻辑放 `checklib.py`。
- 黄金模型与检查器分离：模型可以独立 `selftest`，检查器只做对账。
- 文件名前缀就是分类，不再额外建子目录；新增文件请沿用前缀。
