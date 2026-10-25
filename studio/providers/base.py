"""5 provider interface và 4 loại lỗi chung (05 · Provider interface).

Bước chỉ thấy các Protocol này; adapter cụ thể được chọn bằng cấu hình qua
``studio.providers.get``. Adapter trả phí đặt ``paid = True``: bước ghi dòng
``pending`` vào costs.jsonl trước khi gọi, adapter báo lượng dùng bằng
``studio.costs.report_usage``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from studio.contracts import AssetCandidate, AudioTrack, Brief, ScenePlan, SeriesBible, VoiceSpec


class ProviderError(Exception):
    """Gốc của 4 loại lỗi chung; worker chỉ hiểu 4 lớp con dưới đây."""


class Retryable(ProviderError):
    """Chắc chắn chưa gửi (không kết nối được, 429 bị từ chối) hoặc lượt gọi không trả phí lỗi tạm."""


class QualityFail(ProviderError):
    """Có kết quả nhưng không đạt kiểm tra; ``reason`` được đưa vào prompt của lượt sau."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ContentRefused(QualityFail):
    """Provider từ chối nội dung trước khi sinh (moderation): không tính phí; viết lại prompt hoặc người sửa visual."""


class Unconfirmed(ProviderError):
    """Lượt gọi trả phí có thể đã tới provider nhưng không có kết quả chắc chắn."""


class Fatal(ProviderError):
    """Cấu hình sai, khóa sai, nội dung bị từ chối: task failed."""


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str | None
    paid: bool

    def generate_json(self, prompt: str, schema: dict, name: str) -> dict: ...  # structured output; bước dựng ScenePlan


@runtime_checkable
class ImageProvider(Protocol):
    name: str
    model: str | None
    paid: bool

    def generate(self, prompt: str, refs: list[Path], size: str, out: Path) -> AssetCandidate: ...


@runtime_checkable
class TTSProvider(Protocol):
    name: str
    model: str | None
    paid: bool

    def synthesize(self, seg_id: str, text: str, voice: VoiceSpec, out: Path) -> AudioTrack: ...


@runtime_checkable
class SubtitleAligner(Protocol):
    name: str

    def align(self, audio: Path, script: str) -> list[dict]: ...  # [{start, end, text}] theo từ


@runtime_checkable
class VideoProvider(Protocol):
    name: str
    model: str | None
    paid: bool

    def submit(self, image: Path, prompt: str, seconds: int) -> str: ...

    def poll(self, job_id: str, out: Path) -> AssetCandidate | None: ...


@runtime_checkable
class Renderer(Protocol):
    name: str

    def render(self, project_dir: Path, project_id: str, scenes: list[dict]) -> dict: ...
