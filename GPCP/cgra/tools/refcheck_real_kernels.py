#!/usr/bin/env python3
"""真实 kernel 参考对账：用浮点黄金模型验证生成的微码确实实现了
FFT 蝶形 / MMSE 相关（而非只是自洽但错误的算术）。

做法：
  1. 复刻 gen_kernel_prog 的两阶段调度，拿到 Microcode 与叶子/汇地址；
  2. 取一组 Q15 定点输入（int16），塞进 spad；
  3. rule_map.simulate 跑微码，读出汇；
  4. 用「与硬件同一定点语义反推的浮点公式」算期望，量化到微码的有效输出 Q，
     比绝对误差（允许量化误差，容差 ±ERR_TOL）。
"""
import os, sys, random
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import kernel_spec as ks
import rule_map as rm
import bank_alloc as ba

Q15 = 32768

def _plan_assign(dfg):
    leaves = {n: i for i, n in enumerate(dfg.leaves())}
    sinks = {n: 100 + i for i, n in enumerate(dfg.sinks())}
    return leaves, sinks

def _find_spec(name):
    """兼容三种布局：同目录 / 上级 kernels/ / <repo>/cgra/kernels/。"""
    cands = [
        os.path.join(HERE, name),
        os.path.join(HERE, "..", "kernels", name),
        os.path.join(HERE, "..", "..", "cgra", "kernels", name),
    ]
    for c in cands:
        if os.path.exists(c):
            return os.path.abspath(c)
    raise SystemExit(f"找不到 {name}（试过：{cands}）")


def build_mc(spec_path):
    spec = ks.load_spec(spec_path)
    assert not ks.validate(spec), "spec 不应有校验错误"
    dfg = ks.build_dfg(spec)
    instances = spec.get("instances", 16)
    plan_leaves, plan_sinks = _plan_assign(dfg)
    mc0, _lv, _sa, _b, liv, max_regs = rm.schedule(
        dfg, instances, banks=plan_leaves, sink_addrs=plan_sinks, return_liveness=True)
    end = mc0.cycles - 1
    liv_alloc = {}
    for n in dfg.leaves():
        liv_alloc[n] = (0, end)
    for n in dfg.sinks():
        liv_alloc[n] = (liv.get(n, (0, end))[0], end)
    hints = {}
    for fld in ("leaves", "sinks"):
        for name, bank in ks.expand_mapping(spec.get(fld, {})).items():
            if bank is not None:
                hints[name] = bank
    alloc = ba.alloc_banks(liv_alloc, hints=hints)
    lb = {n: b for n, b in alloc.items() if n in set(dfg.leaves())}
    sb = {n: b for n, b in alloc.items() if n in set(dfg.sinks())}
    mc, _lv, sink_addrs, banks = rm.schedule(dfg, instances, banks=lb, sink_addrs=sb)
    return dfg, mc, banks, sink_addrs, instances


def qround(v, q):
    return int(round(v * (1 << q)))


# ---------------------------------------------------------------- FFT 蝶形
def ref_fft():
    dfg, mc, banks, sink_addrs, inst = build_mc(_find_spec("fft_butterfly.yaml"))
    rnd = random.Random(123)
    spad = {}
    cases = []
    for w in range(inst):
        Ar = rnd.randint(-30000, 30000); Ai = rnd.randint(-30000, 30000)
        Br = rnd.randint(-30000, 30000); Bi = rnd.randint(-30000, 30000)
        Wr = rnd.randint(-30000, 30000); Wi = rnd.randint(-30000, 30000)
        for nm, v in (("Ar", Ar), ("Ai", Ai), ("Br", Br), ("Bi", Bi), ("Wr", Wr), ("Wi", Wi)):
            spad[rm.spad_idx(banks[nm], w)] = v & 0xFFFFFFFF
        # 浮点公式（Q15 输入）：t = W*B, A' = A+t, B' = A-t；输出经 >>1 故有效 Q=14
        Arf, Aif = Ar / Q15, Ai / Q15
        Brf, Bif = Br / Q15, Bi / Q15
        Wrf, Wif = Wr / Q15, Wi / Q15
        tr = Wrf * Brf - Wif * Bif           # Re(W*B)
        ti = Wrf * Bif + Wif * Brf           # Im(W*B)
        # 微码 (Ar + tr_q15) >> 1 = (Arf + tr_f) * 2^14，即有效 Q14（>>1 已是那一步）
        Aor = qround(Arf + tr, 14)
        Aoi = qround(Aif + ti, 14)
        Bor = qround(Arf - tr, 14)
        Boi = qround(Aif - ti, 14)
        cases.append((w, dict(Aor=Aor, Aoi=Aoi, Bor=Bor, Boi=Boi)))
    rm.simulate(mc, spad)
    maxerr = 0
    for w, exp in cases:
        for nm, want in exp.items():
            got = rm._s32(spad[rm.spad_idx(sink_addrs[nm], w)])
            maxerr = max(maxerr, abs(got - want))
    return maxerr

# ---------------------------------------------------------------- MMSE 2x1（复数相关）
def ref_mmse():
    dfg, mc, banks, sink_addrs, inst = build_mc(_find_spec("mmse_2x1.yaml"))
    rnd = random.Random(7)
    spad = {}
    cases = []
    for w in range(inst):
        h1r = rnd.randint(-18000, 18000); h1i = rnd.randint(-18000, 18000)
        h2r = rnd.randint(-18000, 18000); h2i = rnd.randint(-18000, 18000)
        y1r = rnd.randint(-18000, 18000); y1i = rnd.randint(-18000, 18000)
        y2r = rnd.randint(-18000, 18000); y2i = rnd.randint(-18000, 18000)
        for nm, v in (("h1r", h1r), ("h1i", h1i), ("h2r", h2r), ("h2i", h2i),
                      ("y1r", y1r), ("y1i", y1i), ("y2r", y2r), ("y2i", y2i)):
            spad[rm.spad_idx(banks[nm], w)] = v & 0xFFFFFFFF
        # 浮点：num = conj(h1)y1 + conj(h2)y2；微码输出为 Q30
        c1r = (h1r / Q15) * (y1r / Q15) + (h1i / Q15) * (y1i / Q15)
        c2r = (h2r / Q15) * (y2r / Q15) + (h2i / Q15) * (y2i / Q15)
        c1i = (h1r / Q15) * (y1i / Q15) - (h1i / Q15) * (y1r / Q15)
        c2i = (h2r / Q15) * (y2i / Q15) - (h2i / Q15) * (y2r / Q15)
        cases.append((w, dict(numr=qround(c1r + c2r, 30), numi=qround(c1i + c2i, 30))))
    rm.simulate(mc, spad)
    maxerr = 0
    for w, exp in cases:
        for nm, want in exp.items():
            got = rm._s32(spad[rm.spad_idx(sink_addrs[nm], w)])
            maxerr = max(maxerr, abs(got - want))
    return maxerr


if __name__ == "__main__":
    ERR_TOL = 8
    e1 = ref_fft()
    e2 = ref_mmse()
    print(f"[FFT 蝶形]   浮点黄金对账最大绝对误差 = {e1} LSB（容差 ±{ERR_TOL}）")
    print(f"[MMSE 2x1]   浮点黄金对账最大绝对误差 = {e2} LSB（容差 ±{ERR_TOL}）")
    ok = e1 <= ERR_TOL and e2 <= ERR_TOL
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
