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
#   third_party/pdk/sky130hd/       SkyWater sky130 HD liberty from the same
#                                   repo (flow/platforms/sky130hd); selected
#                                   with PDK_NAME=sky130hd, liberty only
#                                   because the PPA flow is pre-layout
#
# All of them are gitignored; this script is idempotent and skips work it has
# already done.  The oss-cad-suite download is the big one (~500 MB).  Override IEDA_URL / ORFS_URL to point at another mirror.  If GitHub is unreachable, set
# GH_PROXY (e.g. GH_PROXY=https://ghproxy.net/) to prefix both the clone URL
# and the raw file URLs.
#
# usage: bash tools/fetch_eda.sh        (or: make -C syn tools|tools-sky130)
# -----------------------------------------------------------------------------
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EDA="${EDA_HOME:-$ROOT/third_party/eda}"

# Which PDK to fetch: nangate45 (default, whole platform incl. LEF) or sky130hd
# (SkyWater 130 nm, the liberty only - the PPA flow is pre-layout and needs no
# LEF).  Selected by PDK_NAME so `make -C syn tools-sky130` and
# `make -C syn tools PDK_NAME=sky130hd` both work.
PDK_NAME="${PDK_NAME:-nangate45}"
case "$PDK_NAME" in
    nangate45) PDK="${PDK_PATH:-$ROOT/third_party/pdk/nangate45}" ;;
    sky130hd)  PDK="${PDK_PATH:-$ROOT/third_party/pdk/sky130hd}"  ;;
    *) echo "unknown PDK_NAME '$PDK_NAME' (nangate45|sky130hd)" >&2; exit 2 ;;
esac

IEDA_URL="${IEDA_URL:-https://ysyx.oscc.cc/slides/resources/archive/ieda.tar.bz2}"
ORFS_URL="${ORFS_URL:-https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git}"
ORFS_PATH="flow/platforms/nangate45"
# Optional mirror prefix for GitHub URLs, e.g. GH_PROXY=https://ghproxy.net/ .
# Raw file URLs become "$GH_PROXYhttps://raw.githubusercontent.com/..." and the
# git clone URL "$GH_PROXY$ORFS_URL".
GH_PROXY="${GH_PROXY:-}"
SKY130_RAW="${SKY130_RAW:-https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD-flow-scripts/master/flow/platforms/sky130hd}"
SKY130_LIB="sky130_fd_sc_hd__tt_025C_1v80.lib"
# pinned to the release the PPA numbers were measured with (Yosys 0.52+63)
YOSYS_TAG="${YOSYS_TAG:-2025-04-20}"
YOSYS_URL="${YOSYS_URL:-https://github.com/YosysHQ/oss-cad-suite-build/releases/download/$YOSYS_TAG/oss-cad-suite-linux-x64-$(echo "$YOSYS_TAG" | tr -d -).tgz}"

# ---- 1) PDK ----------------------------------------------------------------
if [ "$PDK_NAME" = "nangate45" ]; then
    if [ -f "$PDK/lib/NangateOpenCellLibrary_typical.lib" ]; then
        echo "[pdk ] $PDK already present, skip"
    else
        echo "[pdk ] fetching $ORFS_PATH from $ORFS_URL"
        tmp="$(mktemp -d)"
        git clone --depth 1 --filter=blob:none --sparse "$GH_PROXY$ORFS_URL" "$tmp/orfs" >/dev/null 2>&1
        git -C "$tmp/orfs" sparse-checkout set "$ORFS_PATH" >/dev/null 2>&1
        mkdir -p "$(dirname "$PDK")"
        cp -a "$tmp/orfs/$ORFS_PATH" "$PDK"
        rm -rf "$tmp"
    fi
else
    if [ -f "$PDK/lib/$SKY130_LIB" ]; then
        echo "[pdk ] $PDK already present, skip"
    else
        echo "[pdk ] fetching sky130_fd_sc_hd liberty (~13 MB)"
        mkdir -p "$PDK/lib"
        curl -sSL --retry 2 -o "$PDK/lib/$SKY130_LIB" "$GH_PROXY$SKY130_RAW/lib/$SKY130_LIB"
        # pre-layout flow: no LEF/techlef/GDS needed (see syn/Makefile)
    fi
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
echo "PDK  : $PDK ($PDK_NAME)"
LIBF=$(ls "$PDK"/lib/*.lib 2>/dev/null | grep -v -i fakeram | head -1)
echo "  liberty: $(basename "$LIBF") / $(grep -cE '^[[:space:]]+cell \(' "$LIBF") cells"
echo "Yosys: $EDA/oss-cad-suite/bin/yosys"
echo "EDA  : $EDA/bin/iEDA"
env -u LD_LIBRARY_PATH "$EDA/oss-cad-suite/bin/yosys" -V 2>/dev/null || echo "  (yosys not runnable)"
env -u LD_LIBRARY_PATH "$EDA/bin/iEDA" -v 2>/dev/null | head -2 || echo "  (could not query the iEDA version; still usable)"
echo
if [ "$PDK_NAME" = "sky130hd" ]; then
    echo "done.  Next: env -u LD_LIBRARY_PATH make -C syn sta PDK_NAME=sky130hd CLK_FREQ_MHZ=50"
else
    echo "done.  Next: env -u LD_LIBRARY_PATH make -C syn sta CLK_FREQ_MHZ=500"
fi
