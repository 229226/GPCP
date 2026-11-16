#!/usr/bin/env python3
# --- tools path bootstrap (subdirs import root modules) ---
import os as _os, sys as _sys
_T = _os.path.dirname(_os.path.abspath(__file__))
_TOOLS = _T if _os.path.basename(_T) == "tools" else _os.path.dirname(_T)
for _p in (_TOOLS, _os.path.join(_TOOLS, "models"), _os.path.join(_TOOLS, "checks"),
           _os.path.join(_os.path.dirname(_TOOLS), "cgra", "tools")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
_ROOT = _os.path.dirname(_TOOLS)
# --- end bootstrap ---
"""Input-fingerprint cache for the GPCP acceptance gates.

Rule (docs: AGENTS.md section 8 "regression tiers"):

    A gate's result is a pure function of its inputs.  If every input is
    byte-identical to the last recorded PASS, the result cannot have changed,
    so the gate is skipped and reported as SKIP (never as PASS).

Inputs are discovered, not guessed:
  * GLOBAL  - architecture / ABI / toolchain files.  Any change here
              invalidates EVERY gate (RTL, simulator, linker script, CGRA
              microcode headers, both makefiles, the runner itself).
  * per-gate - for SoC gates, the tools/*.py scripts named in that target's
              recipe in sim/soc/Makefile plus their local import closure, the
              sw/soc C source that produces its binary, and the binary itself.
              For model gates, the script and its local import closure.
              kernels only depends on GLOBAL.

The cache lives in build/verify-cache/ (gitignored).  It is a DEVELOPMENT
shortcut only: a stage-end / pre-commit / release run must use --force.

Usage:
    python3 tools/verify_cache.py plan [--force] [--gates "a b c"]
    python3 tools/verify_cache.py mark <gate> <rc>
    python3 tools/verify_cache.py show
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "build" / "verify-cache"
SALT = "gpcp-gate-v1"

# ---- architecture / ABI / toolchain: a change invalidates every gate -------
GLOBAL_GLOBS = [
    "rtl/**/*.v", "rtl/**/*.sv", "rtl/**/*.vh",
    "sim/**/*.v", "rtl/**/*.svh", "sim/**/*.cpp", "sim/**/*.cc", "sim/**/*.h",
    "sim/**/*.py", "cgra/**/*.py", "cgra/**/*.v", "cgra/**/*.sv",
    "cgra/target.yaml",          # 硬件真相：改它等于换芯片，全部失效
    "sw/soc/Makefile", "sw/soc/link.ld", "sw/soc/start.S", "sw/soc/start_irq.S",
    "sw/include/*.h", "sw/soc/gen/*.h",
    "sw/soc/stages/*.inc", "sw/soc/ops/*.inc", "sw/soc/lib/*.inc",
    "sim/soc/Makefile", "Makefile",
    "tools/cgra.py", "tools/verify.sh", "tools/verify_cache.py",
    "build/fft/twiddle.bin", "sim/soc/build/Vgpcp_soc",
]

# Targets whose name is parameterized (`run-proto-$(PROTO)`) cannot be found by
# the recipe parser above, so their inputs are listed explicitly. Keep this in
# sync when a scenario gains a new model module.
GATE_EXTRA = {
    "run-proto-nr_pdcch": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                           "sw/soc/stages/pdcch_stages.inc", "sw/soc/ops/nr_pdcch_ops.inc"],
    "run-proto-nr_pdsch": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                           "sw/soc/stages/pdsch_stages.inc", "sw/soc/ops/nr_pdsch_ops.inc",
                           "sw/soc/lib/ldpc_enc.inc"],
    "run-proto-nr_csi": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                         "sw/soc/stages/csi_stages.inc", "sw/soc/ops/nr_csi_ops.inc"],
    "run-proto-nr_prach": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                           "sw/soc/lib/zc_corr.inc", "sw/soc/ops/nr_prach_ops.inc"],
    "run-proto-nr_pucch": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                           "sw/soc/lib/zc_corr.inc", "sw/soc/ops/nr_pucch_ops.inc"],
    "run-proto-nr_mimo": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                          "sw/soc/stages/mimo_stages.inc", "sw/soc/ops/nr_mimo_ops.inc"],
    "run-proto-nr_pusch": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                           "sw/soc/stages/pusch_stages.inc", "sw/soc/ops/nr_pusch_ops.inc",
                           "sw/soc/lib/ldpc_enc.inc"],
    "run-proto-nr_ssb": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                         "sw/soc/stages/ssb_stages.inc", "sw/soc/ops/nr_ssb_ops.inc",
                         "sw/soc/lib/ssb_fft.inc", "tools/ssb_fid_luts.h"],
    "run-proto-lte_pdsch": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                            "sw/soc/stages/lte_stages.inc", "sw/soc/ops/lte_pdsch_ops.inc"],
    "run-proto-nr_pdsch_16qam": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                                 "sw/soc/stages/pdsch_stages.inc", "sw/soc/ops/nr_pdsch_ops.inc",
                                 "sw/soc/ops/nr_pdsch_16qam_ops.inc", "sw/soc/lib/ldpc_enc.inc"],
    "run-proto-nr_pdsch_64qam": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                                 "sw/soc/stages/pdsch_stages.inc", "sw/soc/ops/nr_pdsch_ops.inc",
                                 "sw/soc/ops/nr_pdsch_64qam_ops.inc", "sw/soc/lib/ldpc_enc.inc"],
    "run-proto-nr_pdsch_harq": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                                "sw/soc/stages/pdsch_harq_stages.inc", "sw/soc/stages/pdsch_stages.inc",
                                "sw/soc/ops/nr_pdsch_harq_ops.inc", "sw/soc/lib/ldpc_enc.inc"],
    "run-proto-nr_pusch_tp": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                              "sw/soc/stages/pusch_stages.inc", "sw/soc/ops/nr_pusch_ops.inc",
                              "sw/soc/ops/nr_pusch_tp_ops.inc", "sw/soc/lib/ldpc_enc.inc"],
    "run-proto-lte_pusch": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                            "sw/soc/stages/lte_stages.inc", "sw/soc/stages/lte_ul_stages.inc",
                            "sw/soc/gen/lte_ul_dft200_tab.h", "sw/soc/ops/lte_pusch_ops.inc"],
    "run-proto-lte_prach": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                            "sw/soc/lib/zc_corr.inc", "sw/soc/ops/nr_prach_ops.inc",
                            "sw/soc/ops/lte_prach_ops.inc"],
    "run-proto-lte_cell_search": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                                  "sw/soc/stages/lte_cell_stages.inc", "sw/soc/gen/lte_pss_tab.h",
                                  "sw/soc/ops/lte_cell_search_ops.inc"],
    "run-proto-wifi_11be": ["proto/**/*.py", "sw/soc/bench/proto_runner.c",
                            "sw/soc/stages/wifi_stages.inc", "sw/soc/gen/wifi_cos256_tab.h",
                            "sw/soc/gen/wifi_ldpc_648_r12.h", "sw/soc/ops/wifi_11be_ops.inc",
                            "tools/models/wifi_ldpc.py"],
}

# Binary -> C source for the few targets whose binary is not named after its
# source (they are compiled from a shared .c with -D switches).
BIN_SRC_ALIAS = {
    "pdsch_16qam_bm": "pdsch_full_bm.c",
    "pdsch_64qam_bm": "pdsch_full_bm.c",
    "pdsch_harq_bm": "pdsch_full_bm.c",
    "sib1_bm": "link_e2e_bm.c",
    "rach_bm": "rach_bm.c",
}


# Explicitly named build products ARE inputs (the simulator binary and the
# twiddle LUT); everything else under sim/*/build/ is an output.
KEEP_PRODUCTS = {"sim/soc/build/Vgpcp_soc", "build/fft/twiddle.bin"}


def expand(patterns):
    out = set()
    for pat in patterns:
        for p in glob.glob(str(ROOT / pat), recursive=True):
            rel = os.path.relpath(p, str(ROOT))
            if (rel not in KEEP_PRODUCTS and rel.startswith("sim/")
                    and "/build/" in rel):
                continue
            if os.path.isfile(p):
                out.add(rel)
    return out


def local_import_closure(scripts):
    """Add tools/<module>.py for every local import, transitively."""
    seen = set(scripts)
    queue = list(scripts)
    while queue:
        rel = queue.pop()
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in re.finditer(r"^\s*(?:from|import)\s+([a-zA-Z_][\w]*)",
                             text, re.M):
            cand = "tools/%s.py" % m.group(1)
            if (ROOT / cand).is_file() and cand not in seen:
                seen.add(cand)
                queue.append(cand)
    return seen


def soc_gate_inputs(gate):
    """Parse that target's recipe out of sim/soc/Makefile."""
    files = set(expand(GATE_EXTRA.get(gate, [])))
    recipe = []
    inside = False
    for line in (ROOT / "sim/soc/Makefile").read_text().splitlines():
        if re.match(r"^%s\s*:" % re.escape(gate), line):
            inside = True
        elif inside and line and not line.startswith(("\t", " ")):
            break
        if inside:
            recipe.append(line)
    body = "\n".join(recipe)
    # recipe text itself (make variables / flags / addresses are inputs)
    files.add("sim/soc/Makefile")
    scripts = {"tools/%s" % m for m in
               re.findall(r"tools/([a-z0-9_]+\.py)", body)}
    files |= local_import_closure(scripts)
    bins = re.findall(r"build/sw/([A-Za-z0-9_.$()/-]*?\.bin)", body)
    bins = [b.split("@")[0] for b in bins]
    if not bins:
        # e.g. run-rach names its binary with $(OCC) inline, which the regex
        # cannot resolve.  Be conservative rather than miss a source change.
        files |= expand(["sw/soc/bench/*.c", "sw/soc/gen/*.h", "sw/soc/**/*.inc", "sw/soc/*.S"])
    for b in bins:
        stem = b.replace(".bin", "")
        files.add("build/sw/%s" % b)
        src = BIN_SRC_ALIAS.get(stem)
        if src is None:
            # rach_bm_1.bin -> rach_bm.c
            stem = re.sub(r"_\d+$", "", stem)
            src = stem + ".c"
        cand = "sw/soc/bench/%s" % src
        if (ROOT / cand).is_file():
            files.add(cand)
        else:
            # Unknown mapping: be conservative, watch every bare-metal source.
            files |= expand(["sw/soc/bench/*.c", "sw/soc/gen/*.h", "sw/soc/**/*.inc", "sw/soc/*.S"])
    return files


def model_gate_inputs(gate):
    table = {
        "model-pdcch": "nr_pdcch.py", "model-ldpc": "nr_ldpc.py",
        "model-mimo": "nr_mimo.py", "model-mac": "nr_mac.py",
        "model-harq": "pdsch_full_plan.py", "model-ul": "nr_ul.py",
        "model-pusch": "nr_pusch.py", "model-sib1": "nr_sib1.py",
        "model-link": "nr_link.py", "model-rach": "nr_rach.py",
        "model-rrc": "nr_rrc.py", "model-conn": "nr_conn.py",
        "model-pucch": "nr_pucch.py", "model-la": "nr_la.py",
        "model-qam": "nr_qam.py", "model-amc": "nr_pdsch_amc.py",
    }
    script = table.get(gate)
    if script is None:
        return set()
    return local_import_closure({"tools/%s" % script})


def digest_for(gate, global_files):
    if gate.startswith("run-") or gate == "linux":
        own = soc_gate_inputs(gate)
    elif gate.startswith("model-"):
        own = model_gate_inputs(gate)
    else:                                  # kernels
        own = set()
    files = sorted(global_files | own)
    h = hashlib.sha256()
    h.update(SALT.encode())
    h.update(gate.encode())
    for rel in files:
        p = ROOT / rel
        if not p.is_file():
            continue
        h.update(rel.encode())
        h.update(b"\0")
        h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode())
        h.update(b"\n")
    return h.hexdigest(), len(files)


def cache_path(gate):
    return CACHE / ("%s.json" % gate.replace("/", "_"))


def read_cache(gate):
    try:
        return json.loads(cache_path(gate).read_text())
    except (OSError, ValueError):
        return None


def cmd_plan(args):
    CACHE.mkdir(parents=True, exist_ok=True)
    gates = args.gates.split() if args.gates else []
    gfiles = expand(GLOBAL_GLOBS)
    skips = []
    for gate in gates:
        dig, nfiles = digest_for(gate, gfiles)
        hit = read_cache(gate)
        if (not args.force and hit and hit.get("digest") == dig
                and hit.get("rc") == 0):
            print("SKIP %s %s" % (gate, hit.get("ts", "?")))
            skips.append((gate, hit.get("ts", "?"), nfiles))
        else:
            print("RUN %s" % gate)
    (CACHE / "skips.json").write_text(json.dumps(skips, indent=1))
    return 0


def cmd_mark(args):
    CACHE.mkdir(parents=True, exist_ok=True)
    gate, rc = args.gate, args.rc
    gfiles = expand(GLOBAL_GLOBS)
    dig, nfiles = digest_for(gate, gfiles)
    import datetime
    rec = dict(digest=dig, rc=rc, nfiles=nfiles,
               ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    cache_path(gate).write_text(json.dumps(rec, indent=1))
    return 0


def cmd_show(args):
    import datetime
    gfiles = expand(GLOBAL_GLOBS)
    rows = []
    for p in sorted(CACHE.glob("*.json")):
        if p.name == "skips.json":
            continue
        try:
            rec = json.loads(p.read_text())
        except ValueError:
            continue
        gate = p.stem
        dig, _ = digest_for(gate, gfiles)
        rows.append((gate, rec.get("ts", "?"), rec.get("rc"),
                     "valid" if rec.get("digest") == dig and rec.get("rc") == 0
                     else "STALE"))
    print("gate                 last PASS              rc   cache")
    for g, ts, rc, st in rows:
        print("%-20s %-20s %-4s %s" % (g, ts, rc, st))
    print("global input files: %d" % len(gfiles))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--force", action="store_true")
    p.add_argument("--gates", default="")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("mark")
    p.add_argument("gate")
    p.add_argument("rc", type=int)
    p.set_defaults(func=cmd_mark)
    p = sub.add_parser("show")
    p.set_defaults(func=cmd_show)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
