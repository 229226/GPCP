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
| PLIC | 无 | 0x0C00_0000（4 MiB，1 个源 = CGRA 中断，1 个上下文） |
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

### 1.3 PLIC（最小实现，1 源 1 上下文）

`0x0C00_0000` 起 4 MiB（claim 在 `0x0C20_0000`，与 QEMU virt 一致），源 1 = CGRA 中断：

| 偏移 | 内容 |
|---|---|
| `0x0004` | 源 1 的优先级（0 = 关闭） |
| `0x1000` | context 0（hart0 M 模式）使能位图 |
| `0x2000` | context 0 阈值 |
| `0x200000` | claim（读）/ complete（写） |

- 电平触发：源电平（CGRA `irq_o`）在未被服务时置 pending；
- claim 返回源 ID（1）并进入 in-service，complete 后按当前电平重新 pending；
- 到核的连线是 CVA6 的 `irq_i[0]`（**机器外部中断**，cause 11）。注意 CVA6 的
  `irq_i[1]` 是 **S 模式外部中断**，接反会导致 M 模式下永远不陷入；
- `wfi` 唤醒：CVA6 在 `|(mip & mie)` 或 `irq_i[1]` 时解除停顿，所以 M 模式用的
  `irq_i[0]` 需要同时把 `mie.MEIE` 打开才能靠 `wfi` 醒来。

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

## 10. 当前阻塞：内核在 arch_initcall 之后进入 idle（2026-11 实测）

结论先说：**这份 RTL 上的 Linux 能启动、能打印、能跑 initcall，但在 `arch_initcall`
级别之后不再出现任何输出，CPU 停在 `arch_cpu_idle`，`/init` 从未执行。**

证据（都可复跑，命令见 `AGENTS.md` §2）：

| 实验 | 周期预算 | 结果 |
|---|---|---|
| 正常启动 | 40 M | 最后一行 `cpu0: Ratio of byte access...`（内核时间 1.45 s），之后 60 M 周期无输出 |
| 正常启动 | 100 M | 同上，`TIMEOUT`；`tools/check_linux.py` 定级 **stage 2** |
| `--trace-pc 2 M` | 70 M | PC 采样：38–46 M 与 50–64 M 都在 `arch_cpu_idle`（单段最长 14 M 周期），中间出现一次 `scheduler_tick`；20–34 M 落在 OpenSBI 的 `$ecall` 路径（0x8000_xxxx）与 `console_unlock` |
| `initcall_debug` 引导参数 | 60 M | 日志里**没有**出现 `calling ...`，只有命令行回显里有 `initcall_debug` |
| `--trace-pc 0.5 M`（earlycon-only） | 75 M | 40–46 M 仍在真干活（`timekeeping_update`、`kernfs_activate_one`、`rwsem_read_trylock`、`hrtimer_interrupt`、`kobject_uevent_env`）；**46.5 M 起连续 28.5 M 周期全是 `arch_cpu_idle`** |
| 160 M 长跑（earlycon-only） | 160 M | 最后输出仍在 1.98 s（50 M 处），之后 110 M 周期零输出；`stage 2` |

从 `vmlinux` 里直接 dump `__initcall3` 表（8 字节/项，`__initcall3_start=0x80613478`，
`__initcall4_start=0x806134d8`）并按链接顺序命名，最后两项是：

```
 9  serial_base_init
11  of_platform_default_populate_init
```

即 `subsys_initcall` 级别跑完了（最后一项就是"按 DT 建平台设备"）。因此：

- `hrtimer_interrupt` / `scheduler_tick` 正常 → 定时器与中断可用；
- 46.5 M 后纯 idle → **所有可运行任务都阻塞**，最可能是某个设备 probe 永久等待；
- 我们 DT 里唯一"驱动明明编进来了（`CONFIG_SERIAL_8250=y` + `CONSOLE=y`）却从头到尾
  没有任何 8250 打印"的设备就是 `ns16550a`；且它的 `interrupts = <1>` 与 PLIC 的
  `riscv,ndev = <1>`（CGRA 的源）冲突，启动时还有 `Fixed dependency cycle(s) with
  /soc/plic@c000000` 警告。

解读：

1. `scheduler_tick` 出现过 → 定时器中断（CLINT `mtip`）是工作的，`jiffies` 会走；
2. 之后长时间 idle → 没有可运行任务，即 `kernel_init` 阻塞在某个等待上（`/init` 未 exec）；
3. `initcall_debug` 没打出 `calling` → 该 `core_param` 在本内核上没生效（待查：`parse_args`
   阶段是否被前面的参数吞掉，或该 6.6.52 配置里打印走的是 trace 路径）。

### 判定实验结果（每个 60 M 周期，要求 stage 3）

| # | 变体 | 最后内核输出 | 结论 |
|---|---|---|---|
| 基线 | earlycon-only | 1.983 s `pnp: PnP ACPI: disabled` | 46.5 M 起纯 idle |
| A | `initcall_blacklist=serial8250_init` | 2.006 s `pnp: ... disabled` | **不是** 8250 注册的锅 |
| B | serial 节点 disabled（sed 写坏，节点被挪出 `/soc`） | 连 OpenSBI 都没打印 | 实验作废（DT 无效） |
| C | 去掉 UART `interrupts = <1>` | 1.980 s `pnp: ... disabled` | **不是** UART 中断号冲突 |
| D | `fw_devlink=off deferred_probe_timeout=5` | **0.349 s** `devtmpfs: initialized` | 反而更早停：设备依赖链一关，某个 probe 更早开始并阻塞 |
| E | `deferred_probe_timeout=5` | 1.818 s `Fixed dependency cycle(s)` | 略早停 |
| F | serial `status = "disabled"`（正确放在 `/soc` 内） | 见下一轮日志 `build/linux_exp_f.log` | 判定"是否存在设备 probe 阻塞" |

由此收敛出的判断：**问题不是一个"等超时"的 deferred probe**（D/E 都没让引导越过 2.0 s），
而是某个**设备 probe 在等一个永远不会来的事件**；`fw_devlink=off` 让它更早开始探测、
于是更早卡住（0.349 s），这与"serial 探测"高度吻合——A 只黑名单了 `serial8250_init`，
但 8250 驱动还有 `univ8250_console_init` 等注册路径，未必都被跳过。

后续若 F 仍卡：下一步是**重编内核**（把 `CONFIG_INITRAMFS_SOURCE` 指向现有
`build/linux/initramfs` 做成内置 initramfs，绕开 initrd 交接），并用
`CONFIG_DYNAMIC_DEBUG`/`dyndbg` 对 `drivers/tty/serial/*` 打开调试打印，
把 probe 的最后一步打出来。

下一步（按代价从低到高）：

- 去掉 `console=ttyS0`、只留 `earlycon=sbi` 跑一次：区分"内核真的停住"还是"8250 接管后
  控制台失效"（后者会让日志看起来像停住）；
- 用 `initcall_blacklist=<fn>` 逐个屏蔽"可能等待外部事件"的 initcall；
- 若仍无解，重编内核（`CONFIG_DEBUG_KERNEL` 已 y，但可加 `CONFIG_DEBUG_INFO`/`KALLSYMS`
  以便 `%pS` 打印函数名），或改用 `tinyconfig` 把 initcall 数量降一个量级；
- 软件侧并行：用 QEMU 跑同一份 initramfs 验证用户态程序（`cgra_test` 的正确性不依赖本 RTL）。

> 注：RTL 里还留着 32 条上限的 `[trap]` 调试打印（`gpcp_soc.sv` 805 行），它会与
> `earlycon=sbi` 的逐字符 ecall 交错，把控制台输出切碎（`tools/check_linux.py` 已做去噪，
> 但新的日志分析脚本要记得处理）。
