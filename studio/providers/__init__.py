"""Bảng tên adapter → lớp. Bước lấy adapter bằng ``get(capability, config)``, không import lớp."""

from __future__ import annotations

import importlib
from typing import Any

REGISTRY: dict[str, dict[str, str]] = {
    "llm": {
        "omniroute": "studio.providers.direct.omniroute:OmniRouteLLM",
        "fake": "studio.providers.fake:FakeLLM",
    },
    "image": {
        "omniroute": "studio.providers.direct.omniroute:OmniRouteImage",
        "fake": "studio.providers.fake:FakeImage",
    },
    "tts": {
        "mpt": "studio.providers.mpt.tts:MptTTS",
        "fake": "studio.providers.fake:FakeTTS",
    },
    "subtitles": {
        "mpt": "studio.providers.mpt.subtitle:MptSubtitleAligner",
        "fake": "studio.providers.fake:FakeAligner",
    },
    "video": {
        "fake": "studio.providers.fake:FakeVideo",
    },
    "renderer": {
        "video_project": "studio.render:VideoProjectRenderer",
    },
}


def get(capability: str, config: dict) -> Any:
    """Khởi tạo adapter theo ``config['adapters'][capability]``; ``"none"`` trả None."""
    name = config["adapters"].get(capability, "none")
    if name in (None, "none"):
        return None
    try:
        target = REGISTRY[capability][name]
    except KeyError:
        from studio.providers.base import Fatal

        raise Fatal(f"không có adapter {capability}={name!r}") from None
    module, _, attr = target.partition(":")
    cls = getattr(importlib.import_module(module), attr)
    return cls(config)
