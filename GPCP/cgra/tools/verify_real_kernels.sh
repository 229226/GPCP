#!/usr/bin/env bash
# 一键验收：MMSE/FFT 等真实 kernel 走 L1/L2 编译器 + 既有产物逐字节回归。
# 兼容两种布局：
#   仓库布局  <repo>/cgra/tools/verify_real_kernels.sh   （VM ~/GPCP）
#   扁平布局  <dir>/verify_real_kernels.sh               （_src 镜像）
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -d "$HERE/../../sw/soc" ] && [ -d "$HERE/../../cgra/kernels" ]; then
  TOOLS="$HERE"; ROOT="$(cd "$HERE/../.." && pwd)"
else
  TOOLS="$HERE"; ROOT="$HERE"
fi
GEN="$TOOLS/gen_kernel_prog.py"
SOC="$ROOT/sw/soc"
fail=0
say() { printf '%s\n' "$*"; }

say "== 0) 布局 =="
say "  TOOLS=$TOOLS"
say "  ROOT =$ROOT"

say ""
say "== 1) 批量生成全部 kernel =="
cp -f "$SOC/pdsch_bn_prog.h" /tmp/_bn_base.h 2>/dev/null
cp -f "$SOC/pdsch_cn_prog.h" /tmp/_cn_base.h 2>/dev/null
if python3 "$GEN" --all; then say "[OK] --all 全成"; else say "[FAIL] --all"; fail=1; fi

say ""
say "== 2) BN/CN 逐字节回归（与同步前基线）=="
if [ -f /tmp/_bn_base.h ] && cmp -s /tmp/_bn_base.h "$SOC/pdsch_bn_prog.h"; then
  say "[OK] pdsch_bn_prog.h 整文件 IDENTICAL"
else
  say "[FAIL] pdsch_bn_prog.h 与基线不同"; fail=1
fi
if [ -f /tmp/_cn_base.h ] && cmp -s /tmp/_cn_base.h "$SOC/pdsch_cn_prog.h"; then
  say "[OK] pdsch_cn_prog.h 整文件 IDENTICAL"
else
  say "[FAIL] pdsch_cn_prog.h 与基线不同"; fail=1
fi

say ""
say "== 3) 薄壳兼容（gen_bn_prog.py / gen_cn_prog.py）=="
python3 "$TOOLS/gen_bn_prog.py" --out /tmp/_bn_shell.h >/dev/null 2>&1 \
  && cmp -s /tmp/_bn_shell.h "$SOC/pdsch_bn_prog.h" && say "[OK] BN 薄壳 IDENTICAL" || { say "[FAIL] BN 薄壳"; fail=1; }
python3 "$TOOLS/gen_cn_prog.py" --out /tmp/_cn_shell.h >/dev/null 2>&1 \
  && cmp -s /tmp/_cn_shell.h "$SOC/pdsch_cn_prog.h" && say "[OK] CN 薄壳 IDENTICAL" || { say "[FAIL] CN 薄壳"; fail=1; }

say ""
say "== 4) 真实 kernel 浮点黄金对账（FFT 蝶形 / MMSE 相关）=="
if python3 "$TOOLS/refcheck_real_kernels.py"; then say "[OK] 真实 kernel 对账 PASS"; else say "[FAIL] 真实 kernel 对账"; fail=1; fi

say ""
say "== 5) 产物清单 =="
ls -1 "$SOC"/*_prog.h | sed 's/^/  /'

say ""
if [ "$fail" -eq 0 ]; then say "RESULT: ALL PASS"; else say "RESULT: FAIL"; fi
exit "$fail"
