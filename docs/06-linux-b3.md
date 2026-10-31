# B3：CVA6 跑 Linux（OpenSBI + BusyBox initramfs）

> 目标：在 B2 的 SoC 上补齐 Linux 所需的外设与语义，启动 **OpenSBI → Linux 6.6 → BusyBox**，
> 并在用户态访问 CGRA。
>
> 状态：**OpenSBI 已完整启动**；**Linux 6.6 内核已进入 earlycon 阶段并输出日志**：
> ```
> [    0.000000] earlycon: sbi0 at I/O port 0x0 (options '')
> [    0.000000] printk: bootconsole [sbi0] enabled
> [    0.000000] OF: reserved mem: ... mmode_resv0@80040000
> ```
> 内核正在 memblock / memmap / 页表初始化阶段推进。受仿真速度（约 55k cycles/s）
> 限制，跑到 BusyBox 用户态需要 10^8 量级周期（约 30–60 分钟仿真）。见 §8。

## 1. SoC 更新（相对 B2）

| 项 | B2 | B3 |
|---|---|---|
| RAM | 512 KiB | **64 MiB**（0x8000_0000–0x83FF_FFFF） |
| 地址映射 | 自定 | **对齐 QEMU `virt`**，直接复用标准 Linux 驱动 |
| CLINT | 无 | 0x0200_0000（msip / mtimecmp / mtime，mtime 每周期 +1） |
| PLIC | 无 | 0x0C00_0000（最小寄存器桩，claim 返回 0） |
| UART | 无 | 0x1000_0000，16550A，**4 字节寄存器步进**（reg-shift=2），TX 走 `$write` |
| 退出 | 0x4001_xxxx | 不变（仿真专用） |
| CGRA | 0x4000_0000 | **0x5000_0000**（避开 virt 的 PCIe 区域） |
| AXI | 单 outstanding + 突发 | 追加 **AXI 原子（atop）** 与 **独占访问（LR/SC）** |

### 1.1 AXI 原子（atop）

CVA6 的 `wt_axi_adapter` 对 `amoswap/amoadd/...` 直接发 **AXI5 原子写**：
`aw.atop != 0`，要求从机在 **R 通道返回旧值**，同时给出 B 响应。

实现要点（`rtl/soc/gpcp_soc.sv`）：
- `atop[5:4]==2'b11`（swap）：按字节写回 `w.data`；`2'b01`（add）按字节回退实现；
- 从机完成 RMW 后返回 `atom_old_q`；
- **B 与 R 必须分两个周期返回**（先 R 后 B）。CVA6 的 `wt_axi_adapter` 用 ID 的 `[1]` 位识别
  原子响应，若 B/R 同拍到达，会在同一拍内竞争 ID FIFO 的 pop，导致死锁。

### 1.2 独占访问（LR/SC）

OpenSBI 的 `sbi_atomic_cmpxchg` 在 RVA 下编译成 `lr.w/sc.w`，不是 AMO。若 SoC 不支持
独占访问，HSM 状态机初始化失败，OpenSBI 会打印
`sbi_hsm_hart_start_finish: ERR: The hart is in invalid state [0]` 并 `wfi` 死循环。

实现要点：
- `ar.lock=1`：记录保留地址，并在 R 通道返回 **EXOKAY**；
- `aw.lock=1`：若地址匹配保留项 → B 返回 **EXOKAY**（SC 成功），否则 **OKAY**（SC 失败）；
- 普通写同地址时清除保留。

## 2. CVA6 补丁：CLINT-backed `time` CSR

CVA6 没有实现只读 CSR `time`（0xC01），每次 `rdtime` 都会陷入 M 模式，由 OpenSBI 的
`sbi_emulate_csr_read` → `hpm_allowed` 模拟（实测 PC 轨迹长期停留在 `hpm_allowed`）。
Linux 启动阶段频繁读时间，这会把启动变成陷阱风暴。

`scripts/patch_cva6.py` 做了 4 处修改：

1. `core/csr_regfile.sv`：新增 `mtime_i[63:0]` 输入，`riscv::CSR_TIME` 读取返回它；
2. `core/cva6.sv`：新增 `mtime_i` 端口并接到 `csr_regfile`；
3. `corev_apu/src/ariane.sv`：新增 `mtime_i` 端口并接到 `cva6`；
4. `rtl/soc/gpcp_soc.sv`：把 CLINT 的 `mtime` 接到 `ariane.mtime_i`。

补丁是幂等的，`scripts/build_b3.sh` 会自动调用。

## 3. OpenSBI 的两处原型补丁

见 `patches/opensbi-gpcp.patch`：

1. `firmware/fw_base.S`：单 hart 原型下跳过原子 lottery（`amoswap.w` 换成 `li a6,0`）。
   多 hart 或上板时应删除该补丁。
2. `platform/generic/platform.c`：SoC 直接启动 OpenSBI 时 `a1=0`，`fw_platform_init` 会
   解析空 FDT 失败并进入 `wfi` 死亡循环；补丁在 `arg1==0` 时回退到 `0x81F00000`
   （与 `FW_JUMP_FDT_ADDR` 一致）。

构建参数：

```bash
make -C third_party/opensbi PLATFORM=generic CROSS_COMPILE=riscv64-linux-gnu- \
     FW_JUMP_ADDR=0x80200000 FW_JUMP_FDT_ADDR=0x81F00000 -j8
```

## 4. 镜像布局

| 镜像 | 地址 | 说明 |
|---|---|---|
| `fw_jump.bin` | `0x8000_0000` | OpenSBI，约 280 KB |
| `Image` | `0x8020_0000` | Linux（未压缩），约 13 MB（精简配置） |
| `gpcp.dtb` | `0x81F0_0000` | 设备树，1.6 KB |
| `initramfs.cpio` | `0x8200_0000` | BusyBox + init + `cgra_test`，约 2.2 MB |

启动参数：`console=ttyS0 earlycon=sbi keep_bootcon loglevel=7 rdinit=/init`。

## 5. 设备树要点（`sw/linux/gpcp.dts`）

- `timebase-frequency = 10000000`（mtime 每周期 +1，因此 1 周期 = 100 ns 客机时间）；
- CLINT / PLIC / UART 与 QEMU virt 一致，Linux 用现成驱动；
- UART：`reg-shift = <2>`、`reg-io-width = <4>`（OpenSBI 与 Linux 8250 都按 4 字节步进）；
- `cgra@50000000` 节点（用户态用 `/dev/mem` 访问）。

## 6. 内核与用户态

内核配置（`scripts/build_b3.sh` 里已固化）：`defconfig` 基础上关闭 SMP/模块/网络/存储/
调试，开启 initramfs、devtmpfs、8250 console，`STRICT_DEVMEM=n`，`HZ=100`；
另外必须开启 **`RISCV_SBI_V01` + `SERIAL_EARLYCON_RISCV_SBI`**，
否则 `earlycon=sbi` 在 6.6 内核里没有驱动（早期日志全静默）。

用户态：
- BusyBox 1.36.1，静态链接（`riscv64-linux-gnu-gcc -static`）；
- `/init` 负责 mount proc/sys/devtmpfs、打印 `uname -a`、运行 `/dev/cgra_test`；
- `cgra_test`（`sw/linux/cgra_u.c`）用 `/dev/mem` + `mmap` 访问 `0x5000_0000`，
  跑同一个 4×4 矩阵乘，打印结果后写退出寄存器 `0x4001_0000`。

## 7. 复现

```bash
# 一次性构建（OpenSBI + 内核 + BusyBox + DTB/initramfs）
bash scripts/build_b3.sh

# 仿真启动
make -C sim/soc linux
```

## 8. 两个必须记住的仿真坑

1. **Verilator `--threads >1` 会把这个设计编译错**：现象是 OpenSBI/内核在随机位置
   “挂死”（`_start_hang`、`fw_platform_init` 的 fail 路径等），但同一 RTL 单线程运行正常。
   结论：`sim/soc/Makefile` 固定 `--threads 1`。
2. **`$display` 放在时钟块里 + 多线程**会触发类似的诡异行为，因此 SoC 不再在 RTL 里打印
   调试信息，改为把 `last_pc` 作为 `dbg_pc_o` 端口输出，由 testbench 的 `--trace-pc` 打印。

## 9. 已知限制与后续

1. **仿真速度**：约 70k cycles/s（4 线程），Linux 启动周期数在 10^8 量级，
   完整到用户态需要较长时间；后续可考虑：
   - 更小的内核（`tinyconfig` + 必需驱动）；
   - 关闭周期性调试打印；
   - 用 QEMU 跑软件侧，RTL 只验证关键路径。
2. **PLIC 是桩**：无外部中断源，UART 仅轮询输出（无输入）。
3. **CSR 陷阱**：已通过 CLINT-backed `time` CSR 解决（见 §2）。若仍有
   `cycle/instret` 陷阱，可进一步在 CVA6 中实现对应 CSR 直读。
4. **缓存一致性**：WT cache 下 AMO 走内存，若目标行同时在 dcache 中，后续普通读可能命中旧行；
   OpenSBI 的用法未触发，但 Linux 高并发场景需要补（AMO 后使行失效）。
5. **无 SMP**：单 hart。
6. **OpenSBI 的两处补丁是原型专用**，上板/多 hart 需移除或改为通用实现。
