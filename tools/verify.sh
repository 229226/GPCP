#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# GPCP acceptance runner - one command that proves the baseline still holds.
#
#   bash tools/verify.sh quick     # ~3-5 min : kernels + 4 SoC targets
#   bash tools/verify.sh full      # ~15-25 min: + every SoC benchmark
#   bash tools/verify.sh full --linux   # also boots Linux (10^8 cycles, long)
#
# Every step is an existing make target; this script only sequences them, keeps
# a log (build/verify.log) and prints the [ OK ]/[info] lines the checkers emit.
# Exit code 0 iff every step passed.
#
# NOTE: LD_LIBRARY_PATH must be cleared for Verilator and its models on this
# machine (the toolchain ships an older libstdc++), hence `env -u`.
# -----------------------------------------------------------------------------
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
MODE=${1:-quick}
shift || true
WITH_LINUX=0
for a in "$@"; do [ "$a" = "--linux" ] && WITH_LINUX=1; done

LOG="$ROOT/build/verify.log"
mkdir -p "$ROOT/build"
: > "$LOG"

PASS=0
FAIL=0
FAILED_NAMES=""
START=$(date +%s)

run() {                                # run <name> <make-args...>
    local name=$1; shift
    local out
    out=$(mktemp)
    printf '%-16s ' "$name"
    if ( cd "$ROOT" && env -u LD_LIBRARY_PATH "$@" ) >"$out" 2>&1; then
        local info
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
    rm -f "$out"
    # keep the log from growing into the next step's [ OK ] line
    printf -- '---- %s ----\n' "$name" >> "$LOG"
}

echo "== GPCP verify ($MODE) =="
echo "log: $LOG"

# ---- 1) CGRA kernel suite (rebuilds the bench if the RTL changed) -----------
run kernels make -C "$ROOT" test

# ---- 2) SoC benchmarks ------------------------------------------------------
# run-ssb-slot sits in the quick list on purpose: it buffers 8 slots and its
# expected numbers are tied to the SSB_RE layout (SSB_K0, NPILOT, DMRS c_init),
# so it silently went stale once when the layout changed and only `full` caught
# it.  It is one of the longer benchmarks, but still worth the coverage.
# run-pdsch-iter (full BG2, 8 iterations, ~6 min) is `full`-only: it is the P1
# end-to-end target and far too slow for the quick baseline.
SOCCORE="run-ring run-rxchain run-pdsch run-pdsch-bn1 run-pdsch-gather run-pdsch-bg2 run-fft run-ssbfft run-ssb-fid run-ssb-slot"
[ "$MODE" = full ] && SOCCORE="run run-dma run-irq run-ring run-rxchain run-fft run-sync run-sss run-polar run-ssb run-ssbfft run-ssb-fid run-ssb-slot run-pdsch run-pdsch-bn1 run-pdsch-gather run-pdsch-bg2 run-pdsch-iter run-fftprobe run-slot"
for t in $SOCCORE; do
    run "$t" make -C "$ROOT/sim/soc" "$t"
done

# ---- 3) optional: Linux boot (very long, 10^8 cycles) -----------------------
if [ "$WITH_LINUX" = 1 ]; then
    run linux make -C "$ROOT/sim/soc" linux
fi

END=$(date +%s)
echo
echo "== result: $PASS passed, $FAIL failed, $((END - START)) s =="
if [ "$FAIL" -gt 0 ]; then
    echo "failed:$FAILED_NAMES"
    echo "hint: grep -n '\[ OK \]|\[info\]|error' $LOG"
    exit 1
fi
echo "PASS: the baseline is intact (kernels + $MODE SoC targets$([ "$WITH_LINUX" = 1 ] && echo ' + linux'))"
