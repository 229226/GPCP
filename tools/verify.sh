#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# GPCP acceptance runner - one command that proves the baseline still holds.
#
#   bash tools/verify.sh quick       # ~2.5 min (146 s): kernels + 10 SoC targets
#   bash tools/verify.sh full        # ~8-10 min: + 16 model self-tests + 29 SoC
#   bash tools/verify.sh slow        # ~10 min (601 s): full + run-rach (11.8 M cycles)
#   bash tools/verify.sh changed     # ~0.5-3 min: only gates touched by git diff
#
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
while [ $# -gt 0 ]; do
    case "$1" in
        --linux) WITH_LINUX=1; shift ;;
        --slow)  [ "$MODE" = full ] && MODE=slow; shift ;;
        --jobs)  JOBS=${2:-}; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
case "$MODE" in quick|full|slow|changed) ;; *) echo "unknown mode: $MODE" >&2; exit 2 ;; esac
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
            model-pucch model-la model-qam model-amc)
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
)
SOCCORE_QUICK="run-ring run-rxchain run-pdsch run-pdsch-bn1 run-pdsch-gather run-pdsch-bg2 run-fft run-ssbfft run-ssb-fid run-ssb-slot"
# run-rach is the single longest gate (~9 min, 11.8 M cycles); it lives in the
# `slow` tier so the default full run stays ~10-12 min.
SOCCORE_FULL="run run-dma run-irq run-ring run-rxchain run-fft run-sync run-sss run-polar run-ssb run-ssbfft run-ssb-fid run-ssb-slot run-pdsch run-pdsch-bn1 run-pdsch-gather run-pdsch-bg2 run-pdsch-iter run-pdsch-full run-pdsch-harq run-pdsch-16qam run-pusch run-sib1 run-pdcch run-link-e2e run-prach run-pucch run-fftprobe run-slot"
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
            tools/nr_pdcch.py)      add_model model-pdcch; add_soc run-pdcch run-link-e2e run-sib1 ;;
            tools/nr_ldpc.py)       add_model model-ldpc ;;
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
            tools/dump_*.py|tools/check_*.py)
                x=${f##*/}; x=${x%.py}; x=${x#dump_}; x=${x#check_}; x=${x//_/-}
                add_soc "run-$x" ;;
            tools/gen_prach_prog.py) add_soc run-prach run-rach run-pucch ;;
            sw/soc/prach_prog.h)    add_soc run-prach run-rach run-pucch ;;
            sw/soc/pdcch_luts.h)    add_soc run-pdcch run-link-e2e run-sib1 run-rach ;;
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
    slow)    MODEL_SET=("${ALL_MODELS[@]}"); SOC_SET=($SOCCORE_FULL $SLOW_EXTRA) ;;
    changed) compute_changed ;;
esac
[ "$WITH_LINUX" = 1 ] && SOC_SET+=(linux)

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
    local name=$1 t0; shift
    t0=$(date +%s)
    ( cd "$ROOT" && env -u LD_LIBRARY_PATH "$@" ) >"$TMPD/$name.out" 2>&1
    printf '%s' "$?" >"$TMPD/$name.rc"
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

# kernels rebuilds the CGRA bench; keep it out of the pool so it cannot race
# with the simulator binaries that the pool reads.
if [ "$WANT_KERNELS" = 1 ]; then
    submit kernels make -C "$ROOT" test
    wait
fi

for m in "${MODEL_SET[@]-}"; do
    [ -n "$m" ] || continue
    submit "$m" ${MODEL_CMD[$m]}
done
for t in "${SOC_SET[@]-}"; do
    [ -n "$t" ] || continue
    if [ "$t" = linux ]; then
        submit linux make -C "$ROOT/sim/soc" linux
    else
        submit "$t" make -C "$ROOT/sim/soc" "$t"
    fi
done
wait
collect

END=$(date +%s)
echo
echo "== result: $PASS passed, $FAIL failed, $((END - START)) s =="
if [ "$FAIL" -gt 0 ]; then
    echo "failed:$FAILED_NAMES"
    echo "hint: grep -n '\[ OK \]|\[info\]|error' $LOG"
    exit 1
fi
echo "PASS: the baseline is intact (verify $MODE, parallel -j$JOBS)"
