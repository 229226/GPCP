"""兼容层：落点逻辑已迁到 backend/gpcp/lower.py（硬件知识归后端）。

保留本文件只是为了让 proto/compile.py 与既有 `make proto` 流程不改一行；
新代码请直接 `from backend.gpcp.lower import ...`。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend.gpcp.lower import (  # noqa: E402,F401
    FFT_SIZES,
    FFT_TAIL,
    FFT_TILE,
    LDPC_ZC,
    NEW_ARRAY_IMAGES,
    POLAR_N,
    POLAR_PES,
    T0_OVERHEAD,
    USE_NEW_ARRAY,
    apply_overrides,
    dumps,
    fft_candidates,
    fft_runs_per_symbol,
    infer_nsym,
    lower,
    polar_decode_runs,
    read_target,
)
