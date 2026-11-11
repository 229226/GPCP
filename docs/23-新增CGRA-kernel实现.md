# 23 — 补齐 docs/22 中「仍缺阵列实现」的四项

> 状态：**已落地并验收（2026-11-11）**。
> 本文只说明新增 kernel 的实现、验收口径与边界；落点结论仍以 `docs/22` 的
> 17 场景双跑实测为准。与 `AGENTS.md` 冲突时以 `AGENTS.md` 为准。

---

## 1. 补齐了什么

`docs/22` §2.3 列出四项「仍缺阵列实现」的算法。本轮把它们全部做成
**编译器可生成、ISA 级可自检、SoC 硬件可运行**的 CGRA 镜像：

| 算法 | kernel YAML | 生成镜像 | 拍数 | 阵列语义 |
|---|---|---|---:|---|
| QAM 映射 QPSK | `qam_map.yaml` (variants) | `qam_qpsk_prog.h` | 14 | 16 PE 各映射 1 个符号，Gray 幅度解码 + 定标 |
| QAM 映射 16QAM | 同上 | `qam_16qam_prog.h` | 24 | 同上，`mag=3-2b1`，A=894 |
| QAM 映射 64QAM | 同上 | `qam_64qam_prog.h` | 34 | 同上，`val=2b1+(b1 xor b2)`，A=436 |
| LDPC 编码 acc | `ldpc_enc_acc.yaml` | `ldpc_enc_acc_prog.h` | 42 | 一个提升行内 20 条边循环移位后异或累加 |
| LTE Turbo 单步 ACS | `turbo_acs.yaml` | `turbo_acs_prog.h` | 8 | 每 PE 一个状态，两路前驱分支相加取 max |
| 802.11 LDPC 变量节点 | `wifi_vn.yaml` | `wifi_vn_prog.h` | 123 | `sum=ch+24` 条消息，输出各边外信息与硬判 |
| 802.11 LDPC 校验节点 | `wifi_cn24.yaml` | `wifi_cn24_prog.h` | 396 | 复用 `schedule_cn(edges=24)`，min1/min2/sign |

全部 7 张镜像由 `cgra/tools/gen_kernel_prog.py --all` 生成，带「勿手改」横幅，
布局契约 `sw/soc/*_layout.h` 由 `layout_contract.py` 生成。
**YAML 里没有任何物理坐标**（`check_yaml_coupling.py`：36 份 spec / 165 处
逻辑地址 / 33 处 `parallelism` / 0 处物理坐标），也**没有手写微码**
（`check_handprog.py`：0 处）。

### 1.1 两个设计要点

**Turbo 不需要循环式微码调度器。** `docs/22` 曾判断 Turbo 需要「可循环的微码
调度器」。本轮改用了项目里已有的 Polar SC 范式：8 个状态的每个 `ns` 恰好有
2 个前驱分支，所以「一 PE 算一个状态的两路候选再取 max」在**一次阵列运行内**
就能完成完整的一步递推，不需要跨 PE 归约、不需要循环。K=104 个 trellis 步与
8 次迭代由 C 侧循环驱动——与既有 `polar_node` 被 C 侧遍历 1120 次同一路数。
反向 beta 递推形式相同，换输入即可复用同一镜像。

**WiFi CN 直接复用参数化的既有调度器。** `schedule_cn(edges=...)` 本来就按
边数参数化，`edges=24` 的半镜像 `3+24*8+3=198` 拍、两个半 396 拍 < IMEM 512 上限，
因此不需要新 kernel，只需一份 `kind: cn` 的 YAML 指定 `edges` 与结果命名区域。

---

## 2. 验收（三层，逐层收紧）

| 层 | 命令 | 判据 | 结果 |
|---|---|---|---|
| 编译器自检 | `make kernels` | 每张镜像 `check_dfg` 逐值 0 差异 | 7/7 通过 |
| 独立黄金模型 | `make new-kernels-check` | 读**生成物**，ISA 仿真 vs 算法规格独立公式 | 7/7 通过 |
| SoC 硬件 | `make -C sim/soc run-new-kernels` | Verilator 全 SoC 跑 7 张镜像，732 个汇值逐值对账 | 通过 |
| 全量回归 | `bash tools/verify.sh full` | 既有门 + 新门全绿 | 见提交信息 |

**为什么要三层**：项目踩过 PRACH 的坑——编译器自带的 `check_dfg` 只证明
「微码 == YAML 的 DFG 语义」，如果 YAML 公式本身写错，两边一起错、照样 0 差异。
因此 `tools/check_new_kernels.py` **不复用 DFG 语义**，而是从算法规格独立推导
期望（38.211 的 LEVEL_SCALE/MAGNITUDE、GF(2) 异或、8 状态 Max-Log-MAP、
min-sum），再读回 `*_prog.h` 的真实指令字、用 ISA 仿真器逐拍执行、比对汇值。
第三层 `sw/soc/new_kernels_bm.c` 把同样的对账搬到真实 RTL 上再跑一遍。

### 2.1 门禁接线

- `Makefile: new-kernels-check` —— 全量重编 kernel + 独立黄金对账。
- `tools/verify.sh` 静态段 —— 每次 verify（含 quick）都跑独立黄金对账，秒级。
- `SOCCORE_QUICK` / `SOCCORE_FULL` 都含 `run-new-kernels`。
- `proto/lower.py` 登记 `NEW_ARRAY_IMAGES`，用 `PROTO_NEW_ARRAY=1` 显式切换。

---

## 3. 关键取舍：默认落点不变

`docs/22` 用 17 场景双跑实测证明：Turbo、WiFi LDPC、QAM、LDPC 编码当前
**留在 CVA6 更快，或阵列被编排拖累**（LDPC 阵列只忙 6%）。因此本轮**补齐实现
但不改默认落点**：

- `USE_NEW_ARRAY = os.environ.get("PROTO_NEW_ARRAY", "") == "1"`，默认关闭；
- 关闭时 `plan.h` 逐字节不变（已用 `cmp` 验证），17 场景既有周期数与门禁不受影响；
- 打开时 `modulate -> qam_map`、`encode(ldpc) -> ldpc_enc_acc`、
  `turbo -> turbo_acs`、`ldpc_wifi -> wifi_ldpc_tiles` 切到阵列，
  `compile.py --all` 与 `emit.py --all` 全链路已验证 rc=0；
- 换厨房测试（4×4 / whatif-8×8 的 `ir.json` 逐字节相同）仍通过。

这样实现是**可用、可 A/B、可验证**的，同时不推翻实测结论。后续 `docs/21`
P4/P5 若要真正下沉，应先修编排与主机接口，再用这个开关做周期对比，
而不是先把落点改掉。

---

## 4. 边界与后续（重要）

本轮交付的是**kernel 层实现**，不是端到端协议链接入。要真正把这四项
下沉到 17 个场景并拿到加速，仍需要：

1. **C 侧编排**：`*_stages.inc` 增加阵列分支。Turbo 需要把 8 次迭代、
   alpha/beta 递推、QPP 交织串起来；WiFi LDPC 需要 flooding 调度与
   每边不同旋转量的 gather/scatter。
2. **接口缺口**：`docs/21` R1 指出的「每 bank 旋转表」RTL 与 P6 的
   64-bit 主机口 / 任务链，是 LDPC 编排开销的根源。本次**未改 RTL**。
3. **周期实测**：只有接入后跑 `run-proto-*` 双跑，才能判断下沉是否真的更快。
   按 `docs/22` 的估算，短 tile 的阵列启动 + DMA 固定开销很可能吃掉收益
   （Polar SC 的 0.55×、MMSE 的 0.35× 就是前车之鉴）。
4. **Viterbi K=7 BCC** 仍无阵列实现（`docs/22` 判留 CPU），本轮未纳入。

因此，把这四项标记为「**有可用 CGRA 实现，默认落点仍为控制核**」，
而不是「已下沉并加速」。

---

## 5. 复现

```bash
# 生成全部 kernel 镜像 + 独立黄金对账
make kernels && make new-kernels-check

# SoC 硬件自检（7 张镜像 / 732 个汇值）
make -C sim/soc run-new-kernels

# 查看新实现打开后的落点（不改变默认产物）
PROTO_NEW_ARRAY=1 python3 proto/compile.py --all

# 全量回归
bash tools/verify.sh full
```
