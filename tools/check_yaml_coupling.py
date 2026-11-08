#!/usr/bin/env python3
"""AGENTS.md 4.19：kernel YAML 不得直接描述硬件坐标。

在每档 verify 的预检里执行（与 check_handprog.py 并列，指纹缓存跳过不了）。
硬件真相只允许出现在 cgra/target.yaml 与 cgra/tools/target.py；kernel YAML
只能引用命名区域、逻辑槽位与 parallelism。
"""
import glob
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "cgra", "tools"))
import target as hw  # noqa: E402

T = hw.load()

# (正则, 违规说明)
FORBIDDEN = [
    (r"\baddr:\s*\{", "物理 flat 地址 addr（改用 at: {region, slot, pe_per_bank}）"),
    (r"(?m)^\s*instances:\s*\d", "写死并行度 instances（改用 parallelism: full / N）"),
    (r"\bbase_offset:", "物理地址平移 base_offset（改用 shift + slot 里的 SHIFT）"),
    (r"\bbank:\s*\d", "物理 bank 号（改用 target.regions 的命名区域）"),
    (r'\bbank:\s*"[0-9][0-9.]*"', "物理 bank 号字符串（改用命名区域）"),
]
DESC_PHYS = re.compile(r"flat\s|stride=|bank\s+\d|word\s+\d|汇\s+\d{3}", re.I)
BANNED_CODE = {
    "cgra/tools/bank_alloc.py": ["N_BANKS = 32"],
    "cgra/tools/rule_map.py": ["regs = [[0] * 8", "SPAD_WIN = 0xA000"],
    "cgra/tools/gen_kernel_prog.py": ["mc.cycles > 512", "maxbank > 31",
                                      "maxflat > 1023", "max_regs > 8"],
    "cgra/tools/image_spec.py": ["max_regs > 8"],
}

bad = []
n_yaml = n_at = n_parallel = 0

for p in sorted(glob.glob(os.path.join(ROOT, "cgra", "kernels", "*.yaml"))):
    rel = os.path.relpath(p, ROOT).replace("\\", "/")
    s = open(p, encoding="utf-8").read()
    n_yaml += 1
    n_at += len(re.findall(r"\bat:\s*\{", s))
    n_parallel += len(re.findall(r"(?m)^\s*parallelism:", s))
    for pat, msg in FORBIDDEN:
        for m in re.finditer(pat, s):
            line = s.count("\n", 0, m.start()) + 1
            bad.append(f"{rel}:{line}  {msg}")
    # 逻辑地址表达式里若出现 > words_per_bank 的裸数字，多半是漏网的物理坐标
    for m in re.finditer(r'(?:region|slot):\s*"([^"]+)"', s):
        for num in re.findall(r"(?<![\w])\d+", m.group(1)):
            if int(num) > T.words:
                line = s.count("\n", 0, m.start()) + 1
                bad.append(f"{rel}:{line}  逻辑地址含物理常量 {num}（改用命名区域/符号）")
    # 说明文字也不该再教人写物理坐标
    for m in re.finditer(r'(?m)^desc:\s*"?([^\n]*)', s):
        if DESC_PHYS.search(m.group(1)):
            line = s.count("\n", 0, m.start()) + 1
            bad.append(f"{rel}:{line}  desc 仍描述物理坐标")

for rel, pats in BANNED_CODE.items():
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        bad.append(f"{rel}  缺失")
        continue
    s = open(p, encoding="utf-8").read()
    for pat in pats:
        if pat in s:
            bad.append(f"{rel}  仍硬编码硬件常量：{pat}")

# 正向检查：ISA/几何必须从 target 加载
p = os.path.join(ROOT, "tools", "cgra.py")
if "target.yaml" not in open(p, encoding="utf-8").read() and \
        "_T = _hw_mod.load()" not in open(p, encoding="utf-8").read():
    bad.append("tools/cgra.py 没有从 cgra/target.yaml 派生 ISA/几何")

if bad:
    print(f"[FAIL] kernel YAML 与硬件耦合 {len(bad)} 处：")
    for b in bad[:40]:
        print("  -", b)
    if len(bad) > 40:
        print(f"  ... 另有 {len(bad) - 40} 处")
    sys.exit(1)

print(f"[ OK ] YAML 与硬件解耦：{n_yaml} 份 spec / {n_at} 处逻辑地址 / "
      f"{n_parallel} 处 parallelism / 0 处物理坐标；target={T.name}")
