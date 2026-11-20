"""架构注册表：arch/<name>/{model.yaml, emitter.py}。

约定：
  - 模型只通过 model.yaml 描述；
  - emitter 只实现 Emitter 接口；同族架构可以**不写** emitter.py，
    自动回退到通用 listing（这就是"只给 YAML 就能接入"的那条路径）；
  - 编译器核心不 import 具体架构，只有这里按名字动态加载。
"""
from __future__ import annotations

import importlib
from pathlib import Path

from .emit.base import Emitter
from .emit.listing import ListingEmitter
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

    if not (d / "emitter.py").is_file():
        return model, ListingEmitter()

    mod = importlib.import_module(f"arch.{name}.emitter")
    for obj in vars(mod).values():
        if (isinstance(obj, type) and issubclass(obj, Emitter)
                and obj is not Emitter and obj.__module__ == mod.__name__):
            return model, obj()
    # 有 emitter.py 但没定义子类：按缺失处理，回退到通用 listing
    return model, ListingEmitter()
