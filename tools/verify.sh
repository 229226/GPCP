#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# GPCP acceptance runner - one command that proves the baseline still holds.
#
#   bash tools/verify.sh quick       # ~2.5 min (146 s): kernels + 10 SoC targets
#   bash tools/verify.sh full        # ~8-10 min: + 16 model self-tests + 29 SoC
#   bash tools/verify.sh slow        # ~10 min (601 s): full + run-rach (11.8 M cycles)
#   bash tools/verify.sh changed     # ~0.5-3 min: only gates touched by git diff
#   bash tools/verify.sh smart       # RECOMMENDED: all 47 gates, but only re-run
#                                   # gates whose inputs changed since the last
#                                   # PASS (input-fingerprint cache); usually a
#                                   # few seconds.  SKIP is never reported as PASS.
#
#   bash tools/verify.sh smart --force    # ignore the cache (stage-end / release)
#   GATES="model-la run-ring" bash tools/verify.sh smart   # manual gate override
#   bash tools/verify.sh full --linux          # also boots Linux (10^8 cycles)
#   bash tools/verify.sh full --jobs 8         # override parallelism (default 6)
#   CHANGED_REF=HEAD~3 bash tools/verify.sh changed
#   CHANGED_ONLY=1     bash tools/verify.sh changed  # never fall back to full
#
# Every gate is an existing make target / python self-test.  Gates are
# independent Verilator simulations (each writes its own *_dump.txt), so they
# run in parallel.  One serial prebuild produces all binaries and the shared
# twiddle LUT; per-target output is buffered and folded back into
# build/verify.log in submission order, so logs stay deterministic.
# Exit code 0 iff every gate passed.
#
# NOTE: LD_LIBRARY_PATH must be cleared for Verilator and its models on this
# machine (the toolchain ships an older libstdc++), hence `env -u`.
# -----------------------------------------------------------------------------
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
MODE=${1:-quick}
shift || true
WITH_LINUX=0
JOBS=${JOBS:-6}
CHANGED_REF=${CHANGED_REF:-HEAD~1}
CHANGED_ONLY=${CHANGED_ONLY:-0}
GATE_OVERRIDE=${GATES:-}
FORCE=0
CACHE_ON=0
while [ $# -gt 0 ]; do
    case "$1" in
        --linux) WITH_LINUX=1; shift ;;
        --slow)  [ "$MODE" = full ] && MODE=slow; shift ;;
        --force) FORCE=1; shift ;;
        --jobs)  JOBS=${2:-}; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
case "$MODE" in quick|full|slow|changed|smart) ;; *) echo "unknown mode: $MODE" >&2; exit 2 ;; esac
[ "$MODE" = smart ] && CACHE_ON=1
case "$JOBS" in ''|*[!0-9]*) echo "JOBS must be a positive number" >&2; exit 2 ;; esac
[ "$JOBS" -ge 1 ] || JOBS=1

LOG="$ROOT/build/verify.log"
mkdir -p "$ROOT/build"
: > "$LOG"

PASS=0
FAIL=0
FAILED_NAMES=""
START=$(date +%s)
TMPD=$(mktemp -d)
trap 'rm -rf "$TMPD"' EXIT

# ---- gate lists -------------------------------------------------------------
ALL_MODELS=(model-pdcch model-ldpc model-mimo model-mac model-harq model-ul
            model-pusch model-sib1 model-link model-rach model-rrc model-conn
            model-pucch model-la model-qam model-amc model-reg model-pc)
declare -A MODEL_CMD=(
    [model-pdcch]="python3 tools/nr_pdcch.py"
    [model-ldpc]="python3 tools/nr_ldpc.py"
    [model-mimo]="python3 tools/nr_mimo.py"
    [model-mac]="python3 tools/nr_mac.py"
    [model-harq]="python3 tools/pdsch_full_plan.py selftest-harq --snr 20 --seed 1"
    [model-ul]="python3 tools/nr_ul.py"
    [model-pusch]="python3 tools/nr_pusch.py selftest"
    [model-sib1]="python3 tools/nr_sib1.py selftest"
    [model-link]="python3 tools/nr_link.py --trials 2"
    [model-rach]="python3 tools/nr_rach.py"
    [model-rrc]="python3 tools/nr_rrc.py"
    [model-conn]="python3 tools/nr_conn.py"
    [model-pucch]="python3 tools/nr_pucch.py selftest"
    [model-la]="python3 tools/nr_la.py"
    [model-qam]="python3 tools/nr_qam.py"
    [model-amc]="python3 tools/nr_pdsch_amc.py selftest"
    [model-reg]="python3 tools/nr_pdcch_reg.py selftest"
    [model-pc]="python3 tools/nr_pc.py selftest"
)
SOCCORE_QUICK="run-pbch-pack run-ifft run-overhead run-ssb-tx run-ldpc-enc run-dl-mod run-pdcch-tx run-ring run-rxchain run-pdsch run-pdsch-bn1 run-pdsch-gather run-pdsch-bg2 run-fft run-ssbfft run-ssb-fid run-ssb-slot"
# run-rach is the single longest gate (~9 min, 11.8 M cycles); it lives in the
# `slow` tier so the default full run stays ~10-12 min.
SOCCORE_FULL="run run-pbch-tx run-pbch-pack run-ifft run-overhead run-ssb-tx run-ldpc-enc run-dl-mod run-pdcch-tx run-dma run-irq run-ring run-rxchain run-fft run-sync run-sss run-polar run-ssb run-ssbfft run-ssb-fid run-ssb-slot run-pdsch run-pdsch-bn1 run-pdsch-gather run-pdsch-bg2 run-pdsch-zc64 run-pdsch-bg1-zc64 run-pdsch-iter run-pdsch-full run-pdsch-harq run-pdsch-16qam run-pdsch-64qam run-pusch run-pusch-tp run-sib1 run-pdcch run-pdcch-loop run-pdsch-loop run-link-e2e run-prach run-prach-cgra run-pucch run-csi run-mimo-ce run-mimo run-fftprobe run-slot run-proto-nr_pdcch run-proto-nr_pdsch run-proto-nr_csi run-proto-nr_prach run-proto-nr_pucch run-proto-nr_mimo run-proto-nr_pusch"
SLOW_EXTRA="run-rach"

MODEL_SET=()
SOC_SET=()
NEEDS_FULL=0
WANT_KERNELS=1

# ---- changed tier: map a git diff onto gates --------------------------------
add_model() {
    local m
    for m in "$@"; do
        case " ${MODEL_SET[*]-} " in *" $m "*) ;; *) MODEL_SET+=("$m") ;; esac
    done
}
add_soc() {
    local t
    for t in "$@"; do
        case " ${SOC_SET[*]-} " in *" $t "*) ;; *) SOC_SET+=("$t") ;; esac
    done
}

compute_changed() {
    local files f x
    files=$(cd "$ROOT" && {
        git diff --name-only "$CHANGED_REF" 2>/dev/null
        git ls-files --others --exclude-standard 2>/dev/null
    } | sort -u)
    if [ -z "$files" ]; then
        echo "changed: no changes vs $CHANGED_REF"
        WANT_KERNELS=0
        return 0
    fi
    for f in $files; do
        case "$f" in
            docs/*|*.md|README.md|AGENTS.md|tools/verify.sh) continue ;;
            sw/soc/Makefile|sim/soc/Makefile|Makefile)
                # A Makefile edit usually only adds a target: pick the newly
                # introduced run-* gates out of the diff instead of falling
                # back to the whole full tier.
                x=$(cd "$ROOT" && git diff "$CHANGED_REF" -- "$f" 2>/dev/null |
                    grep -oE '^\+ *(run|soc-run)-[a-z0-9-]+ *:' |
                    sed -E 's/^\+ *//; s/^soc-run-/run-/; s/ *:$//' | sort -u)
                for x in $x; do add_soc "$x"; done
                ;;
            tools/nr_pdcch.py)      add_model model-pdcch; add_soc run-pdcch run-pdcch-loop run-pdsch-loop run-link-e2e run-sib1 ;;
            tools/nr_pbch.py)       add_soc run-pbch-pack run-pbch-tx run-ssb-fid run-ssb-slot ;;
            tools/nr_ofdm_tx.py)    add_soc run-ifft ;;
            tools/nr_ldpc.py)       add_model model-ldpc; add_soc run-ldpc-enc ;;
            tools/nr_dl_mod.py)     add_soc run-dl-mod ;;
            tools/nr_pdcch_tx.py)   add_soc run-pdcch-tx ;;
            tools/nr_pdcch_reg.py)  add_model model-reg; add_soc run-pdcch-tx ;;
            sw/soc/ldpc_enc.inc|sw/soc/gold_seq.inc)
                add_soc run-ldpc-enc run-pbch-pack run-dl-mod ;;
            sw/soc/pdcch_stages.inc)
                add_soc run-pdcch-loop run-proto-nr_pdcch ;;
            sw/soc/pdsch_stages.inc)
                add_soc run-pdsch-loop run-proto-nr_pdsch ;;
            sw/soc/csi_stages.inc)
                add_soc run-csi run-proto-nr_csi ;;
            sw/soc/zc_corr.inc)
                add_soc run-prach run-pucch run-proto-nr_prach run-proto-nr_pucch run-proto-nr_mimo run-proto-nr_pusch ;;
            sw/soc/mimo_stages.inc)
                add_soc run-mimo run-proto-nr_mimo ;;
            sw/soc/pusch_stages.inc)
                add_soc run-pusch run-proto-nr_pusch ;;
            sw/soc/proto_runner.c|sw/soc/nr_*_ops.inc)
                add_soc run-proto-nr_pdcch run-proto-nr_pdsch run-proto-nr_csi run-proto-nr_prach run-proto-nr_pucch run-proto-nr_mimo run-proto-nr_pusch ;;
            proto/*)                add_soc run-proto-nr_pdcch run-proto-nr_pdsch run-proto-nr_csi run-proto-nr_prach run-proto-nr_pucch run-proto-nr_mimo run-proto-nr_pusch ;;
            tools/nr_mimo.py)       add_model model-mimo ;;
            tools/nr_mac.py)        add_model model-mac model-link model-rach model-conn ;;
            tools/pdsch_full_plan.py) add_model model-harq; add_soc run-pdsch-full run-pdsch-harq run-pdsch-16qam ;;
            tools/nr_ul.py)         add_model model-ul ;;
            tools/nr_pusch.py)      add_model model-pusch model-link model-conn; add_soc run-pusch ;;
            tools/nr_sib1.py)       add_model model-sib1; add_soc run-sib1 ;;
            tools/nr_link.py)       add_model model-link ;;
            tools/nr_rach.py)       add_model model-rach; add_soc run-prach run-rach ;;
            tools/nr_rrc.py)        add_model model-rrc model-conn ;;
            tools/nr_conn.py)       add_model model-conn ;;
            tools/nr_pucch.py)      add_model model-pucch; add_soc run-pucch ;;
            tools/nr_la.py)         add_model model-la ;;
            tools/nr_qam.py)        add_model model-qam ;;
            tools/nr_pdsch_amc.py)  add_model model-amc; add_soc run-pdsch-16qam ;;
            tools/nr_pdcch_reg.py)  add_model model-reg ;;
            tools/nr_pc.py)         add_model model-pc ;;
            tools/dump_*.py|tools/check_*.py)
                x=${f##*/}; x=${x%.py}; x=${x#dump_}; x=${x#check_}; x=${x//_/-}
                add_soc "run-$x" ;;
            cgra/target.yaml|tools/check_yaml_coupling.py|sw/include/gpcp_layout.h|sw/soc/*_layout.h) WANT_KERNELS=1; add_soc $SOCCORE_FULL ;;
            tools/gen_kernel_prog.py|cgra/tools/*|cgra/kernels/*|sw/soc/*_prog.h) WANT_KERNELS=1; add_soc $SOCCORE_FULL ;;
            tools/fft_plan.py)       add_soc run-fft run-ssbfft run-ssb-fid run-ssb-slot ;;
            sw/soc/prach_corr_prog.h) add_soc run-prach run-rach run-pucch ;;
            sw/soc/pdcch_luts.h)    add_soc run-pdcch run-pdcch-loop run-pdsch-loop run-link-e2e run-sib1 run-rach ;;
            sw/soc/link_e2e_bm.c)   add_soc run-link-e2e run-sib1 ;;
            sw/soc/pdsch_full_bm.c) add_soc run-pdsch-full run-pdsch-harq run-pdsch-16qam ;;
            sw/soc/*_bm.c)
                x=${f##*/}; x=${x%_bm.c}; x=${x//_/-}; add_soc "run-$x" ;;
            *)  # RTL, simulator, makefiles, shared headers, unknown sources:
                # too wide to map safely -> fall back to the full tier.
                NEEDS_FULL=1 ;;
        esac
    done
    if [ "$NEEDS_FULL" = 1 ] && [ "$CHANGED_ONLY" != 1 ]; then
        echo "changed: $CHANGED_REF touches shared infrastructure -> running full tier"
        MODEL_SET=("${ALL_MODELS[@]}")
        SOC_SET=($SOCCORE_FULL)
        WANT_KERNELS=1
        return 0
    fi
    if [ "$NEEDS_FULL" = 1 ]; then
        echo "changed: CHANGED_ONLY=1 -> skipping the full-tier fallback"
    fi
    # Pure model/checker/benchmark edits do not need the RTL kernel suite.
    WANT_KERNELS=0
    echo "changed: ${#MODEL_SET[@]} model gates, ${#SOC_SET[@]} SoC gates (ref $CHANGED_REF)"
}

case "$MODE" in
    quick)   SOC_SET=($SOCCORE_QUICK) ;;
    full)    MODEL_SET=("${ALL_MODELS[@]}"); SOC_SET=($SOCCORE_FULL) ;;
    slow|smart) MODEL_SET=("${ALL_MODELS[@]}"); SOC_SET=($SOCCORE_FULL $SLOW_EXTRA) ;;
    changed) compute_changed ;;
esac
[ "$WITH_LINUX" = 1 ] && SOC_SET+=(linux)
[ "$CACHE_ON" = 1 ] && [ -z "$GATE_OVERRIDE" ] && WANT_KERNELS=1

# ---- iron rule: microcode is generated, never hand-assembled ---------------
# Always runs and never cached: a fresh gpcp_cgra_inst() in sw/ must fail the
# build even when the input fingerprint says every gate may be skipped.
if ! HANDPROG_OUT=$(cd "$ROOT" && python3 tools/check_handprog.py 2>&1); then
    printf '%s\n' "$HANDPROG_OUT"
    echo "verify aborted: hand-written microcode (AGENTS.md 4.18)" >&2
    exit 1
fi
printf '%s\n' "$HANDPROG_OUT" | sed -n '1,2p;$p'
echo

# ---- iron rule: kernel YAML must not describe the hardware ------------------
# Also uncached: a physical spad coordinate in a new spec must fail the build
# even when every gate is skipped by the input fingerprint.
if ! COUPLING_OUT=$(cd "$ROOT" && python3 tools/check_yaml_coupling.py 2>&1); then
    printf '%s\n' "$COUPLING_OUT"
    echo "verify aborted: kernel YAML is coupled to hardware (AGENTS.md 4.19)" >&2
    exit 1
fi
printf '%s\n' "$COUPLING_OUT" | sed -n '1p;$p'
echo

# ---- 协议描述层（docs/20）：硬件无关检查 + 换厨房测试（纯静态，秒级） ------
if ! PROTO_OUT=$(cd "$ROOT" && python3 proto/check_hll.py proto/scenarios/*.py 2>&1); then
    printf '%s\n' "$PROTO_OUT"
    echo "verify aborted: 协议场景里出现了硬件概念（docs/20 §3）" >&2
    exit 1
fi
if ! PROTO_C_OUT=$(cd "$ROOT" && python3 proto/compile.py --all 2>&1); then
    printf '%s\n' "$PROTO_C_OUT" | tail -20
    echo "verify aborted: 协议场景无法落点（docs/20 §6）" >&2
    exit 1
fi
if ! PROTO_E_OUT=$(cd "$ROOT" && python3 proto/emit.py --all 2>&1); then
    printf '%s\n' "$PROTO_E_OUT" | tail -20
    echo "verify aborted: 协议场景生成不了 plan.h / 激励（docs/20 §6 第 4 步）" >&2
    exit 1
fi
printf '%s\n' "$PROTO_OUT" | sed -n '1p;$p'
printf '%s\n' "$PROTO_C_OUT" | grep -E "换厨房|FAIL" | tail -2
printf '%s\n' "$PROTO_E_OUT" | grep -E "params hash|plan.h" | tail -2
echo

if [ "$MODE" = changed ] && [ ${#MODEL_SET[@]} -eq 0 ] &&
   [ ${#SOC_SET[@]} -eq 0 ] && [ "$WANT_KERNELS" = 0 ]; then
    echo "changed: no test-affecting changes, nothing to run."
    exit 0
fi

echo "== GPCP verify ($MODE, parallel -j$JOBS) =="
echo "log: $LOG"

# ---- serial prebuild: all binaries + the one shared twiddle LUT ------------
if ! ( cd "$ROOT" \
        && env -u LD_LIBRARY_PATH make -C sw/soc \
        && env -u LD_LIBRARY_PATH make -C sim/soc all \
        && mkdir -p build/fft \
        && python3 tools/fft_plan.py lut build/fft/twiddle.bin ) >>"$LOG" 2>&1; then
    echo "prebuild FAILED (see $LOG)" >&2
    exit 1
fi

# ---- parallel gate pool -----------------------------------------------------
NAMES=()

run_one() {                            # run_one <name> <cmd...>  (background)
    local name=$1 t0 rc; shift
    t0=$(date +%s)
    ( cd "$ROOT" && env -u LD_LIBRARY_PATH "$@" ) >"$TMPD/$name.out" 2>&1
    rc=$?
    printf '%s' "$rc" >"$TMPD/$name.rc"
    if [ "$CACHE_ON" = 1 ]; then
        python3 "$ROOT/tools/verify_cache.py" mark "$name" "$rc" >/dev/null 2>&1 || true
    fi
    printf '  .. %-20s done (%ss)\n' "$name" "$(( $(date +%s) - t0 ))"
}

submit() {                             # submit <name> <cmd...>
    local name=$1
    NAMES+=("$name")
    while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do wait -n; done
    run_one "$@" &
}

collect() {
    local name rc out info l
    for name in "${NAMES[@]}"; do
        out="$TMPD/$name.out"
        rc=$(cat "$TMPD/$name.rc")
        printf '%-16s ' "$name"
        if [ "$rc" = 0 ]; then
            info=$(grep -E '^ALL TESTS PASSED' "$out" | tail -1)
            [ -z "$info" ] && info=$(grep -E '^\[ OK \]|^\[info\]' "$out" | tail -1)
            printf 'PASS  %s\n' "${info#\[ OK \] }"
            PASS=$((PASS + 1))
        else
            printf 'FAIL  (see build/verify.log)\n'
            FAIL=$((FAIL + 1))
            FAILED_NAMES="$FAILED_NAMES $name"
            grep -E '^\[FAIL|Error|error:|Traceback|mismatch' "$out" | tail -4 |
                while IFS= read -r l; do printf '        %s\n' "$l"; done
        fi
        cat "$out" >> "$LOG"
        printf -- '---- %s ----\n' "$name" >> "$LOG"
    done
}

# ---- ordered gate list, then (smart only) drop unchanged gates -------------
PLAN_GATES=()
[ "$WANT_KERNELS" = 1 ] && PLAN_GATES+=(kernels)
for m in "${MODEL_SET[@]-}"; do [ -n "$m" ] && PLAN_GATES+=("$m"); done
for t in "${SOC_SET[@]-}"; do [ -n "$t" ] && PLAN_GATES+=("$t"); done
[ -n "$GATE_OVERRIDE" ] && PLAN_GATES=($GATE_OVERRIDE)

RUN_GATES=()
SKIP_LINES=()
if [ "$CACHE_ON" = 1 ]; then
    FORCE_ARG=""
    [ "$FORCE" = 1 ] && FORCE_ARG="--force"
    if ! python3 "$ROOT/tools/verify_cache.py" plan $FORCE_ARG \
            --gates "${PLAN_GATES[*]}" >"$TMPD/plan.txt" 2>"$TMPD/plan.err"; then
        echo "cache planner failed -> running every gate" >&2
        cat "$TMPD/plan.err" >&2
        RUN_GATES=("${PLAN_GATES[@]}")
    else
        while read -r kind name ts; do
            case "$kind" in
                RUN)  RUN_GATES+=("$name") ;;
                SKIP) SKIP_LINES+=("$name $ts") ;;
            esac
        done < "$TMPD/plan.txt"
    fi
else
    RUN_GATES=("${PLAN_GATES[@]}")
fi

gate_cmd() {                           # gate_cmd <name>
    local n=$1
    case "$n" in
        kernels)  submit "$n" make -C "$ROOT" test ;;
        linux)    submit "$n" make -C "$ROOT/sim/soc" linux ;;
        model-*)  submit "$n" ${MODEL_CMD[$n]} ;;
        *)        submit "$n" make -C "$ROOT/sim/soc" "$n" ;;
    esac
}

# kernels rebuilds the CGRA bench; keep it out of the pool so it cannot race
# with the simulator binaries that the pool reads.
REST=()
for g in "${RUN_GATES[@]-}"; do
    [ -z "$g" ] && continue
    if [ "$g" = kernels ]; then
        gate_cmd kernels
        wait
    else
        REST+=("$g")
    fi
done
for g in "${REST[@]-}"; do [ -n "$g" ] && gate_cmd "$g"; done
wait
collect
for line in "${SKIP_LINES[@]-}"; do
    [ -z "$line" ] && continue
    printf '%-16s SKIP  (inputs unchanged since %s)\n' "${line%% *}" "${line#* }"
done

END=$(date +%s)
echo
if [ ${#SKIP_LINES[@]} -gt 0 ]; then
    echo "== result: $PASS passed, ${#SKIP_LINES[@]} skipped (cached), $FAIL failed, $((END - START)) s =="
else
    echo "== result: $PASS passed, $FAIL failed, $((END - START)) s =="
fi
if [ "$FAIL" -gt 0 ]; then
    echo "failed:$FAILED_NAMES"
    echo "hint: grep -n '\[ OK \]|\[info\]|error' $LOG"
    exit 1
fi
echo "PASS: the baseline is intact (verify $MODE, parallel -j$JOBS)"
