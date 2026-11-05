#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Overnight robustness sweep.  Runs in a detached git worktree of HEAD so the
# developer's working tree can keep changing underneath it.
#
#   bash tools/nightly.sh                 # seeds 1-3, SNR -5..20, slow + linux
#   SEEDS="1 2 3 4 5" bash tools/nightly.sh
#   MAX_SECONDS=28800 bash tools/nightly.sh      # stop starting new cases after 8 h
#   SKIP_LINUX=1 bash tools/nightly.sh
#
# Output: build/nightly/<timestamp>/report.md (one row per case) and
# failures.txt (non-zero exits).  A failure never stops the sweep.
# -----------------------------------------------------------------------------
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TS=$(date +%Y%m%d-%H%M%S)
OUT="$ROOT/build/nightly/$TS"
WT="$ROOT/build/nightly/wt-$TS"
SEEDS=${SEEDS:-"1 2 3"}
SNRS=${SNRS:-"-5 0 5 10 15 20"}
MAX_SECONDS=${MAX_SECONDS:-28800}
SKIP_LINUX=${SKIP_LINUX:-0}
START=$(date +%s)

mkdir -p "$OUT"
: > "$OUT/failures.txt"
REPORT="$OUT/report.md"
{
    echo "# Nightly sweep $TS"
    echo
    echo "- seed set: $SEEDS"
    echo "- SNR set (dB): $SNRS"
    echo "- max seconds: $MAX_SECONDS"
    echo
    echo "| case | result | cycles | wall |"
    echo "|---|---|---|---|"
} > "$REPORT"

say() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }

if ! git -C "$ROOT" worktree add --detach "$WT" HEAD >"$OUT/worktree.log" 2>&1; then
    say "worktree creation failed (see $OUT/worktree.log)"
    exit 1
fi
say "worktree: $WT (detached at $(git -C "$WT" rev-parse --short HEAD))"

# third_party/ is a local, gitignored 4.3 GB tree (CVA6, Linux, PDK...), so a
# fresh worktree does not have it.  Link it read-only instead of copying.
mkdir -p "$WT/third_party"
for d in "$ROOT"/third_party/*; do
    [ -e "$d" ] || continue
    name=$(basename "$d")
    [ "$name" = .gitkeep ] && continue
    ln -sfn "$d" "$WT/third_party/$name"
done
say "linked third_party: $(ls -1 "$WT/third_party" | tr '\n' ' ')"

if ! ( cd "$WT" && env -u LD_LIBRARY_PATH make -C sw/soc \
        && env -u LD_LIBRARY_PATH make -C sim/soc all \
        && mkdir -p build/fft \
        && python3 tools/fft_plan.py lut build/fft/twiddle.bin ) \
        >"$OUT/prebuild.log" 2>&1; then
    say "prebuild failed (see $OUT/prebuild.log)"
    echo "prebuild" >> "$OUT/failures.txt"
    exit 1
fi
say "prebuild done"

budget_left() { [ $(( $(date +%s) - START )) -lt "$MAX_SECONDS" ]; }

run_case() {                            # run_case <tag> <make-args...>
    local tag=$1; shift
    if ! budget_left; then
        printf '| %s | SKIP (time budget) | - | - |\n' "$tag" >> "$REPORT"
        return
    fi
    local t0 log cyc rc
    t0=$(date +%s)
    log="$OUT/$tag.log"
    ( cd "$WT" && env -u LD_LIBRARY_PATH make -C sim/soc "$@" ) >"$log" 2>&1
    rc=$?
    cyc=$(grep -oE '[0-9][0-9,]* cycles' "$log" | tail -1)
    if [ "$rc" = 0 ]; then
        printf '| %s | **OK** | %s | %ss |\n' "$tag" "${cyc:--}" \
               "$(( $(date +%s) - t0 ))" >> "$REPORT"
        say "OK   $tag (${cyc:-?})"
    else
        printf '| %s | **FAIL** | - | %ss |\n' "$tag" \
               "$(( $(date +%s) - t0 ))" >> "$REPORT"
        echo "$tag :: make $*" >> "$OUT/failures.txt"
        say "FAIL $tag"
    fi
}

# ---- A) seed robustness on the full-chain gates -----------------------------
for s in $SEEDS; do
    for g in run-pdsch-full run-pdsch-harq run-pdsch-16qam run-pusch \
             run-sib1 run-pdcch run-link-e2e; do
        run_case "A-$g-seed$s" "$g" "SEED=$s"
    done
    run_case "A-run-rach-seed$s" run-rach "SEED=$s"
done

# ---- B) SNR waterfall on the four chain gates ------------------------------
for snr in $SNRS; do
    for g in run-pdsch-full run-pusch run-sib1 run-pdcch; do
        run_case "B-$g-snr$snr" "$g" "SNR=$snr"
    done
done

# ---- C) full tier, cache bypassed (the archival run) -----------------------
if budget_left; then
    t0=$(date +%s)
    if ( cd "$WT" && env -u LD_LIBRARY_PATH bash tools/verify.sh slow --force ) \
            >"$OUT/C-slow-force.log" 2>&1; then
        res=$(grep -E '^== result' "$OUT/C-slow-force.log" | tail -1)
        printf '| C-slow-force | **OK** | %s | %ss |\n' "$res" \
               "$(( $(date +%s) - t0 ))" >> "$REPORT"
        say "OK   slow --force ($res)"
    else
        printf '| C-slow-force | **FAIL** | - | %ss |\n' \
               "$(( $(date +%s) - t0 ))" >> "$REPORT"
        echo "C-slow-force" >> "$OUT/failures.txt"
        say "FAIL slow --force"
    fi
fi

# ---- D) Linux boot probe ---------------------------------------------------
if [ "$SKIP_LINUX" != 1 ] && budget_left; then
    t0=$(date +%s)
    if ( cd "$WT" && env -u LD_LIBRARY_PATH make -C sim/soc linux ) \
            >"$OUT/D-linux.log" 2>&1; then
        printf '| D-linux | **OK** | - | %ss |\n' "$(( $(date +%s) - t0 ))" >> "$REPORT"
        say "OK   linux"
    else
        printf '| D-linux | **FAIL** | - | %ss |\n' "$(( $(date +%s) - t0 ))" >> "$REPORT"
        echo "D-linux" >> "$OUT/failures.txt"
        say "FAIL linux"
    fi
fi

# ---- summary ---------------------------------------------------------------
TOTAL=$(grep -c '^|' "$REPORT")
FAILS=$(wc -l < "$OUT/failures.txt")
{
    echo
    echo "## Summary"
    echo
    echo "- cases: $((TOTAL - 2))"
    echo "- failures: $FAILS"
    echo "- wall: $(( $(date +%s) - START )) s"
    echo "- worktree kept at: $WT"
} >> "$REPORT"
say "done: $((TOTAL - 2)) cases, $FAILS failures, $(( $(date +%s) - START )) s"
say "report: $REPORT"
