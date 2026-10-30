# B3：CVA6 跑 Linux（OpenSBI + BusyBox initramfs）

> 目标：在 B2 的 SoC 上补齐 Linux 所需的外设与语义，启动 **OpenSBI → Linux 6.6 → BusyBox**，
> 并在用户态访问 CGRA。
>
> 状态：**OpenSBI 已完整启动**（打印 banner / 平台信息 / Domain 配置），
> **Linux 内核已开始执行**（进入内核虚拟地址、清零 BSS、初始化）。
> 完整到用户态受仿真速度限制（约 70k cycles/s），见 §7。

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

## 2. OpenSBI 的两处原型补丁

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

## 3. 镜像布局

| 镜像 | 地址 | 说明 |
|---|---|---|
| `fw_jump.bin` | `0x8000_0000` | OpenSBI，约 280 KB |
| `Image` | `0x8020_0000` | Linux（未压缩），约 13 MB（精简配置） |
| `gpcp.dtb` | `0x81F0_0000` | 设备树，1.6 KB |
| `initramfs.cpio` | `0x8200_0000` | BusyBox + init + `cgra_test`，约 2.2 MB |

启动参数：`console=ttyS0 earlycon=sbi keep_bootcon loglevel=7 rdinit=/init`。

## 4. 设备树要点（`sw/linux/gpcp.dts`）

- `timebase-frequency = 10000000`（mtime 每周期 +1，因此 1 周期 = 100 ns 客机时间）；
- CLINT / PLIC / UART 与 QEMU virt 一致，Linux 用现成驱动；
- UART：`reg-shift = <2>`、`reg-io-width = <4>`（OpenSBI 与 Linux 8250 都按 4 字节步进）；
- `cgra@50000000` 节点（用户态用 `/dev/mem` 访问）。

## 5. 内核与用户态

内核配置（`scripts/build_b3.sh` 里已固化）：`defconfig` 基础上关闭 SMP/模块/网络/存储/
调试，开启 initramfs、devtmpfs、8250 console，`STRICT_DEVMEM=n`，`HZ=100`。

用户态：
- BusyBox 1.36.1，静态链接（`riscv64-linux-gnu-gcc -static`）；
- `/init` 负责 mount proc/sys/devtmpfs、打印 `uname -a`、运行 `/dev/cgra_test`；
- `cgra_test`（`sw/linux/cgra_u.c`）用 `/dev/mem` + `mmap` 访问 `0x5000_0000`，
  跑同一个 4×4 矩阵乘，打印结果后写退出寄存器 `0x4001_0000`。

## 6. 复现

```bash
# 一次性构建（OpenSBI + 内核 + BusyBox + DTB/initramfs）
bash scripts/build_b3.sh

# 仿真启动
make -C sim/soc linux
```

## 7. 已知限制与后续

1. **仿真速度**：约 70k cycles/s（4 线程），Linux 启动周期数在 10^8 量级，
   完整到用户态需要较长时间；后续可考虑：
   - 更小的内核（`tinyconfig` + 必需驱动）；
   - 关闭周期性调试打印；
   - 用 QEMU 跑软件侧，RTL 只验证关键路径。
2. **PLIC 是桩**：无外部中断源，UART 仅轮询输出（无输入）。
3. **缓存一致性**：WT cache 下 AMO 走内存，若目标行同时在 dcache 中，后续普通读可能命中旧行；
   OpenSBI 的用法未触发，但 Linux 高并发场景需要补（AMO 后使行失效）。
4. **无 SMP**：单 hart。
5. **OpenSBI 的两处补丁是原型专用**，上板/多 hart 需移除或改为通用实现。
