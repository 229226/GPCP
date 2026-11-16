#!/usr/bin/env python3
"""纯 RTL 旋转表单测：不经过 CVA6/协议链，直接跑 CGRA 台架。

覆盖四件事（正好对应这次踩过的坑）：
  1. LD 带旋转：addr = (word + rot[bank]) mod ZC；
  2. ST 带旋转：写回落点同样按表旋转；
  3. invalid：表项 bit5=1 的 bank 读回 0；
  4. keep：表项 bit6=1 的 bank 写被丢弃；
  5. rot=0 时地址不被 ZC 取模改写。

用法：python3 sim/cgra/rot_test.py
依赖：sim/cgra/build/Vcgra_axi_lite（make -C sim/cgra）
"""
import os
import random
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "cgra", "tools"))
from cgra import Program, spad_idx  # noqa: E402
import desc64                        # noqa: E402  描述符字段（tools/gen_cgra_desc.py 生成）

BIN = os.path.join(HERE, "build", "Vcgra_axi_lite")
OUT = os.path.join(HERE, "build")
ZC = 20
SENTINEL = 32767
ROT_FLAG = 0x8000
ROT_ROT = 0x07
ROT_ROT2 = 0x05
ROT_INVALID = 0x20
ROT_KEEP = 0x40

# spad 字节地址（主机窗口）= 0xA000 + (bank*32 + word)*4
def spad_addr(bank, word):
    return 0xA000 + (bank * 32 + word) * 4


def build_inputs():
    prog = Program()
    for p in range(16):
        # 0: 读 bank0 原值（不旋转）——用来验证 rot=0 不被取模改写
        prog.set(0, p, "ld", dst=0, imm=spad_idx(0, p))
        # 1: 读 bank1，rot=7
        prog.set(1, p, "ld", dst=1, imm=spad_idx(1, p) | ROT_FLAG)
        # 2: 写 bank2，rot=5
        prog.set(2, p, "st", a=1, imm=spad_idx(2, p) | ROT_FLAG)
        # 3: 读 bank3，invalid → 必须得到 0
        prog.set(3, p, "ld", dst=2, imm=spad_idx(3, p) | ROT_FLAG)
        # 4: 写 bank3，keep → 必须被丢弃
        prog.set(4, p, "st", a=2, imm=spad_idx(3, p) | ROT_FLAG)
        # 5: 写 bank4，rot=0 → 原地址写入
        prog.set(5, p, "st", a=0, imm=spad_idx(4, p) | ROT_FLAG)

    os.makedirs(OUT, exist_ok=True)
    prog_path = os.path.join(OUT, "rot_prog.hex")
    with open(prog_path, "w") as f:
        for w in prog.build(6):
            f.write("0x%08x\n" % w)

    pre = []
    for p in range(16):
        pre.append((spad_addr(0, p), 0x100 + p))
        pre.append((spad_addr(1, p), 0x200 + p))
        pre.append((spad_addr(2, p), 0xFFFFFFFF))
        pre.append((spad_addr(3, p), 0x300 + p))
        pre.append((spad_addr(4, p), 0xFFFFFFFF))
    for p in range(16, 20):
        pre.append((spad_addr(0, p), 0x100 + p))
        pre.append((spad_addr(1, p), 0x200 + p))
        pre.append((spad_addr(2, p), 0xFFFFFFFF))
        pre.append((spad_addr(3, p), 0x300 + p))
        pre.append((spad_addr(4, p), 0xFFFFFFFF))
    # 表：bank0=0，bank1=7，bank2=5，bank3=invalid|keep，bank4=0
    pre += [(0x0080 + 0 * 4, 0x00), (0x0080 + 1 * 4, ROT_ROT),
            (0x0080 + 2 * 4, ROT_ROT2), (0x0080 + 3 * 4, ROT_INVALID | ROT_KEEP),
            (0x0080 + 4 * 4, 0x00), (0x006C, ZC)]
    pre_path = os.path.join(OUT, "rot_preload.txt")
    with open(pre_path, "w") as f:
        for a, v in pre:
            f.write("%04x %08x\n" % (a, v))
    return prog_path, pre_path


def run(tag, prog_path, pre_path, cycles):
    dump = os.path.join(OUT, tag + "_dump.txt")
    cmd = [BIN, "--prog", prog_path, "--cycles", str(cycles),
           "--preload", pre_path, "--spad-dump", "0xA000", "832",
           "--dump", dump]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                       env={k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"})
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        sys.exit("台架运行失败，returncode=%d" % r.returncode)
    spad = {}
    for line in open(dump):
        m = re.match(r"spad ([0-9a-fA-F]{4}) 0x([0-9a-fA-F]{8})", line)
        if m:
            spad[int(m.group(1), 16)] = int(m.group(2), 16)
    return spad


def check(spad):
    bad = 0

    def want(addr, v, what):
        nonlocal bad
        got = spad.get(addr)
        if got != v:
            if bad < 12:
                print("  FAIL %-28s addr=0x%04x got=0x%08x want=0x%08x"
                      % (what, addr, got if got is not None else 0xDEADBEEF, v))
            bad += 1

    for p in range(20):
        want(spad_addr(0, p), 0x100 + p, "bank0 原值(rot=0)")
        want(spad_addr(1, p), 0x200 + p, "bank1 原值(只读)")
        want(spad_addr(3, p), 0x300 + p, "bank3 invalid+keep 不变")
    for p in range(16):
        want(spad_addr(4, p), 0x100 + p, "bank4 rot=0 直写")
        src = 0x200 + ((p + ROT_ROT) % ZC)
        want(spad_addr(2, (p + ROT_ROT2) % ZC), src, "bank2 旋转写")
    for w in range(20):
        if w not in [(p + ROT_ROT2) % ZC for p in range(16)]:
            want(spad_addr(2, w), 0xFFFFFFFF, "bank2 未写位置")
        if w >= 16:
            want(spad_addr(4, w), 0xFFFFFFFF, "bank4 未写位置")
    return bad


def build_cn_inputs():
    """20 提升行 × 10 边；bank 8/9 当哨兵边（keep=1），用于同时验证 m' 与 keep。"""
    import rule_map as rm
    mc = rm.schedule_cn(writeback=True)
    prog_path = os.path.join(OUT, "cn_prog.hex")
    with open(prog_path, "w") as f:
        for w in mc.build():
            f.write("0x%08x\n" % w)

    rnd = random.Random(7)
    vals = {}
    pre = []
    real = 5
    for w in range(20):
        for k in range(10):
            if k < real:
                v = rnd.randint(-5000, 5000)
            else:
                v = SENTINEL
            vals[(k, w)] = v
        pre.append((spad_addr(30, w), SENTINEL))
    # 精确复现协议 row10/word3：两个 0、123、124、-124
    for k, v in enumerate([0, 0, 123, 124, -124]):
        vals[(k, 3)] = v
    for (k, w), v in vals.items():
        pre.append((spad_addr(k, w), v & 0xFFFFFFFF))
    for k in range(real):
        pre.append((0x0080 + k * 4, 0))
    for k in range(real, 10):
        pre.append((0x0080 + k * 4, ROT_KEEP))
    for k in (10, 11, 12, 30):
        pre.append((0x0080 + k * 4, 0))
    pre.append((0x006C, ZC))
    pre_path = os.path.join(OUT, "cn_preload.txt")
    with open(pre_path, "w") as f:
        for a, v in pre:
            f.write("%04x %08x\n" % (a, v))
    return prog_path, pre_path, mc.cycles, vals


def check_cn(spad, vals):
    bad = 0
    real = 5

    def want(addr, v, what):
        nonlocal bad
        got = spad.get(addr)
        if got != v:
            if bad < 12:
                print("  FAIL %-28s addr=0x%04x got=0x%08x want=0x%08x"
                      % (what, addr, got if got is not None else 0xDEADBEEF, v))
            bad += 1

    for w in range(20):
        col = [vals[(k, w)] for k in range(10)]
        mags = sorted(abs(x) for x in col)
        m1, m2 = mags[0], mags[1]
        sg = -1 if sum(1 for x in col if x < 0) % 2 else 0
        for k in range(10):
            if k >= real:                    # keep：哨兵边不被 m' 覆盖
                want(spad_addr(k, w), SENTINEL, "CN keep 哨兵边")
                continue
            x = col[k]
            am = -1 if x < 0 else 0
            mag = m2 if abs(x) == m1 else m1
            nm = am ^ sg
            out = ((mag ^ nm) - nm) & 0xFFFFFFFF
            want(spad_addr(k, w), out, "CN m' 回写")
    return bad


def build_bn_inputs():
    """一个变量块：20 条真实边（bank 0..19）+ 4 个无效槽（bank 20..23）+ LLR(24)+硬判(25)。

    每条边的旋转量 sh[e] 故意取非零，用来验证 BN 的负向旋转：
    变量索引 i 读的是 CN 索引 (i - sh) mod ZC。
    """
    import rule_map as rm
    mc = rm.schedule_bn_split(rows=20, rot=True)
    prog_path = os.path.join(OUT, "bn_prog.hex")
    with open(prog_path, "w") as f:
        for w in mc.build():
            f.write("0x%08x\n" % w)

    n = 20
    sh = [(e * 3 + 1) % ZC for e in range(n)]
    rnd = random.Random(11)
    val = [[0] * ZC for _ in range(24)]
    llr = [rnd.randint(-3000, 3000) for _ in range(ZC)]
    pre = []
    for e in range(24):
        for w in range(ZC):
            v = rnd.randint(-3000, 3000) if e < n else 0xDEADBEEF
            val[e][w] = v
            pre.append((spad_addr(e, w), v & 0xFFFFFFFF))
    for w in range(ZC):
        pre.append((spad_addr(24, w), llr[w] & 0xFFFFFFFF))
        pre.append((spad_addr(25, w), 0xDEADBEEF))
    for e in range(n):
        pre.append((0x0080 + e * 4, (ZC - sh[e]) % ZC))
    for e in range(n, 24):
        pre.append((0x0080 + e * 4, ROT_INVALID))
    pre.append((0x0080 + 24 * 4, 0))
    pre.append((0x0080 + 25 * 4, 0))
    pre.append((0x006C, ZC))
    pre_path = os.path.join(OUT, "bn_preload.txt")
    with open(pre_path, "w") as f:
        for a, v in pre:
            f.write("%04x %08x\n" % (a, v))
    return prog_path, pre_path, mc.cycles, (n, sh, val, llr)


def check_bn(spad, case):
    n, sh, val, llr = case
    bad = 0

    def want(addr, v, what):
        nonlocal bad
        got = spad.get(addr)
        if got != v:
            if bad < 12:
                print("  FAIL %-28s addr=0x%04x got=0x%08x want=0x%08x"
                      % (what, addr, got if got is not None else 0xDEADBEEF, v))
            bad += 1

    for w in range(ZC):
        want(spad_addr(24, w), llr[w] & 0xFFFFFFFF, "BN LLR 不变")
    for r in range(ZC):
        reads = []
        for e in range(24):
            if e < n:
                reads.append(val[e][(r - sh[e]) % ZC])
            else:
                reads.append(0)          # invalid 槽读 0
        v = llr[r] + sum(reads)
        for e in range(n):
            word = (r - sh[e]) % ZC
            want(spad_addr(e, word), (v - reads[e]) & 0xFFFFFFFF, "BN 旋转回写")
        for e in range(n, 24):
            want(spad_addr(e, r), v & 0xFFFFFFFF, "BN 无效槽写 v")
        want(spad_addr(25, r), (-1 if v < 0 else 0) & 0xFFFFFFFF, "BN 硬判")
    return bad


def run_desc(tag, prog_path, pre_path, cycles, ring, mem):
    """走真实 64B 描述符环：表装载 + DMA 搬入 + 复合(阵列) + DMA 搬出。"""
    dump = os.path.join(OUT, tag + "_dump.txt")
    dma_dump = os.path.join(OUT, tag + "_dma.txt")
    ring_path = os.path.join(OUT, tag + "_ring.txt")
    mem_path = os.path.join(OUT, tag + "_mem.txt")
    with open(ring_path, "w") as f:
        for d in ring:
            f.write(" ".join("0x%08x" % x for x in d) + "\n")
    with open(mem_path, "w") as f:
        for v in mem:
            f.write("0x%08x\n" % v)
    cmd = [BIN, "--prog", prog_path, "--cycles", str(cycles),
           "--preload", pre_path, "--spad-dump", "0xA000", "832",
           "--ring-desc", ring_path, "--ring-base", "0x200", "--ring-size", "16",
           "--desc64", "--no-start", "--dma-mem", mem_path,
           "--dma-dump", dma_dump, "--dump", dump, "--watch-writes"]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                       env={k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"})
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        sys.exit("台架运行失败，returncode=%d" % r.returncode)
    spad = {}
    for line in open(dump):
        m = re.match(r"spad ([0-9a-fA-F]{4}) 0x([0-9a-fA-F]{8})", line)
        if m:
            spad[int(m.group(1), 16)] = int(m.group(2), 16)
    dma = [int(x, 16) for x in open(dma_dump).read().split()]
    return spad, dma


def build_desc_cn():
    import rule_map as rm
    mc = rm.schedule_cn(writeback=True)
    prog_path = os.path.join(OUT, "desc_cn_prog.hex")
    with open(prog_path, "w") as f:
        for w in mc.build():
            f.write("0x%08x\n" % w)

    rnd = random.Random(13)
    real = 5
    msg = []
    for k in range(10):
        for w in range(20):
            msg.append(rnd.randint(-5000, 5000) if k < real else SENTINEL)
    # 复现协议里 row10/word3 的真实数据：两个 0 + 123/124/675
    for k, v in enumerate([0, 0, 123, 124, 675]):
        msg[k * 20 + 3] = v
    mem = [0] * 1024
    table_addr, msg_addr = 0x000, 0x400
    for i in range(32):
        mem[table_addr // 4 + i] = 0
    for k in range(real, 10):
        mem[table_addr // 4 + k] = ROT_KEEP
    for i in range(200):
        mem[msg_addr // 4 + i] = msg[i] & 0xFFFFFFFF

    t = [0] * 16
    t[0] = table_addr
    t[1] = 1 << 19
    t[2] = 32
    d = [0] * 16
    d[0] = msg_addr
    d[1] = 0
    d[2] = 200
    d[3] = 32 | (20 << 16)
    d[6] = mc.cycles | (1 << 31)
    d[8] = 200
    d[9] = 1
    d[10] = msg_addr
    d[12] = 1
    pre = [(spad_addr(30, w), SENTINEL) for w in range(20)]
    pre.append((0x006C, ZC))
    pre_path = os.path.join(OUT, "desc_cn_preload.txt")
    with open(pre_path, "w") as f:
        for a, v in pre:
            f.write("%04x %08x\n" % (a, v))
    return prog_path, pre_path, mc.cycles, [t, d], mem, msg


def check_desc_cn(dma, msg):
    bad = 0
    base = 0x400 // 4
    real = 5
    for w in range(20):
        col = [msg[k * 20 + w] for k in range(real)]
        mags = sorted(abs(x) for x in col)
        m1, m2 = mags[0], mags[1]
        sg = -1 if sum(1 for x in col if x < 0) % 2 else 0
        for k in range(10):
            if k >= real:
                want = SENTINEL
            else:
                x = col[k]
                am = -1 if x < 0 else 0
                mag = m2 if abs(x) == m1 else m1
                nm = am ^ sg
                want = ((mag ^ nm) - nm) & 0xFFFFFFFF
            got = dma[base + k * 20 + w]
            if got != want:
                if bad < 12:
                    print("  FAIL CN desc k=%d w=%d got=0x%08x want=0x%08x"
                          % (k, w, got, want))
                bad += 1
    return bad


def build_desc_bn():
    import rule_map as rm
    mc = rm.schedule_bn_split(rows=20, rot=True)
    prog_path = os.path.join(OUT, "desc_bn_prog.hex")
    with open(prog_path, "w") as f:
        for w in mc.build():
            f.write("0x%08x\n" % w)

    n = 6
    sh = [(e * 3 + 1) % ZC for e in range(n)]
    rnd = random.Random(17)
    edges = [[rnd.randint(-3000, 3000) for _ in range(ZC)] for _ in range(n)]
    llr = [rnd.randint(-3000, 3000) for _ in range(ZC)]
    mem = [0] * 1024
    table_addr, edge_addr, llr_addr, hard_addr = 0x000, 0x600, 0x800, 0x880
    tab = [0] * 32
    for e in range(n):
        tab[e] = (ZC - sh[e]) % ZC
    for e in range(n, 24):
        tab[e] = ROT_INVALID
    for i in range(32):
        mem[table_addr // 4 + i] = tab[i]
    for e in range(n):
        for w in range(ZC):
            mem[edge_addr // 4 + e * 20 + w] = edges[e][w] & 0xFFFFFFFF
    for w in range(ZC):
        mem[llr_addr // 4 + w] = llr[w] & 0xFFFFFFFF

    ring = []
    t = [0] * 16
    t[0] = table_addr
    t[1] = 1 << 19
    t[2] = 32
    ring.append(t)
    d = [0] * 16
    d[0] = llr_addr
    d[1] = spad_idx(24, 0)
    d[2] = ZC
    ring.append(d)
    for e in range(n - 1):
        d = [0] * 16
        d[0] = edge_addr + e * 80
        d[1] = spad_idx(e, 0)
        d[2] = ZC
        ring.append(d)
    d = [0] * 16
    d[0] = edge_addr + (n - 1) * 80
    d[1] = spad_idx(n - 1, 0)
    d[2] = ZC
    d[6] = mc.cycles | (1 << 31)
    d[8] = ZC
    d[9] = 1
    d[10] = edge_addr
    d[12] = 1
    ring.append(d)
    for e in range(1, n):
        d = [0] * 16
        d[0] = edge_addr + e * 80
        d[1] = spad_idx(e, 0) | (1 << 17)
        d[2] = ZC
        ring.append(d)
    d = [0] * 16
    d[0] = hard_addr
    d[1] = spad_idx(25, 0) | (1 << 17)
    d[2] = ZC
    ring.append(d)

    pre = [(0x006C, ZC)]
    pre_path = os.path.join(OUT, "desc_bn_preload.txt")
    with open(pre_path, "w") as f:
        for a, v in pre:
            f.write("%04x %08x\n" % (a, v))
    return (prog_path, pre_path, mc.cycles, ring, mem,
            (n, sh, edges, llr, edge_addr, llr_addr, hard_addr))


def check_desc_bn(dma, case):
    n, sh, edges, llr, edge_addr, llr_addr, hard_addr = case
    bad = 0

    def want(addr, v, what):
        nonlocal bad
        got = dma[addr // 4]
        if got != v:
            if bad < 12:
                print("  FAIL %-24s addr=0x%03x got=0x%08x want=0x%08x"
                      % (what, addr, got, v))
            bad += 1

    for w in range(ZC):
        want(llr_addr + w * 4, llr[w] & 0xFFFFFFFF, "BN desc LLR 不变")
    for r in range(ZC):
        reads = [edges[e][(r - sh[e]) % ZC] for e in range(n)]
        v = llr[r] + sum(reads)
        for e in range(n):
            word = (r - sh[e]) % ZC
            want(edge_addr + e * 80 + word * 4,
                 (v - reads[e]) & 0xFFFFFFFF, "BN desc 旋转回写")
        want(hard_addr + r * 4, (-1 if v < 0 else 0) & 0xFFFFFFFF, "BN desc 硬判")
    return bad


def build_dma_probe(src=0x400, spad=0, length=20, stride=0, row=0):
    """最小 DMA 落点探针：一条输入描述符 + 1 拍 NOP 程序。"""
    p = Program()
    prog_path = os.path.join(OUT, "probe_prog.hex")
    with open(prog_path, "w") as f:
        for w in p.build(1):
            f.write("0x%08x\n" % w)
    vals = [0x1000 + i for i in range(200)]
    mem = [0] * 1024
    for i in range(200):
        mem[src // 4 + i] = vals[i]
    d = [0] * 16
    d[0] = src
    d[1] = spad
    d[2] = length
    d[3] = stride | (row << 16)
    pre_path = os.path.join(OUT, "probe_preload.txt")
    with open(pre_path, "w") as f:
        f.write("%04x %08x\n" % (0x006C, ZC))
    return prog_path, pre_path, [d], mem, vals


def check_dma_probe(spad, vals, length):
    bad = 0
    found = {}
    for b in range(16):
        for w in range(32):
            v = spad.get(spad_addr(b, w))
            if v in (0x1000, 0x1001, 0x1002):
                found.setdefault(v, (b, w))
    if found.get(0x1000) != (0, 0) or found.get(0x1001) != (0, 1) or found.get(0x1002) != (0, 2):
        print("  probe 落点：0x1000→%s 0x1001→%s 0x1002→%s（期望 (0,0)/(0,1)/(0,2)）"
              % (found.get(0x1000), found.get(0x1001), found.get(0x1002)))
        bad += 1
    for i in range(length):
        b, w = divmod(i, 32)
        got = spad.get(spad_addr(b, w))
        want = vals[i]
        if got != want:
            if bad < 12:
                print("  FAIL probe i=%d bank=%d word=%d got=0x%08x want=0x%08x"
                      % (i, b, w, got if got is not None else 0xDEADBEEF, want))
            bad += 1
    return bad


def build_desc_cn2(msg_addrs=(0x400, 0x800), mem_size=1024):
    """两行 CN 环：模拟协议里 row9→row10 的连续描述符交互。"""
    import rule_map as rm
    mc = rm.schedule_cn(writeback=True)
    prog_path = os.path.join(OUT, "desc_cn2_prog.hex")
    with open(prog_path, "w") as f:
        for w in mc.build():
            f.write("0x%08x\n" % w)
    rnd = random.Random(13)
    real = 5

    def rowdata():
        m = []
        for k in range(10):
            for w in range(20):
                m.append(rnd.randint(-5000, 5000) if k < real else SENTINEL)
        for k, v in enumerate([0, 0, 123, 124, 675]):
            m[k * 20 + 3] = v
        return m

    rows = [rowdata(), rowdata()]
    mem = [0] * mem_size
    for tab in (0x000, 0x080):
        for i in range(32):
            mem[tab // 4 + i] = 0
        for k in range(real, 10):
            mem[tab // 4 + k] = ROT_KEEP
    for msg_addr, data in zip(msg_addrs, rows):
        for i in range(200):
            mem[msg_addr // 4 + i] = data[i] & 0xFFFFFFFF
    ring = []
    for tab, msg_addr in zip((0x000, 0x080), msg_addrs):
        t = [0] * 16
        t[0] = tab
        t[1] = 1 << 19
        t[2] = 32
        d = [0] * 16
        d[0] = msg_addr
        d[1] = 0
        d[2] = 200
        d[3] = 32 | (20 << 16)
        d[6] = mc.cycles | (1 << 31)
        d[8] = 200
        d[9] = 1
        d[10] = msg_addr
        d[12] = 1
        ring += [t, d]
    pre = [(spad_addr(30, w), SENTINEL) for w in range(20)]
    pre.append((0x006C, ZC))
    pre_path = os.path.join(OUT, "desc_cn2_preload.txt")
    with open(pre_path, "w") as f:
        for a, v in pre:
            f.write("%04x %08x\n" % (a, v))
    return prog_path, pre_path, mc.cycles, ring, mem, rows


def check_desc_cn2(dma, rows, msg_addrs=(0x400, 0x800)):
    bad = 0
    real = 5
    for ri, (msg_addr, data) in enumerate(zip(msg_addrs, rows)):
        base = msg_addr // 4
        for w in range(20):
            col = [data[k * 20 + w] for k in range(real)]
            mags = sorted(abs(x) for x in col)
            m1, m2 = mags[0], mags[1]
            sg = -1 if sum(1 for x in col if x < 0) % 2 else 0
            for k in range(10):
                if k >= real:
                    want = SENTINEL
                else:
                    x = col[k]
                    am = -1 if x < 0 else 0
                    mag = m2 if abs(x) == m1 else m1
                    nm = am ^ sg
                    want = ((mag ^ nm) - nm) & 0xFFFFFFFF
                got = dma[base + k * 20 + w]
                if got != want:
                    if bad < 12:
                        print("  FAIL CN2 row=%d k=%d w=%d got=0x%08x want=0x%08x"
                              % (ri, k, w, got, want))
                    bad += 1
    return bad


def build_desc_cn4k(msg_addr=0xF00):
    """一行 CN，msg 放在跨 4 KiB 的地址（0xF00=整 32-beat，0xF40=部分 burst）。"""
    import rule_map as rm
    mc = rm.schedule_cn(writeback=True)
    prog_path = os.path.join(OUT, "desc_cn4k_prog.hex")
    with open(prog_path, "w") as f:
        for w in mc.build():
            f.write("0x%08x\n" % w)
    rnd = random.Random(13)
    real = 5
    msg = []
    for k in range(10):
        for w in range(20):
            msg.append(rnd.randint(-5000, 5000) if k < real else SENTINEL)
    for k, v in enumerate([0, 0, 123, 124, 675]):
        msg[k * 20 + 3] = v

    mem = [0] * 2048
    table_addr = 0x000
    for k in range(real, 10):
        mem[table_addr // 4 + k] = ROT_KEEP
    for i in range(200):
        mem[msg_addr // 4 + i] = msg[i] & 0xFFFFFFFF
    t = [0] * 16
    t[0] = table_addr
    t[1] = 1 << 19
    t[2] = 32
    d = [0] * 16
    d[0] = msg_addr
    d[1] = 0
    d[2] = 200
    d[3] = 32 | (20 << 16)
    d[6] = mc.cycles | (1 << 31)
    d[8] = 200
    d[9] = 1
    d[10] = msg_addr
    d[12] = 1
    pre = [(spad_addr(30, w), SENTINEL) for w in range(20)]
    pre.append((0x006C, ZC))
    pre_path = os.path.join(OUT, "desc_cn4k_preload.txt")
    with open(pre_path, "w") as f:
        for a, v in pre:
            f.write("%04x %08x\n" % (a, v))
    return prog_path, pre_path, mc.cycles, [t, d], mem, msg


def check_desc_cn4k(dma, msg, msg_addr=0xF00):
    bad = 0
    real, base = 5, msg_addr // 4
    for w in range(20):
        col = [msg[k * 20 + w] for k in range(real)]
        mags = sorted(abs(x) for x in col)
        m1, m2 = mags[0], mags[1]
        sg = -1 if sum(1 for x in col if x < 0) % 2 else 0
        for k in range(10):
            if k >= real:
                want = SENTINEL
            else:
                x = col[k]
                am = -1 if x < 0 else 0
                mag = m2 if abs(x) == m1 else m1
                nm = am ^ sg
                want = ((mag ^ nm) - nm) & 0xFFFFFFFF
            got = dma[base + k * 20 + w]
            if got != want:
                if bad < 12:
                    print("  FAIL CN4k k=%d w=%d got=0x%08x want=0x%08x"
                          % (k, w, got, want))
                bad += 1
    return bad


def _burst_plan(start, ln, beats=32, buggy=False):
    """逐段推进 RTL 的突发规划，返回最终写到的字数（旧实现会超过 ln）。

    buggy=True 复刻旧实现：dma_4k < DMA_BEATS 时直接拿"到 4 KiB 边界的拍数"
    当突发长度（而不是取 min），传输只剩 4 拍时会多写 24 拍。
    """
    idx = 0
    for _ in range(4096):
        if idx >= ln:
            break
        left = ln - idx
        nb_raw = beats if left >= 2 * beats else (left + 1) // 2
        odd_full = (left & 1) if left < 2 * beats else 0
        lo = (start + idx * 4) & 0xFFF
        to4k = (4096 - lo) >> 3
        nb = (nb_raw if to4k >= beats else max(to4k, 1)) if buggy \
            else min(nb_raw, max(to4k, 1))
        idx += 2 * nb - (odd_full if nb == nb_raw else 0)
    return idx


def build_desc_sweep(ncase=12):
    """4 KiB 尾部扫描：挑出"旧实现会越界"的 (start, len)，每组放独立页尾。

    越界会写到下一页开头（那里没有声明窗口），由台架的 --watch-writes 抓。
    """
    cand = []
    for end_off in range(0xE00, 0x1000, 0x20):        # 传输结束落在页尾
        for ln in (8, 16, 24, 32, 48, 64, 96, 128, 160, 200, 240, 260):
            lo = end_off - ln * 4
            if lo < 0x400 or lo >= 0x1000:
                continue
            if _burst_plan(lo, ln) != ln:             # 新实现必须恰好写完
                continue
            if _burst_plan(lo, ln, buggy=True) <= ln:  # 旧实现要越界
                continue
            cand.append((lo, ln))
    cand = sorted(set(cand))
    step = max(1, len(cand) // ncase)
    cases = cand[::step][:ncase]

    src = 0x0800
    maxlen = max(ln for _lo, ln in cases)
    pattern = [(0x1000 + i * 7) & 0xFFFFFFFF for i in range(maxlen)]
    mem = [0] * 1024
    for i, v in enumerate(pattern):
        mem[src // 4 + i] = v

    ring = []
    for i, (lo, ln) in enumerate(cases):
        ring.append(desc64.make(src=src, in_len=ln, out_len=ln, out_en=1,
                                out_dst=0x2000 + i * 0x1000 + lo, rep=1))

    prog_path = os.path.join(OUT, "desc_sweep_prog.hex")
    with open(prog_path, "w") as f:
        for _ in range(16):
            f.write("0x00000000\n")
    pre_path = os.path.join(OUT, "desc_sweep_preload.txt")
    open(pre_path, "w").close()
    return prog_path, pre_path, 1, ring, mem, cases, pattern


def check_desc_sweep(dma, cases, pattern):
    bad = 0
    for i, (lo, ln) in enumerate(cases):
        base = (0x2000 + i * 0x1000 + lo) // 4
        for j in range(ln):
            if dma[base + j] != pattern[j]:
                if bad < 8:
                    print("  FAIL sweep[%d] dst=0x%x +%d got=0x%08x want=0x%08x"
                          % (i, base * 4, j, dma[base + j], pattern[j]))
                bad += 1
    return bad


def build_desc_cn_ring84():
    """84 条 CN 整环（42 行 × [表, 复合]），复用同一 msg/表以适配 8 KiB 模型。"""
    import rule_map as rm
    mc = rm.schedule_cn(writeback=True)
    prog_path = os.path.join(OUT, "desc_cn_ring84_prog.hex")
    with open(prog_path, "w") as f:
        for w in mc.build():
            f.write("0x%08x\n" % w)
    msg_addr = 0x1800
    mem = [0] * (msg_addr // 4 + 200)
    for i in range(200):
        mem[msg_addr // 4 + i] = (i * 37 - 4000) & 0xFFFFFFFF
    ring = []
    for _ in range(42):
        t = [0] * 16
        t[0] = 0x000
        t[1] = 1 << 19
        t[2] = 32
        d = [0] * 16
        d[0] = msg_addr
        d[2] = 200
        d[3] = 32 | (20 << 16)
        d[6] = mc.cycles | (1 << 31)
        d[8] = 200
        d[9] = 1
        d[10] = msg_addr
        d[12] = 1
        ring += [t, d]
    pre = [(spad_addr(30, w), SENTINEL) for w in range(20)]
    pre.append((0x006C, ZC))
    pre_path = os.path.join(OUT, "desc_cn_ring84_preload.txt")
    with open(pre_path, "w") as f:
        for a, v in pre:
            f.write("%04x %08x\n" % (a, v))
    return prog_path, pre_path, mc.cycles, ring, mem


def run_desc_ring84(tag, prog_path, pre_path, cycles, ring, mem):
    dump = os.path.join(OUT, tag + "_dump.txt")
    dma_dump = os.path.join(OUT, tag + "_dma.txt")
    ring_path = os.path.join(OUT, tag + "_ring.txt")
    mem_path = os.path.join(OUT, tag + "_mem.txt")
    with open(ring_path, "w") as f:
        for d in ring:
            f.write(" ".join("0x%08x" % x for x in d) + "\n")
    with open(mem_path, "w") as f:
        for v in mem:
            f.write("0x%08x\n" % v)
    cmd = [BIN, "--prog", prog_path, "--cycles", str(cycles),
           "--preload", pre_path, "--spad-dump", "0xA000", "832",
           "--ring-desc", ring_path, "--ring-base", "0x200", "--ring-size", "128",
           "--desc64", "--no-start", "--dma-mem", mem_path,
           "--dma-dump", dma_dump, "--dump", dump]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                       env={k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"})
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        sys.exit("台架运行失败，returncode=%d" % r.returncode)
    return r.stderr


def build_desc_bn_ring():
    """52 块 × 10 条 = 520 条 BN 整环，覆盖协议规模的描述符数量。"""
    import rule_map as rm
    mc = rm.schedule_bn_split(rows=20, rot=True)
    prog_path = os.path.join(OUT, "desc_bn_ring_prog.hex")
    with open(prog_path, "w") as f:
        for w in mc.build():
            f.write("0x%08x\n" % w)
    n = 4
    sh = [(e * 3 + 1) % ZC for e in range(n)]
    rnd = random.Random(19)
    edge_addr, llr_addr, hard_addr, table_addr = 0xE000, 0xE400, 0xE500, 0x000
    mem = [0] * (0xE600 // 4)
    tab = [0] * 32
    for e in range(n):
        tab[e] = (ZC - sh[e]) % ZC
    for e in range(n, 24):
        tab[e] = ROT_INVALID
    for i in range(32):
        mem[table_addr // 4 + i] = tab[i]
    for e in range(n):
        for w in range(ZC):
            mem[(edge_addr + e * 80) // 4 + w] = rnd.randint(-3000, 3000) & 0xFFFFFFFF
    for w in range(ZC):
        mem[llr_addr // 4 + w] = rnd.randint(-3000, 3000) & 0xFFFFFFFF

    ring = []
    for _ in range(52):
        t = [0] * 16
        t[0] = table_addr
        t[1] = 1 << 19
        t[2] = 32
        ring.append(t)
        d = [0] * 16
        d[0] = llr_addr
        d[1] = spad_idx(24, 0)
        d[2] = ZC
        ring.append(d)
        for e in range(n - 1):
            d = [0] * 16
            d[0] = edge_addr + e * 80
            d[1] = spad_idx(e, 0)
            d[2] = ZC
            ring.append(d)
        d = [0] * 16
        d[0] = edge_addr + (n - 1) * 80
        d[1] = spad_idx(n - 1, 0)
        d[2] = ZC
        d[6] = mc.cycles | (1 << 31)
        d[8] = ZC
        d[9] = 1
        d[10] = edge_addr
        d[12] = 1
        ring.append(d)
        for e in range(1, n):
            d = [0] * 16
            d[0] = edge_addr + e * 80
            d[1] = spad_idx(e, 0) | (1 << 17)
            d[2] = ZC
            ring.append(d)
        d = [0] * 16
        d[0] = hard_addr
        d[1] = spad_idx(25, 0) | (1 << 17)
        d[2] = ZC
        ring.append(d)
    pre = [(0x006C, ZC)]
    pre_path = os.path.join(OUT, "desc_bn_ring_preload.txt")
    with open(pre_path, "w") as f:
        for a, v in pre:
            f.write("%04x %08x\n" % (a, v))
    return prog_path, pre_path, mc.cycles, ring, mem


def run_desc_bn_ring(tag, prog_path, pre_path, cycles, ring, mem):
    dump = os.path.join(OUT, tag + "_dump.txt")
    dma_dump = os.path.join(OUT, tag + "_dma.txt")
    ring_path = os.path.join(OUT, tag + "_ring.txt")
    mem_path = os.path.join(OUT, tag + "_mem.txt")
    with open(ring_path, "w") as f:
        for d in ring:
            f.write(" ".join("0x%08x" % x for x in d) + "\n")
    with open(mem_path, "w") as f:
        for v in mem:
            f.write("0x%08x\n" % v)
    cmd = [BIN, "--prog", prog_path, "--cycles", str(cycles),
           "--preload", pre_path, "--spad-dump", "0xA000", "832",
           "--ring-desc", ring_path, "--ring-base", "0x1000", "--ring-size", "1024",
           "--desc64", "--no-start", "--dma-mem", mem_path,
           "--dma-dump", dma_dump, "--dump", dump, "--watch-writes"]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                       env={k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"})
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        sys.exit("台架运行失败，returncode=%d" % r.returncode)
    return r.stderr, len(ring)


def main():
    if not os.path.exists(BIN):
        sys.exit("先构建台架：make -C sim/cgra")

    prog, pre = build_inputs()
    bad_rot = check(run("rot", prog, pre, 6))
    if bad_rot:
        print("[FAIL] rot-test：%d 个值不符" % bad_rot)
        return 1
    print("[OK] rot-test：LD/ST 旋转、invalid、keep、rot=0 直通 全部通过")

    cn_prog, cn_pre, cn_cycles, cn_vals = build_cn_inputs()
    bad_cn = check_cn(run("cn", cn_prog, cn_pre, cn_cycles), cn_vals)
    if bad_cn:
        print("[FAIL] cn-wb-test：%d 个值不符" % bad_cn)
        return 1
    print("[OK] cn-wb-test：20 行 × 10 边 m' 回写 + keep 全部通过（%d 拍）" % cn_cycles)

    bn_prog, bn_pre, bn_cycles, bn_case = build_bn_inputs()
    bad_bn = check_bn(run("bn", bn_prog, bn_pre, bn_cycles), bn_case)
    if bad_bn:
        print("[FAIL] bn-rot-test：%d 个值不符" % bad_bn)
        return 1
    print("[OK] bn-rot-test：20 条边负向旋转 + invalid + 硬判全部通过（%d 拍）" % bn_cycles)

    p, pr, ring, mem, vals = build_dma_probe()
    spad, _ = run_desc("probe", p, pr, 1, ring, mem)
    bad = check_dma_probe(spad, vals, 20)
    if bad:
        print("[FAIL] dma-probe：输入 DMA 落点不对")
        return 1
    print("[OK] dma-probe：线性 20 字落在 bank0 word0..19")

    p, pr, cyc, ring, mem, msg = build_desc_cn()
    _, dma = run_desc("desc_cn", p, pr, cyc, ring, mem)
    bad = check_desc_cn(dma, msg)
    if bad:
        print("[FAIL] desc-cn-test：%d 个值不符" % bad)
        return 1
    print("[OK] desc-cn-test：表装载 + DMA 2D 搬入 + 阵列 + 搬出全部通过")

    p, pr, cyc, ring, mem, rows = build_desc_cn2()
    _, dma = run_desc("desc_cn2", p, pr, cyc, ring, mem)
    bad = check_desc_cn2(dma, rows)
    if bad:
        print("[FAIL] desc-cn2-test：%d 个值不符" % bad)
        return 1
    print("[OK] desc-cn2-test：两行连续 CN 环全部通过")

    # 相邻行：row0 结束地址正好是 row1 起始地址
    p, pr, cyc, ring, mem, rows = build_desc_cn2((0x400, 0x720), 2048)
    _, dma = run_desc("desc_cn2_adj", p, pr, cyc, ring, mem)
    bad = check_desc_cn2(dma, rows, (0x400, 0x720))
    if bad:
        print("[FAIL] desc-cn2-adj-test：%d 个值不符（相邻行被上一行输出覆盖）" % bad)
        return 1
    print("[OK] desc-cn2-adj-test：两行地址相邻、无越界覆盖")

    p, pr, cyc, ring, mem, rows = build_desc_cn2((0xA00, 0xF00), 2048)
    _, dma = run_desc("desc_cn2_4k", p, pr, cyc, ring, mem)
    bad = check_desc_cn2(dma, rows, (0xA00, 0xF00))
    if bad:
        print("[FAIL] desc-cn2-4k-test：%d 个值不符" % bad)
        return 1
    print("[OK] desc-cn2-4k-test：前一行不跨、后一行跨 4 KiB 全部通过")

    p, pr, cyc, ring, mem, msg = build_desc_cn4k()
    _, dma = run_desc("desc_cn4k", p, pr, cyc, ring, mem)
    bad = check_desc_cn4k(dma, msg)
    if bad:
        print("[FAIL] desc-cn4k-test：%d 个值不符" % bad)
        return 1
    print("[OK] desc-cn4k-test：跨 4 KiB 边界的 CN 行全部通过")

    p, pr, cyc, ring, mem, msg = build_desc_cn4k(0xF40)
    _, dma = run_desc("desc_cn4k_b", p, pr, cyc, ring, mem)
    bad = check_desc_cn4k(dma, msg, 0xF40)
    if bad:
        print("[FAIL] desc-cn4k-partial-test：%d 个值不符" % bad)
        return 1
    print("[OK] desc-cn4k-partial-test：4 KiB 边界前部分 burst 全部通过")

    # 回归：最后一次突发只剩 4 拍，但离 4 KiB 边界还有 28 拍。旧 RTL 会直接拿
    # “到边界的拍数”当突发长度，把 4 拍扩成 28 拍，多写 48 个字覆盖紧邻缓冲区
    # （协议里 msg[9] 的输出就是这样盖掉 msg[10] 的）。
    p, pr, cyc, ring, mem, msg = build_desc_cn4k(0x1C20)
    _, dma = run_desc("desc_cn_tail", p, pr, cyc, ring, mem)
    bad = check_desc_cn4k(dma, msg, 0x1C20)
    if bad:
        print("[FAIL] desc-cn-tail-test：%d 个值不符" % bad)
        return 1
    tail = [i for i in range(0x1F40 // 4, 0x2000 // 4) if dma[i] != mem[i]]
    if tail:
        print("[FAIL] desc-cn-tail-test：末突发越界写 %d 个字（首地址 0x%x）"
              % (len(tail), tail[0] * 4))
        return 1
    print("[OK] desc-cn-tail-test：末突发止于描述符长度，无越界写")

    # 4 KiB 尾部扫描：旧突发规划在这些点上会多写（最坏 48 字），
    # 台架的 --watch-writes 会报"窗口外被写"。
    p, pr, cyc, ring, mem, cases, pattern = build_desc_sweep()
    _, dma = run_desc("desc_sweep", p, pr, cyc, ring, mem)
    bad = check_desc_sweep(dma, cases, pattern)
    if bad:
        print("[FAIL] desc-sweep-test：%d 个值不符" % bad)
        return 1
    print("[OK] desc-sweep-test：%d 组 4 KiB 尾部组合，数据正确且窗口外无写入"
          % len(cases))

    p, pr, cyc, ring, mem, case = build_desc_bn()
    _, dma = run_desc("desc_bn", p, pr, cyc, ring, mem)
    bad = check_desc_bn(dma, case)
    if bad:
        print("[FAIL] desc-bn-test：%d 个值不符" % bad)
        return 1
    print("[OK] desc-bn-test：描述符环 + 旋转 + 硬判搬出全部通过")

    p, pr, cyc, ring, mem = build_desc_cn_ring84()
    err = run_desc_ring84("desc_cn_ring84", p, pr, cyc, ring, mem)
    m = re.search(r"\[ring\] (\d+) descriptors, size=(\d+), done=(\d+)", err)
    if not m or int(m.group(3)) != 84:
        print(err)
        print("[FAIL] desc-cn-ring84-test：整环未全部退休（done=%s）" %
              (m.group(3) if m else "?"))
        return 1
    print("[OK] desc-cn-ring84-test：84 条 CN 描述符整环全部退休")

    p, pr, cyc, ring, mem = build_desc_bn_ring()
    err, want = run_desc_bn_ring("desc_bn_ring", p, pr, cyc, ring, mem)
    m = re.search(r"\[ring\] (\d+) descriptors, size=(\d+), done=(\d+)", err)
    if not m or int(m.group(3)) != want:
        print(err)
        print("[FAIL] desc-bn-ring-test：整环未全部退休（done=%s / %d）" %
              (m.group(3) if m else "?", want))
        return 1
    print("[OK] desc-bn-ring-test：%d 条 BN 描述符整环全部退休" % want)
    return 0


if __name__ == "__main__":
    sys.exit(main())
