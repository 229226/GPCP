"""把语义 IR 落到某个硬件目标上（docs/20 §3 第 3 步）。

**这是整个方案里唯一允许出现硬件数字的地方**（另一个是 cgra/target.yaml）。
它只做三件事：
  1. 读目标几何（阵列规模 / 存储容量 / 指令内存深度），可用 what-if 补丁覆盖；
  2. 为每个语义节点挑算子实现与切块，算出资源需求；
  3. 判断可行性，不可行就说明"缺什么"，绝不回退成让用户手写坐标。
"""
import json
import re

# 算子库当前能提供的实现（与 cgra/kernels/*.yaml 对应；换库时改这里）
FFT_SIZES = (256, 1024, 4096)
LDPC_ZC = (20, 64)          # cn20/bn24 与 zc64 两套
POLAR_N = 512               # polar 9 级镜像覆盖 512 点
# 经验常数：一次阵列运行的编排开销（T0 台架实测 T(c)=32+1.0045c，取上限估计）
T0_OVERHEAD = 32


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
            "target": target, "stages": [], "ok": True, "needs": []}
    d = ir.get("derived", {})

    for nd in ir["nodes"]:
        op = nd["op"]
        st = {"id": nd["id"], "op": op, "out": nd["out"], "where": "control-core",
              "impl": None, "notes": []}

        if op in ("ofdm_modulate", "ofdm_demodulate"):
            need = d.get("n_fft_min")
            if need is None:
                st["notes"].append("cfg 未给出带宽/子空间隔，无法推导 FFT 点数")
                plan["ok"] = False
                plan["needs"].append("cfg(bw_mhz, scs_khz)")
            else:
                size, over = _pick(need, FFT_SIZES)
                if size is None:
                    plan["ok"] = False
                    plan["needs"].append("FFT >= %d 的算子实现" % need)
                else:
                    st["where"] = "array"
                    st["impl"] = "fft%d" % size
                    st["over_provision"] = round(over, 3)
                    if over > 1.0:
                        st["notes"].append(
                            "最小需要 %d 点，算子库只有 %d 点 → 空转 %.2fx"
                            % (need, size, over))
                    tiles = max(1, size // 1024)
                    st["array_runs"] = tiles
                    if 448 + T0_OVERHEAD > target["imem_cycles"]:
                        plan["ok"] = False
                        plan["needs"].append("IMEM >= 512 拍（FFT 镜像 448 拍）")

        elif op == "decode" and nd["params"].get("scheme") == "polar":
            st["where"] = "array"
            st["impl"] = "polar_node@%d" % POLAR_N
            st["array_runs"] = 128
            st["notes"].append("SC 树 127 个内部节点，每节点一次阵列运行")

        elif op == "encode" and nd["params"].get("scheme") == "polar":
            st["where"] = "array"
            st["impl"] = "polar9@%d" % POLAR_N
            st["array_runs"] = 9
            st["notes"].append("9 级变换，512 点由 16 PE 并行")

        elif op in ("encode", "decode") and nd["params"].get("scheme") == "ldpc":
            # 38.212：BG2 的码字长 N = 52*Zc，信息位 K = Kb*Zc（K<=640 时 Kb=6）。
            # encode 节点带的是编码后比特数，decode 节点带的是 TB 比特数，两条路
            # 都推回同一个 Zc 需求，而不是拿"比特数 / 52 + 1"去猜。
            if op == "encode":
                zc_need = max(1, (nd.get("length") or 0) // 52)
            else:
                k_info = (d.get("a_bits") or 0) + 24        # TB + CRC24
                zc_need = max(1, -(-k_info // 6))
            zc, over = _pick(zc_need, LDPC_ZC)
            if zc is None:
                plan["ok"] = False
                plan["needs"].append("LDPC 提升长度 >= %d 的实现" % zc_need)
            else:
                st["where"] = "control-core" if op == "encode" else "array"
                st["impl"] = "ldpc_bg2_zc%d" % zc
                words = 26 * zc
                st["spad_words"] = words
                st["notes"].append("每块 %d 字（26 槽 x zc）" % words)
                if words > n_spad_words:
                    plan["ok"] = False
                    plan["needs"].append("spad >= %d 字（当前 %d）"
                                         % (words, n_spad_words))
                if op == "decode":
                    iters = nd["params"].get("max_iter", 8)
                    st["array_runs"] = iters * 94

        elif op in ("channel", "estimate", "equalise_demap", "extract"):
            st["where"] = "control-core"

        plan["stages"].append(st)

    # 语义图必须与目标无关（docs/20 §4）：这里只做自检，不修改 IR
    plan["ir_sha"] = __import__("hashlib").sha256(
        json.dumps(ir, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    return plan, plan["ok"]


def dumps(plan):
    return json.dumps(plan, sort_keys=True, indent=1, ensure_ascii=False)
