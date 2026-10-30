#!/usr/bin/env bash
# Source this before using Verilator / Yosys / gtkwave directly:
#   source scripts/env.sh
#
# Two things are handled:
#   1. oss-cad-suite (Verilator 5.x) is not on PATH by default;
#   2. the IDE injects LD_LIBRARY_PATH entries that shadow the system
#      libstdc++, which breaks g++ used by Verilator's build step.

_oss_cad="$(ls -d "$HOME"/Downloads/oss-cad-suite*/oss-cad-suite 2>/dev/null | head -n1)"
if [ -n "$_oss_cad" ]; then
    export PATH="$_oss_cad/bin:$PATH"
fi
unset LD_LIBRARY_PATH
unset _oss_cad
