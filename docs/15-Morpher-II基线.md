# 15. Morpher II 基准线（2026-11-06）

> 目的：给"映射质量"一个外部参照。自研编译器以后做出来，好知道自己的 II 算不算好。
> 环境：`morpher-env:latest`（Ubuntu 20.04 + LLVM 10），源码 `~/morpher`。

## 1. 方法

Morpher 的前端要求：在被映射的循环体里放 `please_map_me();`，且**循环体内所有访存下标
必须对该循环变量仿射**（见 §4 的踩坑）。按此写了三个 kernel（源码见
`cgra/benchmarks/morpher/`，同时拷到 `~/morpher/Morpher_DFG_Generator/benchmarks/morpher_benchmarks/`）。

## 2. 结果

| Kernel | 每迭代工作量 | II | ResMin / RecMin | 映射耗时 | 仿真 |
|---|---|---|---|---|---|
| `array_add`（它自带，作对照） | 1 add | **4** | 2 / 4 | 9 s | 243 匹配 / 0 |
| `gpcp_fir16`（16 tap 内积，标量循环） | 2 加载 + 1 MAC | **4** | 2 / 4 | 18 s | 1975 / 0 |
| `gpcp_gemm4`（polybench 形状） | 1 MAC | **4** | 2 / 4 | 47 s | 1035 / 0 |
| `gpcp_fft_bfly`（16 蝶形/迭代） | 12 加载 + 6 存储 + 算术 | — | — | **> 6 min 未收敛（中止）** | — |

三者 II 全是 4，而且 **RecMin II 也都是 4** —— 这不是巧合，见下。

## 3. 三个结论

### 3.1 RecMinII=4 来自"寄存器-内存回环"，而我们的 acc 正是为消除它设计的

它们的累加形式是 `C[i] += A[i]*B[i]`，编译成 `LD → MUL → ADD → ST`，**ST 到下一次 LD
形成跨迭代依赖**：一次访存往返 + 加法 = 4 拍，于是 `RecMin II = 4`，映射器再快也压不下去。

我们的 ISA 有累加器：`MAC` 直接把乘积累进 `acc`，**回环完全在寄存器里**，不碰 scratchpad。
所以同样一次累加，我们的理论 II 是 **1**，它们是 **4**。

这不是"我们映射得更好"，而是**架构设计差异**被基准线量化出来了：
`docs/02` §5 的 `matmul4x4 = 4 周期` 正是靠这个（4 次 `MAC`，每拍一次）。

### 3.2 前端限制让"空间展开"无法表达（这条比 II 更重要）

我们手写的 kernel 把 16 个抽头**摊开**到 16 个 PE 上（每 PE 一个系数，`h[0]`…`h[15]` 是常量下标）。
Morpher 的 DFG 生成器**直接断言失败**：

```
dfg.cpp: getTransferVariables: Assertion `gep_derivatives.find(ld_ptr) != ...' failed
```

原因：它要求访存地址是循环变量的仿射函数，**常量下标（查表系数、旋转因子表）不被接受**。
后果：只能写"标量循环"形式 → 每次迭代 1 个 MAC → FIR 要 `16 × II=4 = 64` 拍/样本，
而我们手写是 **2 拍/样本**，差 **32 倍**。

也就是说：**差距主要来自前端不能表达空间展开，而不是映射算法差。**

### 3.3 规模上限：>50 节点就不可用

FFT 蝶形（16 蝶形/迭代，DFG 52 节点）映射 **6 分钟未出 II**（array_add 只要 9 秒）。
换 `morpher_light` 想加速，DFG 生成器直接 `Assertion 'false' failed`。
我们的 kernel 里 `fft_stage`、`ldpc_cn` 都在这个量级以上。

## 4. 对我们自研编译器的三条要求（基准线换来的）

1. **必须把累加留在寄存器**：`acc`/`MAC` 的回环不落 scratchpad，否则 II 直接被 4 拍锁死
   —— 这条我们架构上已经满足，但编译器实现时**不能把 `MAC` 退化成 `MUL+ADD+LD/ST`**。
2. **必须支持"常量下标"的查表访问**：`h[t]`、`tw[k]`、`lut[i]` 这类系数表要能进 DFG，
   作为"立即数/常量池"处理。这恰好是 Morpher 的前端做不到、而我们必须做到的一点。
3. **映射器要能处理 50–200 节点**：这条量级 Morpher 要几分钟，我们的目标是秒级，
   否则进不了 `make` 循环。

## 5. 复现

```bash
# 源码
cgra/benchmarks/morpher/gpcp_fir16.c   gpcp_gemm4.c   gpcp_fft_bfly.c
# 拷到 Morpher 的 benchmark 目录后
cd ~/morpher
docker run --rm -v $HOME/morpher:/morpher -w /morpher morpher-env:latest \
  bash -c "python3 -u run_morpher.py morpher_benchmarks/gpcp_fir16/gpcp_fir16.c gpcp_fir16"
# 关键输出：Initial target II / Map Success with II / Simulation Result
```

## 6. 结论

**Morpher 的 II 数字对我们只有一个用途：当作"架构差异的标尺"，而不是"我们要追平的目标"。**
它测出的是它自己那套记忆化累加 + 无累加器架构的极限（II=4），
而我们的架构上限是 II=1。真正值得记住的是 §4 的三条实现约束。
