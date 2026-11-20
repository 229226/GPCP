# cgra/kernels/ 分类

每个 YAML 是一个 kernel spec，由 `cgra/tools/gen_kernel_prog.py` 生成
`sw/soc/gen/*_prog.h`。文件名按算法族分类：

| 族 | 文件 |
|---|---|
| LDPC | `bn_24`、`bn24`、`bn_rot`、`cn`、`cn4`、`cn10`、`cn20`、`cn_wb`、`vn3`、`ldpc_enc_acc` |
| FFT | `fft_butterfly`、`fft_image` |
| Polar | `polar_node`、`polar_inword_s0`–`s4`、`polar_word_d1/d2/d4/d8` |
| 相关/同步 | `pss_corr`、`sss_corr`、`prach_corr`、`bpsk_corr`、`chnest16` |
| MMSE/复乘 | `mmse_2x1`、`mmse_dual`、`mmse_full`、`cplx_mul` |
| 场景/复合 | `qam_map`、`rxchain`、`wifi_vn`、`wifi_cn24`、`turbo_acs` |
| 玩具/自检 | `add2`、`vadd`、`matmul`、`stress_20leaf` |

## YAML 结构

公共字段：

```yaml
name: cn20          # 唯一名
kind: cn            # bn / bn_rot / cn / cn_wb / dfg / image / fft_image
desc: "..."         # 一句话说明
out: sw/soc/gen/pdsch_cn20_prog.h
```

其余字段按 kind：

- `bn` / `cn`：`edges`、`sentinel_bank`、`res_banks` 等；
- `dfg`：`leaves` / `ops` / `sinks`，可带 `bank:` hint；
- `image` / `fft_image`：子图串接与蝶形调度参数。

## 规则

- 不写 spad 平坦坐标、bank 号、PE 数；只用 `cgra/target.yaml` 的命名区域
  或 `bank:` hint。
- 一个 kernel 一个 `out:`；生成文件带「勿手改」头。
- 生成时自带 ISA 级自检，失败 `exit 1`。
- 新增 kernel 后：在 `sw/soc/Makefile` 补生成头依赖，在对应 `*_stages.inc` /
  `*_ops.inc` 引用，并加 SoC 门禁。

## 命令

```bash
python3 cgra/tools/gen_kernel_prog.py --all
make kernels
make handprog-check
make yaml-check
```
