# -*- coding: utf-8 -*-
"""阶段级 TaskPlan：把「什么时候启动阵列、数据怎么搬、双缓冲怎么翻」变成数据。

现在只覆盖 LDPC min-sum 迭代（`decode`），因为它的编排占全部编排的 63%。
其余阶段仍是手写 C；生成器先证明「逐位等价」，再让 knob 改调度。

一份 plan 的语义
----------------
- `setup`     ：进入阶段前的一次性动作（spad 填哨兵、IMEM 装载）
- `loop`      ：外层迭代（LDPC 的 8 次 min-sum 迭代）
- `phase`     ：一段按 tile 重复的流水，tile 数由 `tiles` 给
  - `prologue`：进循环前先把第 0 个 tile 的输入搬进来并等待
  - `prefetch`：循环体内、计算之前发起的下一个 tile 的输入搬运
  - `item`    ：每个 tile 要做的事，按顺序执行（array → wait → dma_out → …）
  - `epilogue`：循环之后的收尾调用（缓存 flush、gather 等）

`item` 里的语句只允许这几种，生成器负责循环、下标、守卫、计数：
  {"array": {cycles, page}}          启动阵列并等到完成
  {"dma_in"/"dma_out": {fn, arg, page, extra, if}}
  {"wait": True} / {"wait_if": "expr"}
  {"hook": "C 语句"}                  算法侧的钩子（如 bn_scatter）
"""

SCHEMA = "gpcp-taskplan/1"


def decode_plan(imem_reload="iteration"):
    """LDPC BG2 min-sum：8 次迭代 × (42 行 CN + 52 列 BN)，page = i & 1 双缓冲。

    `imem_reload` 是调度旋钮：
      "iteration" —— 每个迭代重装 CN 微码，随后装 BN 微码（**必须**）
      "once"      —— 只在进入阶段时装一次

    注意：`once` 是错的，已验证会被 golden 挡住（tb_bad=43、码字 467/1040 不同）。
    原因是 CN 与 BN 两套微码在同一迭代里交替驻留：迭代结束时装的是 BN，
    下一次迭代如果不重新装 CN，就会用错微码跑出错误结果。
    这个旋钮保留只为记录这条结论——不要在生产配置里用。
    """
    return {
        "schema": SCHEMA,
        "stage": "decode",
        "impl": "ldpc_bg2_zc20",
        "knobs": {"imem_reload": imem_reload},
        "setup": [
            {"fill_spad": {"bank": "SENTINEL_BANK", "words": "ZC",
                           "value": "SENTINEL"}},
            {"fill_spad": {"bank": "SENTINEL_BANK + GPCP_LAYOUT_PAGE_BANKS",
                           "words": "ZC", "value": "SENTINEL"}},
            {"imem": {"prog": "cn_prog", "cycles": "CN_PROG_CYCLES"}},
        ],
        "loop": {
            "var": "it", "count": "qs_decode_iters",
            "body": [
                {"imem": {"prog": "cn_prog", "cycles": "CN_PROG_CYCLES",
                          "reload": "iteration"}},
                {"phase": {
                    "name": "cn", "tiles": "BG2_ROWS",
                    "prologue": {"dma_in": {"fn": "cn_row_in", "arg": "0",
                                            "page": "0"},
                                 "wait": True},
                    "prefetch": {"dma_in": {"fn": "cn_row_in", "arg": "i + 1",
                                            "page": "(i + 1) & 1u",
                                            "if": "i + 1 < BG2_ROWS"}},
                    "item": [
                        {"array": {"cycles": "CN_PROG_CYCLES", "page": "i & 1u"}},
                        {"wait_if": "i + 1 < BG2_ROWS"},
                        {"dma_out": {"fn": "cn_row_out", "arg": "i",
                                     "page": "i & 1u"}},
                        {"wait": True},
                    ],
                    "epilogue": ["rt_dcache_flush()", "reconstruct_gather()"],
                }},
                {"imem": {"prog": "bn_prog", "cycles": "BN_PROG_CYCLES",
                          "reload": "iteration"}},
                {"phase": {
                    "name": "bn", "tiles": "BG2_COLS",
                    "declare": ["int want_hard = (it + 1u == qs_decode_iters);"],
                    "prologue": {"dma_in": {"fn": "bn_dma_in", "arg": "0"},
                                 "wait": True},
                    "item": [
                        {"array": {"cycles": "BN_PROG_CYCLES", "page": "0"}},
                        {"dma_out": {"fn": "bn_dma_out", "arg": "i",
                                     "extra": "want_hard"}},
                        {"wait": True},
                        {"dma_in": {"fn": "bn_dma_in", "arg": "i + 1",
                                    "if": "i + 1 < BG2_COLS"}},
                        {"hook": "bn_scatter(i, want_hard, qs_hard[i]);"},
                        {"wait_if": "i + 1 < BG2_COLS"},
                    ],
                }},
            ],
        },
    }


def validate(plan):
    """生成前的最低限度自检：语句种类、必填字段。"""
    ok = {"array", "dma_in", "dma_out", "wait", "wait_if", "hook"}
    problems = []
    if plan.get("schema") != SCHEMA:
        problems.append("schema 不是 %s" % SCHEMA)

    def check_item(it, where):
        if len(it) != 1:
            problems.append("%s: 语句必须是单键字典 %r" % (where, it))
            return
        k = next(iter(it))
        if k not in ok:
            problems.append("%s: 未知语句 %s" % (where, k))
        if k in ("dma_in", "dma_out"):
            for f in ("fn", "arg"):
                if f not in it[k]:
                    problems.append("%s: %s 缺 %s" % (where, k, f))

    for st in plan.get("setup", []):
        pass
    body = (plan.get("loop") or {}).get("body") or []
    for node in body:
        if "phase" in node:
            ph = node["phase"]
            for it in ph.get("item", []):
                check_item(it, "phase " + ph.get("name", "?"))
            if "prologue" in ph:
                for k in ("dma_in",):
                    if k in ph["prologue"]:
                        check_item({k: ph["prologue"][k]}, "prologue")
    return problems


if __name__ == "__main__":
    import json
    import sys
    p = decode_plan(sys.argv[1] if len(sys.argv) > 1 else "iteration")
    print(json.dumps(p, indent=1, ensure_ascii=False))
