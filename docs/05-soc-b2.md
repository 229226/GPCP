# B2：CVA6 + CGRA 裸机联调（已跑通）

> 目标：用**真实 RISC-V 核**替代 M0 的 C++ 总线 BFM，CPU 通过 AXI4 真正驱动 CGRA。
> 结果：CVA6（RV64GC）裸机程序在 Verilator 中启动 → 下发 CGRA 微码/预装载 → 轮询完成 →
> 回读 16 个累加器 → 与黄金模型逐位一致，**2058 个周期结束**。

## 1. 组成

```
rtl/soc/gpcp_soc.sv      SoC：CVA6 + 地址译码 + RAM + CGRA + 退出寄存器
rtl/cgra/*               复用 M0 的 CGRA（同一套寄存器/ISA，未改动）
sim/soc/tb_soc.cpp       Verilator TB：装载镜像、跑、等退出、回读 RAM
sim/soc/cva6.f           CVA6 子集的文件列表（scripts/gen_flist.py 生成）
sw/soc/                  裸机程序：start.S / link.ld / cgra_bm.c
tools/check_soc.py       结果比对（Python 黄金模型）
third_party/cva6/        CVA6 源码（通过 scripts/fetch_cva6.py 拉取）
```

## 2. 地址映射

| 起始 | 结束 | 目标 | 属性 |
|---|---|---|---|
| `0x8000_0000` | `0x8007_FFFF` | 512 KiB RAM（读写/执行） | cacheable（WT cache） |
| `0x4000_0000` | `0x4000_FFFF` | CGRA 寄存器窗口 | uncached，单拍 |
| `0x4001_xxxx` | — | 仿真控制（写 = 退出） | uncached |

CGRA 窗口内部沿用 M0 的 16 位偏移：`0x0000` 控制/状态、`0x1000` PE 寄存器、`0x1200` 累加器、
`0x2000` 指令存储器。

## 3. CPU 配置

- 配置包：`cv64a6_imafdc_sv39`（RV64GC、WT cache、AXI 64-bit、ID 4-bit）
- 缓存区间：`0x8000_0000` + `0x4000_0000`（来自配置包 `CachedRegionAddrBase/Length`）
- 启动地址：`0x8000_0000`（M 模式），hart id = 0
- 中断全部拉低；未使用调试模块

## 4. SoC 侧 AXI 从机实现要点

1. **单 outstanding**：`arready/awready` 只在空闲且请求有效时拉高，主设备自然被节流；
   AXI 允许从机这样做，对 cache miss 引擎与 uncached MMIO 都够用。
2. **写通道**：`aw` 与 `w` 同时有效才接收；按 `wstrb` 逐字节写入，支持 `len>0` 的 INCR 突发。
3. **读通道**：按 AXI 字节 lane 规则返回（数据放在被寻址的 byte lane，而不是固定低 lane）；
   支持 `len` 突发（icache 的 8 拍 line refill）。
4. **CGRA 访问**：64-bit AXI 数据总线 → 取对应 32-bit lane（地址 `[2:0]==0` 取低 32 位，
   否则取高 32 位），写操作转换为 `cgra_top` 的单周期 `wr_en` 脉冲。
5. **退出**：写 `0x4001_xxxx` 置 `exit_o`，TB 停止仿真并回读 RAM。

> 注意：CVA6 的 `ariane` 包装默认配置是 `cva6_cfg_empty`（全 0），必须由 SoC 传入
> `build_config_pkg::build_config(cva6_config_pkg::cva6_cfg)`，否则会看到大量
> "Size-changing cast to zero/negative size" 之类的伪错误。

## 5. 裸机程序

`sw/soc/cgra_bm.c`：

1. 写入 4 拍微码：`MAC rk, r(4+k)`（`k=0..3`，16 个 PE 各一条）；
2. 预装载：`PE(i,j)` 的 `r0..r3 = A[i][:]`、`r4..r7 = B[:][j]`；
3. `NUM_CYCLES=4` → `CTRL.start=1` → 轮询 `STATUS.done`；
4. 回读 16 个 `acc`，写入 `0x8000_2000`，再写 marker 与退出寄存器。

编译（RV64GC，无 libc）：

```bash
make -C sw/soc     # 产出 build/sw/cgra_bm.elf / .bin / .dis
```

## 6. 复现

```bash
# 1) 拉取 CVA6（本机 github.com:443 被封，用 raw/api 方式）
python3 scripts/fetch_cva6.py

# 2) 生成文件列表（已生成好）
python3 scripts/gen_flist.py

# 3) 构建 Verilator 模型（CVA6 约 35 s，2 线程）
make -C sim/soc

# 4) 编译裸机程序并跑
make -C sim/soc run
```

预期输出：

```
[warn] CGRA poll loop spun 1 times
[info] exit_code=0x0000600d cycles=2058
[ OK ] CVA6 -> CGRA matmul matches the golden model
```

## 7. 已知限制（下一步）

- 无 UART / 中断：结果靠 RAM 回读，后续接 UART + PLIC 中断；
- 无 DMA：CGRA 数据仍靠 CPU 逐寄存器预装载，下一步加描述符环 + AXI4 主口；
- 无 Linux：当前只跑裸机，B3 再接 CVA6 + Linux（BusyBox initramfs）；
- RAM 512 KiB：够裸机，Linux 需要扩到 64 MiB 以上（需要更省内存的 RAM 模型）；
- 仅支持单 outstanding，复杂并发场景需换成 `axi_xbar` / `axi_demux` 成熟 IP。
