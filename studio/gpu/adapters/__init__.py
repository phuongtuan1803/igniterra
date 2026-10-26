"""Bảng tên adapter GPU → lớp, chọn bằng ``[gpu] provider``. Service không import lớp cụ thể."""

from __future__ import annotations

import importlib

from studio.gpu.adapters.base import GPUError, GPUProvider

REGISTRY: dict[str, str] = {
    "fake": "studio.gpu.adapters.fake:FakeGPUProvider",
    "vast": "studio.gpu.adapters.vast:VastGPUProvider",
}


def get(config: dict) -> GPUProvider | None:
    """Khởi tạo adapter theo ``config['gpu']['provider']``; ``"none"`` trả None (D-05 chưa mở)."""
    name = config.get("gpu", {}).get("provider", "none")
    if name in (None, "none"):
        return None
    try:
        target = REGISTRY[name]
    except KeyError:
        raise GPUError(f"không có GPU provider {name!r}") from None
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)(config)
