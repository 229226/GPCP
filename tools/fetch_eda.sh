#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Fetch the PPA toolchain + PDK that belong to THIS project.
#
# The project must not depend on (or touch) any other checkout on the machine
# (e.g. the ysyx-workbench/yosys-sta installation).  Everything the synthesis
# flow needs lives under third_party/ and is fetched here:
#
#   third_party/eda/bin/iEDA     iEDA (OSCC-Project/iEDA, open source; the
#                                published binary, iSTA = static timing + power)
#   third_party/pdk/nangate45/   Nangate45 PDK from OpenROAD-flow-scripts
#                                (flow/platforms/nangate45, Apache-2.0)
#
# Both directories are gitignored; this script is idempotent and skips work it
# has already done.  Override IEDA_URL / ORFS_URL to point at another mirror.
#
# usage: bash tools/fetch_eda.sh        (or: make -C syn tools)
# -----------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EDA="${EDA_HOME:-$ROOT/third_party/eda}"
PDK="${PDK_PATH:-$ROOT/third_party/pdk/nangate45}"

IEDA_URL="${IEDA_URL:-https://ysyx.oscc.cc/slides/resources/archive/ieda.tar.bz2}"
ORFS_URL="${ORFS_URL:-https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git}"
ORFS_PATH="flow/platforms/nangate45"

# ---- 1) PDK ----------------------------------------------------------------
if [ -f "$PDK/lib/NangateOpenCellLibrary_typical.lib" ]; then
    echo "[pdk ] $PDK already present, skip"
else
    echo "[pdk ] fetching $ORFS_PATH from $ORFS_URL"
    tmp="$(mktemp -d)"
    git clone --depth 1 --filter=blob:none --sparse "$ORFS_URL" "$tmp/orfs" >/dev/null 2>&1
    git -C "$tmp/orfs" sparse-checkout set "$ORFS_PATH" >/dev/null 2>&1
    mkdir -p "$(dirname "$PDK")"
    cp -a "$tmp/orfs/$ORFS_PATH" "$PDK"
    rm -rf "$tmp"
fi

# ---- 2) iEDA ----------------------------------------------------------------
if [ -x "$EDA/bin/iEDA" ]; then
    echo "[ieda] $EDA/bin/iEDA already present, skip"
else
    echo "[ieda] fetching $IEDA_URL"
    mkdir -p "$EDA/bin"
    curl -sSL "$IEDA_URL" | tar xj -C "$EDA/bin"
    chmod +x "$EDA/bin/iEDA"
fi

# ---- 3) sanity check -------------------------------------------------------
echo
echo "PDK  : $PDK"
echo "  cells : $(grep -c '^  cell (' "$PDK/lib/NangateOpenCellLibrary_typical.lib")"
echo "  lefs  : $(grep -c '^MACRO ' "$PDK/lef/NangateOpenCellLibrary.macro.mod.lef") macros (mod), $(ls "$PDK/lef" | grep -c fakeram) fakeram sizes"
echo "EDA  : $EDA/bin/iEDA"
env -u LD_LIBRARY_PATH "$EDA/bin/iEDA" -v 2>/dev/null | head -2 || echo "  (could not query the version; still usable)"
echo
echo "done.  Next: env -u LD_LIBRARY_PATH make -C syn sta CLK_FREQ_MHZ=500"
