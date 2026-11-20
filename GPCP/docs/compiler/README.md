# 编译器与微码生成

## 1. 目标与边界

- kernel 的**算法结构**写在 YAML/spec 里，微码由工具生成，生成物带「勿手改」头。
- 生成器必须自带 ISA 级自检：把生成的微码在 Python 模型上跑一遍，与黄金结果逐位
  比较，不一致就 `exit 1`。
- 不允许手写微码；`make handprog-check`（`tools/checks/check_handprog.py`）会扫描仓库。
- 不允许在 kernel YAML 里写 spad 平坦坐标、bank 号、PE 数等物理数字；这些只能
  来自 `cgra/target.yaml`。

## 2. 事实来源

```
cgra/target.yaml                 硬件几何/ISA/命名区域（唯一真相）
cgra/kernels/*.yaml              kernel 结构（kind/leaves/ops/sinks/bank hint）
cgra/tools/*.py                  生成器与映射器
sw/soc/gen/*_prog.h                  生成物（微码数组 + 周期数）
sw/soc/gen/*_layout.h                生成的 C 侧布局契约（区名 → bank/word）
rtl/cgra/cgra_desc64.yml         描述符字段契约（另一条生成链，见 software）
```

`cgra/target.yaml` 的 `regions` 给逻辑区名（IN/CONST/DATA/REF/OUT/LLR/HARD…）
对应的 bank 起点；kernel 只写 `at.region` 或 `region + k`。

## 3. 工具链

```
kernel.yaml ──► kernel_spec ──► rule_map（SIMD 调度）
                    │                 │
                    │                 ├─ bank_alloc（区间图着色）
                    │                 └─ target（逻辑地址 → flat）
                    └──────────────► gen_kernel_prog.py ──► *_prog.h
                                        │
                                        └─ ISA 自检（黄金模型逐位）
```

| 工具 | 职责 |
|---|---|
| `cgra/tools/target.py` | 读 `target.yaml`，逻辑地址 → flat 的唯一换算层 |
| `cgra/tools/kernel_spec.py` | 解析 kernel YAML，校验 kind/叶子/算子/汇 |
| `cgra/tools/rule_map.py` | DFG → 微码：按数据依赖拍串行发射，并行来自多实例分 PE |
| `cgra/tools/bank_alloc.py` | 叶子/汇的 bank 区间图着色；显式 `bank:` 作为强制 hint |
| `cgra/tools/gen_kernel_prog.py` | 统一入口：`--spec`、`--all`、`--check` |
| `cgra/tools/image_spec.py` | `kind: image` 的多子图串接（MMSE/FFT 等复合镜像） |
| `cgra/tools/fft_image.py` | `kind: fft_image` 的蝶形专用调度 |
| `cgra/tools/layout_contract.py` | 生成 `*_layout.h`，让 C 侧也用区名而不是裸坐标 |

### kernel 类型

| kind | 生成路径 | 典型用途 |
|---|---|---|
| `bn` / `bn_rot` | `rule_map.schedule_bn_split` | LDPC 变量节点（含旋转版） |
| `cn` / `cn_wb` | `rule_map.schedule_cn` | LDPC 校验节点（min-sum / 写回） |
| `dfg` | `kernel_spec.build_dfg` + `rule_map.schedule` | 通用数据流 kernel（多数） |
| `image` | `image_spec` | 多段串接的复合 kernel（MMSE 等） |
| `fft_image` | `fft_image` | FFT 蝶形/尾块 |

## 4. 调度模型

- **拍号 = 数据依赖层号**：同一 PE 上按拓扑序串行发射，每条指令 1 拍。
- **并行 = 数据实例**：第 w 个实例跑第 w 个 PE；这就是 SIMD 广播模型。
- 算子结果留在 `r0..r7`；只有叶子和汇访问 spad。
- 目前不做：跨 PE link 路由的自动规划、PE 内多拍软件流水、寄存器 spill。
  link 需要多跳时由 `rule_map` 的后续阶段或专用 kernel 处理。

## 5. 自检与门禁

```bash
# 生成全部 kernel 并自检
python3 cgra/tools/gen_kernel_prog.py --all
make kernels

# 重生成后检查生成物是否有意外变化
git diff --stat sw/soc/gen/*_prog.h sw/soc/gen/*_layout.h

# 禁止手写微码 / YAML 与物理坐标解耦
make handprog-check
make yaml-check
```

自检内容：

1. 每条指令编码能被 `cgra.py` 的 ISA 模型执行；
2. 所有叶子的 spad 地址、bank、word 在合法范围；
3. 汇的结果与黄金模型逐位一致；
4. bank 数 ≤ 31（`spad_idx` 只有 5 位），超了直接报错。

## 6. 添加一个新 kernel

1. 在 `cgra/kernels/` 写 YAML：`name/kind/desc/out`，以及 `leaves/ops/sinks`
   （或 `edges` 等 kind 专用字段）；
2. 需要固定布局时写 `bank:` hint，否则交给 `bank_alloc`；
3. 运行 `gen_kernel_prog.py --spec ...`，确认自检通过；
4. 在 `sw/soc/Makefile` 给对应程序补生成头依赖；
5. 在 `sw/soc/` 的 stage/ops 文件里引用生成的 `*_prog.h` 与 `*_layout.h`；
6. 加门禁：SoC 级 `run-*` + checker，并在 `tools/verify.sh` 注册。

## 7. 自动映射的结论

- CGRA-ME：8×8 ADL 能跑，但给不出可靠落点；只作为实验工具保留。
- 自研 C 前端 + Morpher 后端：前端产不出纯数据流 DFG，后端把不同节点铺到不同
  PE（空间映射），与本项目的 SIMD 执行模型不兼容，已判定不可用。
- 当前主线是「规则映射 + 多实例 SIMD」，不是空间映射编译器。

## 8. 常用命令

```bash
make kernels                 # 重生成全部 kernel 微码
make handprog-check          # 禁手写微码
make yaml-check              # YAML 解耦 + 描述符契约生成物同步
make -C sw/soc -j8 all       # 重编全部裸机程序
```
