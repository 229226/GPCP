# sw/soc/ 目录结构

裸机程序、共享阶段实现与生成的微码头文件按类型分目录存放。

```
sw/soc/
├── bench/   44 个裸机程序（*_bm.c、proto_runner.c）
├── stages/  11 个阶段实现（*_stages.inc）
├── ops/     18 个场景算子表（*_ops.inc，供 -DPROTO_OPS_INC）
├── lib/     6 个共享算法库（fft_cpu、ssb_fft、ldpc_enc、gold_seq、zc_corr、dft_bluestein）
├── gen/     103 个生成头文件（*_prog.h / *_layout.h / *_tab.h / *_luts.h）
├── start.S / start_irq.S / link.ld / Makefile
└── README.md
```

## 各目录职责

| 目录 | 内容 | 约定 |
|---|---|---|
| `bench/` | 每个程序对应 `sim/soc/Makefile` 的一条 `run-*` 门禁 | 新程序加入 `PROGS`，并在 `verify.sh` 注册 |
| `stages/` | 协议阶段实现，手写基准与 `proto_runner` 共用 | 只写一次，不在两处复制算法 |
| `ops/` | 协议运行器的 `proto_ops[]` + 锚点块 | 编译期由 `-DPROTO_OPS_INC='"<scenario>_ops.inc"'` 选择 |
| `lib/` | FFT/LDPC 编码/Gold/ZC 等共享算法 | 被多个 stage/bench 复用 |
| `gen/` | `cgra/kernels/*.yaml` 与各生成器的输出 | **勿手改**；改 YAML 后 `make kernels` |

## 构建

Makefile 在 `sw/soc` 下运行，关键变量：

- `VPATH := bench:gen:stages:ops:lib`：规则里的裸文件名（如 `pdsch_stages.inc`、
  `pdsch_cn_prog.h`）自动在这些目录里查找。
- `CFLAGS` 带 `-I. -I../include -Igen -Istages -Iops -Ilib`。
- 显式编译命令用 `$(BENCH)/xxx_bm.c`；`start.S`、`link.ld` 留在根。

新增/移动文件时：

1. 源文件放对应目录，保持裸 include 名（`"pdsch_stages.inc"`）；
2. 如新增目录，更新 `VPATH` 与 `CFLAGS -I`；
3. 相对包含 `../include/...` 要按新深度改成 `../../include/...`（bench/stages/ops/lib
   比根深一层）。
4. 生成器的输出路径统一写到 `sw/soc/gen/`；不要直接写根目录。

## 生成物

`gen/` 里的头文件全部由脚本生成：

- `*_prog.h`：`cgra/tools/gen_kernel_prog.py`（默认输出 `sw/soc/gen/`）；
- `*_layout.h`：`cgra/tools/layout_contract.py`；
- `*_tab.h` / `*_luts.h` / `rev4_lut.h` 等：`tools/gen/*` 或 `cgra/tools/*`。

改 kernel/表后运行：

```bash
make kernels
make -C sw/soc -j16 all
```
