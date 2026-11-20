# GPCP 开发文档

本目录按功能块组织，每块一篇汇总文档；需要的细节以子文档补充。
文档不记录流水账，只保留当前仍然成立的设计、接口、命令与数字。

## 功能块

| 功能块 | 文档 | 回答的问题 |
|---|---|---|
| 硬件架构 | [architecture/README.md](architecture/README.md) | 系统由什么组成、地址空间、主机接口、平台状态 |
| CGRA 微架构 | [architecture/cgra.md](architecture/cgra.md) | 阵列/存储/ISA/描述符长什么样 |
| 编译器与微码 | [compiler/README.md](compiler/README.md) | kernel 如何从 YAML 变成可执行微码 |
| 软件栈 | [software/README.md](software/README.md) | 裸机程序如何驱动阵列、DMA、描述符环 |
| 协议链 | [protocols/README.md](protocols/README.md) | NR/LTE/WiFi 场景如何分层、如何落到阵列 |
| 验证体系 | [verification/README.md](verification/README.md) | 门禁有哪些、怎么跑、怎么判定 |
| 性能与 PPA | [performance/README.md](performance/README.md) | 当前数字、度量口径、优化方向 |
| 项目与流程 | [project/README.md](project/README.md) | 里程碑、目录地图、开发规则 |

## 阅读路径

1. 新接手：`architecture` → `software` → `verification`；
2. 改硬件/ISA：`architecture/cgra.md` → `compiler`；
3. 加协议/场景：`protocols` → `software` → `compiler`；
4. 做性能：`performance` → `protocols`；
5. 日常开发：`project` 的命令与规则，然后 `verification`。

## 约定

- 文档中的路径相对于仓库根目录。
- 代码是事实来源；文档与代码冲突时以代码和 `AGENTS.md` 为准，并修正文档。
- 工时台账 `dev-log.csv` 留在本目录，由 `tools/devlog.py` 管理，`make devcheck` 校验。
