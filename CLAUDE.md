# CLAUDE.md — Claude Code 入口

> 本仓库的 agent 规则以 [`AGENTS.md`](AGENTS.md) 为**唯一事实来源**。本文件是入口摘要，
> 与 AGENTS.md 冲突时一律以 AGENTS.md 为准。

接手前先读：`AGENTS.md` §1（这是什么、到哪了）、§2（五条命令与预期输出）、
§4（15 条硬规矩，每条都是踩过的坑）、§9（回归分层）；阶段与验收见
`docs/11-项目计划.md`。

<!-- devlog-rule:v1 -->
### 强制规则：开发工时台账

每次开发都必须记录**这项任务花了多久**与**它属于哪个领域**：写 RTL、做综合、验证测试、
写软件（裸机 C / 驱动）、写微码、写协议模型、搭基建、写文档 —— 都算开发。

单一事实来源是 `docs/dev-log.csv`，工具是 `tools/devlog.py`。**不要手写 CSV**，用工具。

```bash
python3 tools/devlog.py start <domain> "<任务>"        # 开工立即记
python3 tools/devlog.py stop  --commit <hash> --evidence "<周期数 / 门数 / [OK] 行>"
python3 tools/devlog.py add --domain <domain> --kind <kind> --minutes N \
        --task "<任务>" --commit <hash> --confidence measured|estimate|backfill
python3 tools/devlog.py report                         # 按领域 / 类型 / 天 / 周汇总
make devcheck                                          # 提交前必须 0 error
```

`domain`（写什么）：`arch` `rtl` `microcode` `model` `sw` `verif` `synth` `pd` `infra` `doc` `wait`
`kind`（为什么花时间）：`feature` `fix` `infra` `experiment` `doc` `wait`

**四条不可协商：**
1. 功能 / 修复 / 基建提交都必须有台账行，`commit` 填提交哈希（多个用 `;` 分隔）。
2. 必须有 `evidence`（周期数、门数、`[OK]`、PASS 汇总至少一项），否则按**未完成**处理。
3. 精度不得伪装：`estimate`（事后估算 ±25%）不许写成 `measured`（有明确起止）。
4. 负结果也要记（`kind=experiment`，可以没有 commit）——它比成功的实验更能防止重复踩坑。

**口径细节**（wall-clock 到验收通过；纯等待单列 `domain=wait`；并行按投入比例不重复计；
单条 ≤3 h 否则拆分；跨领域必须拆成多条）：见 `AGENTS.md` §10。

### 完成定义（DoD）

一个任务要同时满足三条才算完成，缺一条不算：
1. 相关门绿 —— `make verify-smart`，或对应的 `make -C sim/soc run-*`；
2. `make devcheck` **0 error**；
3. 文档与实测数字同步（`AGENTS.md` §2 的数字、`docs/11` 的基线与变更记录）。
<!-- /devlog-rule -->
