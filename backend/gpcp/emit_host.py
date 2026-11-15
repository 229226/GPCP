# -*- coding: utf-8 -*-
"""TaskPlan -> host C：生成「启动阵列 / 搬数据 / 翻双缓冲」的编排代码。

生成物只有编排，没有算法：算法侧的动作以 `hook` 的形式原样保留在 C 里。
好处是调度变成数据——改调度改 plan，不改 C。

命名约定：生成函数叫 `task_<stage>()`，返回 0/-1；错误统一累加 `errs`，
阵列运行次数累加 `runs`（与手写实现同一套计数器，方便逐位对账）。
"""

HEADER = """/* 生成物：backend/gpcp/emit_host.py 从 TaskPlan 生成，勿手改。
 * stage=%s impl=%s imem_reload=%s
 * 编排（何时启动阵列 / 数据怎么搬 / 双缓冲怎么翻）由 plan 决定；
 * 算法侧动作以 hook 原样嵌入。
 */
#ifndef GPCP_TASK_%s_INC
#define GPCP_TASK_%s_INC

static int task_%s(void)
{
"""

FOOTER = """    return 0;
}

#endif /* GPCP_TASK_%s_INC */
"""


def _imem(prog, cycles, ind="    "):
    return ("%sif (gpcp_cgra_imem_load(CGRA_BASE, %s, %s * NPES))\n"
            "%s    errs++;\n" % (ind, prog, cycles, ind))


def _stmt(it, ind):
    """单条 tile 语句 -> C。"""
    (k, v), = it.items()
    if k == "array":
        return ("%sif (rt_array_run(CGRA_BASE, %s, %s)) errs++;\n"
                "%sruns++;\n"
                "#ifdef PS_TRACK_STATS\n"
                "%sbusy += %s;\n"
                "#endif\n" % (ind, v["page"], v["cycles"], ind, ind, v["cycles"]))
    if k == "wait":
        return "%sif (rt_dma_wait(CGRA_BASE)) errs++;\n" % ind
    if k == "wait_if":
        return "%sif (%s) { if (rt_dma_wait(CGRA_BASE)) errs++; }\n" % (ind, v)
    if k == "hook":
        v = v.rstrip()
        return "%s%s\n" % (ind, v if v.endswith(";") else v + ";")
    if k in ("dma_in", "dma_out"):
        args = [v["arg"]]
        if v.get("page") is not None:
            args.append(v["page"])
        if v.get("extra"):
            args.append(v["extra"])
        call = "%s(%s);" % (v["fn"], ", ".join(args))
        if v.get("if"):
            return "%sif (%s) %s\n" % (ind, v["if"], call)
        return ind + call + "\n"
    raise ValueError("未知语句 %s" % k)


def _phase(ph, ind, knobs):
    out = []
    out.append("%s/* ---- phase %s：%s 个 tile ---- */\n"
               % (ind, ph.get("name", "?"), ph["tiles"]))
    for d in ph.get("declare", []):
        out.append(ind + d + "\n")
    pro = ph.get("prologue")
    if pro:
        if "dma_in" in pro:
            out.append(_stmt({"dma_in": pro["dma_in"]}, ind))
        if pro.get("wait"):
            out.append(_stmt({"wait": True}, ind))
    out.append("%sfor (uint32_t i = 0; i < %s; i++) {\n" % (ind, ph["tiles"]))
    body = ind + "    "
    pre = ph.get("prefetch")
    if pre and "dma_in" in pre:
        out.append(_stmt({"dma_in": pre["dma_in"]}, body))
    for it in ph.get("item", []):
        out.append(_stmt(it, body))
    out.append("%s}\n" % ind)
    for e in ph.get("epilogue", []):
        out.append(ind + e + ";\n")
    return "".join(out)


def render(plan):
    stage = plan["stage"]
    knobs = plan.get("knobs") or {}
    up = stage.upper()
    L = [HEADER % (stage, plan.get("impl", "-"),
                   knobs.get("imem_reload", "-"), up, up, stage)]

    # setup：spad 填充 / IMEM 首次装载
    for st in plan.get("setup", []):
        if "fill_spad" in st:
            f = st["fill_spad"]
            L.append("    for (uint32_t i = 0; i < %s; i++)\n"
                     "        mmio_w(CGRA_BASE + GPCP_SPAD_ADDR(%s, i), "
                     "(uint32_t)%s);\n" % (f["words"], f["bank"], f["value"]))
        elif "imem" in st:
            L.append(_imem(st["imem"]["prog"], st["imem"]["cycles"]))
    L.append("\n")

    loop = plan.get("loop") or {}
    if loop:
        L.append("    for (uint32_t %s = 0; %s < %s; %s++) {\n"
                 % (loop["var"], loop["var"], loop["count"], loop["var"]))
        for node in loop["body"]:
            if "imem" in node:
                if node["imem"].get("reload", "iteration") == "iteration" \
                        and knobs.get("imem_reload", "iteration") == "iteration":
                    L.append(_imem(node["imem"]["prog"],
                                   node["imem"]["cycles"], "        "))
            elif "phase" in node:
                L.append(_phase(node["phase"], "        ", knobs))
        L.append("    }\n")

    L.append(FOOTER % up)
    return "".join(L)


if __name__ == "__main__":
    import json
    import sys
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from taskplan import decode_plan, validate
    p = decode_plan(sys.argv[1] if len(sys.argv) > 1 else "iteration")
    bad = validate(p)
    if bad:
        sys.exit("plan 校验失败: " + "; ".join(bad))
    sys.stdout.write(render(p))
