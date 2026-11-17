"""架构注册表：arch/<name>/{model.yaml, emitter.py}。

约定：
  - 模型只通过 model.yaml 描述；emitter 只实现 Emitter 接口；
  - 编译器核心不 import 具体架构，只有这里按名字动态加载。
"""
from __future__ import annotations

import importlib
from pathlib import Path

from .emit.base import Emitter
from .ir.model import MachineModel, load_model

DEFAULT_ROOT = "arch"


def arch_dir(name: str, root: str | Path = DEFAULT_ROOT) -> Path:
    d = Path(root) / name
    if not d.is_dir():
        raise FileNotFoundError(f"找不到架构目录 {d}")
    return d


def load_arch(name: str, root: str | Path = DEFAULT_ROOT) -> tuple[MachineModel, Emitter]:
    d = arch_dir(name, root)
    model = load_model(d / "model.yaml")
    mod = importlib.import_module(f"arch.{name}.emitter")
    for obj in vars(mod).values():
        if isinstance(obj, type) and issubclass(obj, Emitter) and obj is not Emitter:
            return model, obj()
    raise ImportError(f"{d}/emitter.py 里没有 Emitter 的子类")
