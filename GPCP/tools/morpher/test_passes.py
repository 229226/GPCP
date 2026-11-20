#!/usr/bin/env python3
"""L2-B / L2-C 纯函数合成测试（不依赖 Morpher 的 array_add，那个端到端验收见 L3）。

- L2-B fold_addresses：把 `base + k*stride` 常量链折叠成 imm[9:0]。
- L2-C route_links：把「同拍、但距离 >1 PE」的生产者用多跳 link 路由（旧 translate 只认同 PE/四邻）。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cc  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------- L2-B 折叠
def test_fold():
    # base=5, stride=4, i=3  -> addr = 5 + 4*3 = 17  (bank0,word17)
    nodes = {
        0: {"op": "const", "val": 5, "t": 0, "x": 0, "y": 0},
        1: {"op": "const", "val": 4, "t": 0, "x": 0, "y": 0},
        2: {"op": "const", "val": 3, "t": 0, "x": 0, "y": 0},
        3: {"op": "mul", "t": 0, "x": 0, "y": 0},
        4: {"op": "add", "t": 0, "x": 0, "y": 0},
        5: {"op": "ld", "t": 1, "x": 0, "y": 0},
    }
    preds = {
        3: [(1, 0), (2, 1)],
        4: [(0, 0), (3, 1)],
        5: [(4, 0)],
    }
    folded = cc.fold_addresses(nodes, preds, ii=2)
    check("L2-B 折叠 base+stride*i -> imm", folded.get(5) == 17,
          f"folded={folded}")
    # 非全常量：地址依赖运行时变量 -> 不折叠
    nodes2 = dict(nodes)
    nodes2[2] = {"op": "ld", "t": 0, "x": 0, "y": 0}  # i 变成运行时 ld
    preds2 = {k: list(v) for k, v in preds.items()}
    folded2 = cc.fold_addresses(nodes2, preds2, ii=2)
    check("L2-B 运行时变量不折叠（仍报 no_addr）", 5 not in folded2,
          f"folded={folded2}")


# ---------------------------------------------------------------- L2-C 多跳
def test_route():
    # 生产者 PE(0,0) t0；消费者 PE(0,2) t1（曼哈顿距离 2，超四邻）
    # 注意：cc.py 的 OP_MAP 用 Morpher 大写算子名（ADD/...），测试对齐
    nodes = {
        0: {"op": "ADD", "t": 0, "x": 0, "y": 0},
        1: {"op": "ADD", "t": 1, "x": 0, "y": 2},
    }
    preds = {1: [(0, 0)]}
    words, rep = cc.route_links(nodes, preds, ii=2, max_hops=4)
    enc_idx = {e["idx"] for e in rep["encoded"]}
    check("L2-C 2 跳生产者被路由（不再 no_route）",
          1 in enc_idx and rep["moved"] == 1 and not rep["skipped"],
          f"encoded={enc_idx} moved={rep['moved']} skipped={rep['skipped']}")
    # 对照：旧 translate 对同样输入会报 no_route
    w0, r0 = cc.translate(nodes, preds, ii=2)
    check("旧 translate 对 2 跳仍 no_route（证明 route_links 是增量能力）",
          r0["skipped"].get("no_route") == [1], f"skipped={r0['skipped']}")
    # 同 PE 仍走寄存器
    nodes2 = {0: {"op": "ADD", "t": 0, "x": 1, "y": 1},
              1: {"op": "ADD", "t": 1, "x": 1, "y": 1}}
    preds2 = {1: [(0, 0)]}
    _, r2 = cc.route_links(nodes2, preds2, ii=2)
    check("L2-C 同 PE 走寄存器（无 moved）",
          r2["moved"] == 0 and 1 in {e["idx"] for e in r2["encoded"]}
          and not r2["skipped"],
          f"moved={r2['moved']} encoded={[e['idx'] for e in r2['encoded']]} "
          f"skipped={r2['skipped']}")


if __name__ == "__main__":
    print("L2-B / L2-C 纯函数合成测试")
    test_fold()
    test_route()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
