#!/usr/bin/env python3
"""数值契约门禁：确认机器可读契约真的被编译器静态证明，且证明逻辑没被改坏。

两件事：
  1. 遍历 cgra/kernels/*.yaml，凡声明 contract 的 dfg spec，validate() 必须无错；
  2. 用一份真实 spec 派生若干「故意写坏」的契约，validate() 必须全部拒绝。

前者防止有人把不安全的数值范围写进契约；后者防止有人把静态范围传播
改成空实现（那样所有契约都会"通过"，比没有契约更危险）。
"""
import copy
import glob
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "cgra", "tools"))
import kernel_spec as ks  # noqa: E402

bad = []
contracted = []
for p in sorted(glob.glob(os.path.join(ROOT, "cgra", "kernels", "*.yaml"))):
    d = ks.load_spec(p)
    if not isinstance(d, dict) or d.get("kind", "dfg") != "dfg":
        continue                       # image / bn / cn 顶层不走 dfg 校验
    if "variants" in d or "ops" not in d:
        continue                       # variants 展开后才有完整 DFG
    if "contract" not in d:
        continue
    name = os.path.basename(p)
    contracted.append(name)
    errs = ks.validate(d)
    if errs:
        bad.append("%s 契约不成立：%s" % (name, "; ".join(errs[:2])))

if not contracted:
    print("[FAIL] 没有任何 spec 声明 contract，数值契约门禁形同虚设")
    sys.exit(1)

# ---------------------------------------------------------------- 反例自测
cands = []
for name in contracted:
    d = ks.load_spec(os.path.join(ROOT, "cgra", "kernels", name))
    cap = d.get("contract", {}).get("leaf_max_abs")
    if isinstance(cap, int):
        cands.append((cap, d))
if not cands:
    print("[FAIL] 找不到可用于反例自测的 leaf_max_abs 契约")
    sys.exit(1)
# 选上界最小的那份：1.5 倍仍在 int16 内，能单独测「回绕」而非「截断」
template = min(cands, key=lambda t: t[0])[1]

base = int(template["contract"]["leaf_max_abs"])
sink_cap = template["contract"].get("sink_max_abs")

cases = []


def case(label, mutate, expect_error):
    d = copy.deepcopy(template)
    mutate(d)
    errs = ks.validate(d)
    cases.append((label, bool(errs), expect_error, errs))


case("叶子上界放大到中间值回绕（仍在 int16 内）",
     lambda d: d["contract"].update(leaf_max_abs=int(base * 1.5)), True)
case("叶子上界超过 16 位（mul 输入被截断）",
     lambda d: d["contract"].update(leaf_max_abs=40000), True)
case("汇声明小于推导值",
     lambda d: d["contract"].update(sink_max_abs=1), True)
case("删除 leaf_max_abs",
     lambda d: d["contract"].pop("leaf_max_abs", None), True)
case("contract 不是映射",
     lambda d: d.update(contract="Q15"), True)
case("合法契约仍然通过", lambda d: None, False)

n_reject = 0
for label, got_err, expect, errs in cases:
    ok = got_err == expect
    if not ok:
        bad.append("反例 %r：期望 %s，实际 %s" %
                   (label, "拒绝" if expect else "通过", "拒绝" if got_err else "通过"))
    if expect and got_err:
        n_reject += 1

if bad:
    print("[FAIL] 数值契约门禁 %d 处问题：" % len(bad))
    for b in bad:
        print("  -", b)
    sys.exit(1)

print("[ OK ] 数值契约：%d 份 spec 声明 / 静态证明全通过 / %d 项反例正确拒绝；"
      "基准 leaf_max_abs=%d%s" %
      (len(contracted), n_reject, base,
       (" / sink_max_abs=%d" % sink_cap) if sink_cap is not None else ""))
print("       " + ", ".join(contracted))
