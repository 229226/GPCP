"""把语义 IR 落到某个硬件目标上（docs/20 §3 第 3 步）。

**这是整个方案里唯一允许出现硬件数字的地方**（另一个是 cgra/target.yaml）。
它只做三件事：
  1. 读目标几何（阵列规模 / 存储容量 / 指令内存深度），可用 what-if 补丁覆盖；
  2. 为每个语义节点挑算子实现与切块，算出资源需求；
  3. 判断可行性，不可行就说明"缺什么"，绝不回退成让用户手写坐标。
"""
import json
import os
import math
import re

# 算子库当前能提供的实现（与 cgra/kernels/*.yaml 对应；换库时改这里）
# 注意：仓库里只有 4096 点 FFT 镜像（sw/soc/ssb_fft.inc 的 FFT_N=4096），
# 不要在计划里声称存在 256/1024 点镜像。
FFT_SIZES = (4096,)
LDPC_ZC = (20, 64)          # cn20/bn24 与 zc64 两套
POLAR_N = 512               # polar 9 级镜像覆盖 512 点
POLAR_PES = 16              # polar_node 微码固定 16 PE（与 target 的 what-if 无关）
# 经验常数：一次阵列运行的编排开销（T0 台架实测 T(c)=32+1.0045c，取上限估计）
T0_OVERHEAD = 32

# ---------------------------------------------------------------------------
# 新增 CGRA 镜像（docs/22 §2.3「仍缺阵列实现」四项，2026-11-11 补齐）
#
# 镜像都由 cgra/kernels/*.yaml -> gen_kernel_prog.py 生成，自带 ISA 级逐值
# 自检，并由 tools/check_new_kernels.py（算法黄金）与
# sim/soc:run-new-kernels（SoC 硬件）双重对账。
#
# 默认不启用：docs/22 实测这些算法留在 CVA6 更快，或阵列被编排拖累
# （LDPC 阵列只忙 6%）。置 PROTO_NEW_ARRAY=1 才切到阵列，用于 A/B 与
# docs/21 P4/P5 的后续优化，不改变任何默认 plan.h。
# ---------------------------------------------------------------------------
NEW_ARRAY_IMAGES = {
    "modulate":        ("qam_map",        "qam_qpsk/qam_16qam/qam_64qam"),
    "ldpc_encode":     ("ldpc_enc_acc",   "ldpc_enc_acc"),
    "turbo":           ("turbo_acs",      "turbo_acs"),
    "ldpc_wifi":       ("wifi_ldpc_tiles", "wifi_vn + wifi_cn24"),
}
USE_NEW_ARRAY = os.environ.get("PROTO_NEW_ARRAY", "") == "1"

# 4096 点 FFT 镜像的 tiling（与 ssb_fft.inc 的 fe_desc_stages/fe_desc_tail 同源）：
#   每 256 点一个 tile，radix-4 阶段数 = floor(log4(N/256)) + 1，最后 64 点一个 tail tile。
FFT_TILE = 256
FFT_TAIL = 64


def fft_runs_per_symbol(size):
    """一次 N 点 FFT 实际启动阵列的次数（不是 N/1024 这种粗估）。"""
    tiles = max(1, size // FFT_TILE)
    stages = 0
    while (size >> (2 * stages)) >= FFT_TILE:
        stages += 1
    return stages * tiles + size // FFT_TAIL


def fft_candidates(need, cp_len):
    """枚举算子库能覆盖 need 的 FFT 实现；返回 (chosen, candidates)。"""
    out = []
    for size in FFT_SIZES:
        if size >= need:
            out.append({
                "impl": "fft%d" % size,
                "size": size,
                "runs_per_symbol": fft_runs_per_symbol(size),
                "over_provision": round(size / float(need), 3),
                "buffer_words_per_symbol": 2 * (size + cp_len),
            })
    if not out:
        return None, []
    return min(out, key=lambda c: (c["runs_per_symbol"], c["size"])), out


def infer_nsym(ir):
    """符号数：cfg.nsym > received(tdN)/长度 > 自环默认 2。"""
    cfg = ir.get("cfg", {})
    if cfg.get("nsym"):
        return int(cfg["nsym"])
    d = ir.get("derived", {})
    nfft = int(d.get("n_fft_min") or 0)
    cp = int(d.get("cp_len") or 0)
    for nd in ir["nodes"]:
        if nd["op"] != "received":
            continue
        form = str(nd["params"].get("form") or "")
        m = re.match(r"td(\d+)$", form)
        if m:
            return int(m.group(1))
        length = int(nd.get("length") or 0)
        if nfft and length and length % (2 * (nfft + cp)) == 0:
            return length // (2 * (nfft + cp))
    return 1


def polar_decode_runs(npol=POLAR_N, npes=POLAR_PES):
    """polar SC 树的真实阵列运行数：每个内部节点两次 node_run，按 npes 分块。"""
    levels = int(round(math.log(npol, 2)))
    total = 0
    for lvl in range(levels):
        nodes = 1 << lvl
        half = npol >> (lvl + 1)
        total += nodes * ((half + npes - 1) // npes)
    return 2 * total



def read_target(path):
    """只读需要的几个字段（避免给仓库加 PyYAML 依赖）。"""
    txt = open(path, encoding="utf-8").read()
    def grab(key, default=None):
        m = re.search(r"^\s*%s:\s*(\d+)" % key, txt, re.M)
        return int(m.group(1)) if m else default
    rows = grab("rows", 4)
    cols = grab("cols", 4)
    return {
        "source": path,
        "rows": rows,
        "cols": cols,
        "n_pes": rows * cols,
        "banks": grab("banks", 32),
        "words_per_bank": grab("words_per_bank", 32),
        "imem_cycles": grab("max_cycles", 512),
        "pe_regs": grab("regs", 8),
        "clock_hz": grab("clock_hz", 100000000),
    }


def apply_overrides(base, path):
    """what-if 配置：只覆盖指定字段，用于"换厨房测试"（不是第二个真相）。"""
    t = dict(base)
    t.update(read_target(path))
    t["source"] = "%s + %s" % (base["source"], path)
    return t


def _pick(size, choices):
    for c in choices:
        if c >= size:
            return c, c / float(size)
    return None, None


def lower(ir, target):
    """返回 (plan, ok)。plan 里每个节点一行，附可行性与倍数诊断。"""
    n_spad_words = target["banks"] * target["words_per_bank"]
    plan = {"schema": "gpcp-proto-plan/1", "scenario": ir["scenario"],
            "target": target, "stages": [], "ok": True, "needs": [],
            "search": {"fft": [], "wifi_dft": [], "ldpc": []}}
    d = ir.get("derived", {})
    need = d.get("n_fft_min")
    nsym = infer_nsym(ir)
    cp_len = int(d.get("cp_len", 0) or 0)

    for nd in ir["nodes"]:
        op = nd["op"]
        st = {"id": nd["id"], "op": op, "out": nd["out"], "where": "control-core",
              "impl": None, "notes": []}

        if op in ("ofdm_modulate", "ofdm_demodulate"):
            if ir.get("rat") == "wifi":
                # WiFi 没有阵列 FFT 镜像：在控制核上搜索标量 DFT 的 tiling。
                n = need or 0
                cands = [{
                    "impl": "dft%d_scalar" % n if n else "dft_scalar",
                    "kind": "direct-dft",
                    "est_ops": n * n,
                    "buffer_words": 2 * n,
                    "tiling": {"form": "direct", "points": n},
                }]
                if n == 256:
                    cands.append({
                        "impl": "dft256_radix2",
                        "kind": "radix-2-fft",
                        "est_ops": 5 * n * 8,       # 8 级 × 128 蝶形 × ~5 op
                        "buffer_words": 2 * n,
                        "tiling": {"form": "radix-2", "stages": 8,
                                   "butterflies": n // 2 * 8},
                    })
                    cands.append({
                        "impl": "dft256_cgra",
                        "kind": "cgra-radix4-fft",
                        "est_ops": 4096,            # 1 stage tile + 4 tail block
                        "buffer_words": 2 * n,
                        "tiling": {"form": "cgra-fft", "stages": 1,
                                   "blocks": 4},
                    })
                best = min(cands, key=lambda c: (c["est_ops"], c["buffer_words"]))
                st["where"] = "control-core"
                st["impl"] = best["impl"]
                st["array_runs"] = 0
                st["tiling"] = best["tiling"]
                st["buffer_words"] = best["buffer_words"]
                st["search"] = {
                    "candidates": [c["impl"] for c in cands],
                    "chosen": best["impl"],
                    "rule": "min est_ops; tie min buffer_words",
                }
                st["notes"].append(
                    "802.11be %d 点 DFT 由 CVA6 标量执行；搜索选择 %s"
                    % (n, best["impl"]))
                plan["search"]["wifi_dft"].append(
                    {"stage": nd["id"], "need": n, "candidates": cands,
                     "chosen": best["impl"]})
            elif need is None:
                st["notes"].append("cfg 未给出带宽/子空间隔，无法推导 FFT 点数")
                plan["ok"] = False
                plan["needs"].append("cfg(bw_mhz, scs_khz)")
            else:
                cand, cands = fft_candidates(need, cp_len)
                if cand is None:
                    plan["ok"] = False
                    plan["needs"].append("FFT >= %d 的算子实现" % need)
                else:
                    runs = cand["runs_per_symbol"] * nsym
                    st["where"] = "array"
                    st["impl"] = cand["impl"]
                    st["over_provision"] = cand["over_provision"]
                    st["array_runs"] = runs
                    st["tiling"] = {
                        "size": cand["size"],
                        "symbols": nsym,
                        "tiles_per_symbol": cand["runs_per_symbol"],
                        "tiles_total": runs,
                    }
                    st["buffer_words"] = cand["buffer_words_per_symbol"] * min(nsym, 2)
                    st["search"] = {
                        "candidates": [c["impl"] for c in cands],
                        "chosen": cand["impl"],
                        "rule": "min runs; tie min size",
                    }
                    if cand["over_provision"] > 1.0:
                        st["notes"].append(
                            "最小需要 %d 点，算子库只有 %d 点 → 空转 %.2fx"
                            % (need, cand["size"], cand["over_provision"]))
                    if 448 + T0_OVERHEAD > target["imem_cycles"]:
                        plan["ok"] = False
                        plan["needs"].append("IMEM >= 512 拍（FFT 镜像 448 拍）")
                    plan["search"]["fft"].append(
                        {"stage": nd["id"], "op": op, "need": need,
                         "nsym": nsym, "candidates": cands,
                         "chosen": cand["impl"], "runs": runs})

        elif op == "decode" and nd["params"].get("scheme") == "polar":
            st["where"] = "array"
            st["impl"] = "polar_node@%d" % POLAR_N
            st["array_runs"] = polar_decode_runs()
            st["notes"].append("SC 树每内部节点两次 node_run，按 %d PE 分块 → %d 次阵列运行"
                               % (POLAR_PES, st["array_runs"]))

        elif op == "encode" and nd["params"].get("scheme") == "polar":
            st["where"] = "array"
            st["impl"] = "polar9@%d" % POLAR_N
            st["array_runs"] = 9
            st["notes"].append("9 级变换，512 点由 16 PE 并行")

        elif op in ("encode", "decode") and nd["params"].get("scheme") == "ldpc_wifi":
            # IEEE 802.11 N=648 / Z=27 / rate 1/2：标量 min-sum，不占阵列。
            st["where"] = "control-core"
            st["impl"] = "wifi_ldpc_z27"
            st["notes"].append("802.11 N648 R1/2 (Z=27) 标量 min-sum")

        elif op in ("encode", "decode") and nd["params"].get("scheme") == "conv":
            # EHT-SIG 的 K=7 卷积码：标量 Viterbi，不占阵列。
            st["where"] = "control-core"
            st["impl"] = "viterbi_k7"
            st["notes"].append("K=7 BCC (0o133/0o171)")

        elif op in ("encode", "decode") and nd["params"].get("scheme") == "turbo":
            # 36.212 Turbo（1/3、QPP）：标量实现，跑在控制核上；不占阵列。
            st["where"] = "control-core"
            st["impl"] = "turbo_k104"
            st["notes"].append("36.212 Turbo 1/3 + 速率匹配（K=104）")

        elif op in ("encode", "decode") and nd["params"].get("scheme") == "ldpc":
            # 38.212：BG2 的码字长 N = 52*Zc，信息位 K = Kb*Zc（K<=640 时 Kb=6）。
            # encode 节点带的是编码后比特数，decode 节点带的是 TB 比特数，两条路
            # 都推回同一个 Zc 需求，而不是拿"比特数 / 52 + 1"去猜。
            if op == "encode":
                zc_need = max(1, (nd.get("length") or 0) // 52)
            else:
                k_info = (d.get("a_bits") or 0) + 24        # TB + CRC24
                zc_need = max(1, -(-k_info // 6))
            iters = int(nd["params"].get("max_iter", 8))
            cands = []
            for z in LDPC_ZC:
                words = 26 * z
                if words <= n_spad_words:
                    cands.append({
                        "impl": "ldpc_bg2_zc%d" % z,
                        "zc": z,
                        "spad_words": words,
                        "iters": iters,
                        "array_runs": iters * 94 if op == "decode" else 0,
                    })
            if not cands:
                plan["ok"] = False
                plan["needs"].append("LDPC 提升长度 >= %d 的实现" % zc_need)
            else:
                best = min(cands, key=lambda c: (c["spad_words"], c["array_runs"]))
                st["where"] = "control-core" if op == "encode" else "array"
                st["impl"] = best["impl"]
                st["spad_words"] = best["spad_words"]
                st["buffer_words"] = best["spad_words"]
                st["search"] = {
                    "need_zc": zc_need,
                    "candidates": [c["impl"] for c in cands],
                    "chosen": best["impl"],
                    "rule": "min spad_words; tie min runs",
                }
                st["notes"].append("每块 %d 字（26 槽 x zc）" % best["spad_words"])
                if op == "decode":
                    st["array_runs"] = best["array_runs"]
                plan["search"]["ldpc"].append({
                    "stage": nd["id"], "op": op, "need_zc": zc_need,
                    "candidates": cands, "chosen": best["impl"],
                })

        elif op == "correlate":
            # 相关搜索：一个候选假设 = 一次阵列运行（prach_corr / pss_corr /
            # sss_corr 都是 16 PE 的部分和 + 阵列内归约）。
            seq = nd["params"].get("seq", "zc")
            if seq in ("lte_pss", "lte_sss"):
                # 62 点相关复用 prach_corr 镜像（127 tap 补零），一个假设一次运行。
                st["where"] = "array"
                st["impl"] = "cell_search_cgra"
                st["array_runs"] = int(nd["params"].get("hypotheses", 1))
                st["notes"].append("%d 个假设，每个一次阵列相关" % st["array_runs"])
            else:
                st["where"] = "array"
                st["impl"] = {"zc": "prach_corr", "pss": "pss_corr",
                              "sss": "sss_corr"}.get(seq, "prach_corr")
                st["array_runs"] = int(nd["params"].get("hypotheses", 1))
                st["notes"].append("%d 个候选假设，每个一次阵列运行" % st["array_runs"])

        elif op == "mimo_equalise":
            # 每层每 RE 出 qm 个硬判比特；mmse_2x1 tile 一次算 n_pes 个 RE、
            # 一次运行只做一个层，所以运行数 = ceil(RE/n_pes) * 层数。
            qm = int(nd["params"].get("qm") or 2)
            layers = int(nd["params"].get("layers", 2))
            nbits = nd.get("length") or 0
            nre = max(1, nbits // max(1, qm * layers))
            tiles = (nre + target["n_pes"] - 1) // target["n_pes"]
            st["where"] = "array"
            st["impl"] = "mmse_2x1"
            st["array_runs"] = tiles * layers
            st["notes"].append("%d 个数据 RE / %d 层，tile 每次 %d 个 RE"
                               % (nre, layers, target["n_pes"]))

        elif op == "dmrs":
            # 参考序列的种类决定实现（SSB 里 PSS/SSS/PBCH 三种参考同时存在，
            # 只有 PBCH 那种在建 Gold 序列；另外两种由相关搜索自己生成）。
            st["where"] = "control-core"
            st["impl"] = "ref_%s" % (nd["params"].get("type") or "generic")

        elif op == "estimate":
            # 自环发射（有 ofdm_modulate、但没有 ofdm_demodulate）时，阶段实现
            # 在 estimate 内部做接收 FFT；计划必须把这份阵列工作算进去。
            has_demod = any(n["op"] == "ofdm_demodulate" and n["id"] < nd["id"]
                            for n in ir["nodes"])
            has_mod = any(n["op"] == "ofdm_modulate" and n["id"] < nd["id"]
                          for n in ir["nodes"])
            has_td = any(n["op"] == "received"
                         and str(n["params"].get("form") or "") in ("td", "td2", "td4")
                         and n["id"] < nd["id"] for n in ir["nodes"])
            if (not has_demod) and (has_mod or has_td) and need is not None:
                cand, cands = fft_candidates(need, cp_len)
                if cand is None:
                    st["where"] = "control-core"
                else:
                    runs = cand["runs_per_symbol"] * nsym
                    st["where"] = "array"
                    st["impl"] = cand["impl"]
                    st["array_runs"] = runs
                    st["tiling"] = {
                        "size": cand["size"],
                        "symbols": nsym,
                        "tiles_per_symbol": cand["runs_per_symbol"],
                        "tiles_total": runs,
                    }
                    st["buffer_words"] = cand["buffer_words_per_symbol"] * min(nsym, 2)
                    st["search"] = {
                        "candidates": [c["impl"] for c in cands],
                        "chosen": cand["impl"],
                        "rule": "min runs; tie min size",
                    }
                    st["notes"].append(
                        "实现库在 estimate 内做 FFT（自环接收，与 ofdm_modulate 同口径）")
                    plan["search"]["fft"].append(
                        {"stage": nd["id"], "op": op, "need": need,
                         "nsym": nsym, "candidates": cands,
                         "chosen": cand["impl"], "runs": runs, "hidden": True})
            else:
                st["where"] = "control-core"

        elif op in ("channel", "equalise_demap", "extract", "received", "measure"):
            st["where"] = "control-core"

        # 新增镜像的显式落点开关（默认关闭，见文件头的取舍说明）。
        # 放在所有既有分支之后，保证 USE_NEW_ARRAY=0 时 plan 逐字节不变。
        if USE_NEW_ARRAY:
            key = None
            if op == "modulate":
                key = "modulate"
            elif op == "encode" and nd["params"].get("scheme") == "ldpc":
                key = "ldpc_encode"
            elif op in ("encode", "decode") and nd["params"].get("scheme") == "turbo":
                key = "turbo"
            elif op in ("encode", "decode") and nd["params"].get("scheme") == "ldpc_wifi":
                key = "ldpc_wifi"
            if key:
                images = NEW_ARRAY_IMAGES[key]
                st["where"] = "array"
                st["impl"] = images[0]
                st["array_runs"] = 1
                st["notes"].append(
                    "PROTO_NEW_ARRAY=1：改用新增镜像 %s（%s）；"
                    "默认落点见 docs/22 实测结论" % (images[0], images[1]))
                plan["search"].setdefault("new_array", []).append(
                    {"stage": nd["id"], "op": op, "images": images[1]})

        plan["stages"].append(st)

    # 语义图必须与目标无关（docs/20 §4）：这里只做自检，不修改 IR
    plan["ir_sha"] = __import__("hashlib").sha256(
        json.dumps(ir, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return plan, plan["ok"]


def dumps(plan):
    return json.dumps(plan, sort_keys=True, indent=1, ensure_ascii=False)
