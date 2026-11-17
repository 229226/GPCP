"""GPCP 4x4 的发射器。

目前复用通用 listing：真实 IMEM 编码（ISA 位域 + spad 布局）后续再接。
保留这个文件是为了给"架构专属发射器"一个明确的落点。
"""
from __future__ import annotations

from cgracc.emit.listing import ListingEmitter


class Gpcp4x4Emitter(ListingEmitter):
    pass
