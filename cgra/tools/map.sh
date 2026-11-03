#!/usr/bin/env bash
# 编译并运行 GPCP 的 CGRA-ME 驱动。
#   bash cgra/tools/map.sh <arch.xml> <dfg.dot> <II> [ilp|anneal] [timelimit_s]
# CGRA-ME 在仓库外（EULA 禁止再分发），默认 ~/cgra-me，可用 CGRA_ME_HOME 覆盖。
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
home=${CGRA_ME_HOME:-$HOME/cgra-me}
bin=$here/build/cgrame_map

if [[ ! -f $home/build/lib/libcgra-me.so ]]; then
  echo "CGRA-ME not built at $home" >&2
  exit 2
fi

mkdir -p "$here/build"
if [[ ! -x $bin || $here/cgrame_map.cpp -nt $bin ]]; then
  env -u LD_LIBRARY_PATH g++ -std=c++17 -O2 -I "$home/inc" "$here/cgrame_map.cpp" \
    -L "$home/build/lib" -lcgra-me -lscipopt -Wl,-rpath,"$home/build/lib" -o "$bin"
fi

LD_LIBRARY_PATH=$home/build/lib exec "$bin" "$@"
