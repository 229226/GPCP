#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Fetch the PPA toolchain + PDK that belong to THIS project.
#
# The project must not depend on (or touch) any other checkout on the machine
# (e.g. the ysyx-workbench/yosys-sta installation).  Everything the synthesis
# flow needs lives under third_party/ and is fetched here:
#
#   third_party/eda/oss-cad-suite/  Yosys 0.52 + the slang SV frontend, and the
#                                   Verilator used by the simulation flows
#   third_party/eda/bin/iEDA        iEDA (OSCC-Project/iEDA, open source; the
#                                   published binary, iSTA = timing + power)
#   third_party/pdk/nangate45/      Nangate45 PDK from OpenROAD-flow-scripts
#                                   (flow/platforms/nangate45, Apache-2.0)
#
# All of them are gitignored; this script is idempotent and skips work it has
# already done.  The oss-cad-suite download is the big one (~500 MB).  Override IEDA_URL / ORFS_URL to point at another mirror.
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
# pinned to the release the PPA numbers were measured with (Yosys 0.52+63)
YOSYS_TAG="${YOSYS_TAG:-2025-04-20}"
YOSYS_URL="${YOSYS_URL:-https://github.com/YosysHQ/oss-cad-suite-build/releases/download/$YOSYS_TAG/oss-cad-suite-linux-x64-$(echo "$YOSYS_TAG" | tr -d -).tgz}"

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

# ---- 2) Yosys + slang (oss-cad-suite, pinned release) ------------------------
if [ -x "$EDA/oss-cad-suite/bin/yosys" ]; then
    echo "[yosys] $EDA/oss-cad-suite already present, skip"
else
    echo "[yosys] fetching oss-cad-suite $YOSYS_TAG (~500 MB, this takes a while)"
    mkdir -p "$EDA"
    curl -sSL "$YOSYS_URL" | tar xz -C "$EDA"
fi

# ---- 3) iEDA ----------------------------------------------------------------
if [ -x "$EDA/bin/iEDA" ]; then
    echo "[ieda] $EDA/bin/iEDA already present, skip"
else
    echo "[ieda] fetching $IEDA_URL"
    mkdir -p "$EDA/bin"
    curl -sSL "$IEDA_URL" | tar xj -C "$EDA/bin"
    chmod +x "$EDA/bin/iEDA"
fi

# ---- 4) sanity check -------------------------------------------------------
echo
echo "PDK  : $PDK"
echo "  cells : $(grep -c '^  cell (' "$PDK/lib/NangateOpenCellLibrary_typical.lib")"
echo "  lefs  : $(grep -c '^MACRO ' "$PDK/lef/NangateOpenCellLibrary.macro.mod.lef") macros (mod), $(ls "$PDK/lef" | grep -c fakeram) fakeram sizes"
echo "Yosys: $EDA/oss-cad-suite/bin/yosys"
echo "EDA  : $EDA/bin/iEDA"
env -u LD_LIBRARY_PATH "$EDA/oss-cad-suite/bin/yosys" -V 2>/dev/null || echo "  (yosys not runnable)"
env -u LD_LIBRARY_PATH "$EDA/bin/iEDA" -v 2>/dev/null | head -2 || echo "  (could not query the iEDA version; still usable)"
echo
echo "done.  Next: env -u LD_LIBRARY_PATH make -C syn sta CLK_FREQ_MHZ=500"
