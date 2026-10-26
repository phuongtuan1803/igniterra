"""Kiểu dùng chung của các bước: ngữ cảnh task, mục đang chạy, kết quả trả về luồng chính."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from studio import costs, providers
from studio.contracts import AssetCandidate, Brief, ScenePlan, SeriesBible
from studio.store import Store
from studio.taskdir import TaskDir


class WaitFor(Exception):
    """Bước yêu cầu task chờ người (ví dụ duration_exceeded); không phải lỗi provider."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass
class Item:
    task_id: str
    step: str
    scene: str
    attempt: int
    run_id: int
    note: str | None = None
    feedback: list[str] = field(default_factory=list)

    @property
    def request_id(self) -> str:
        return f"{self.task_id}:{self.scene}:{self.step}:{self.attempt}"


@dataclass
class Result:
    output: Path                              # file đã commit (tuyệt đối)
    candidate: AssetCandidate | None = None   # luồng chính ghi vào assets.json
    plan: ScenePlan | None = None             # bước plan: luồng chính ghi revision


@dataclass
class Ctx:
    task_id: str
    store: Store
    td: TaskDir
    config: dict
    brief: Brief
    bible: SeriesBible
    series: str | None
    series_dir: Path | None
    plan: ScenePlan | None
    plan_rev: int
    _adapters: dict[str, Any] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def adapter(self, capability: str) -> Any:
        with self._lock:
            if capability not in self._adapters:
                self._adapters[capability] = providers.get(capability, self.config)
            return self._adapters[capability]

    def adapter_id(self, capability: str) -> dict:
        adapter = self.adapter(capability)
        if adapter is None:
            return {"adapter": "none"}
        result = {"adapter": getattr(adapter, "name", type(adapter).__name__), "model": getattr(adapter, "model", None)}
        if hasattr(adapter, "input_signature"):
            result["configuration"] = adapter.input_signature
        return result

    @contextmanager
    def call(self, item: Item, adapter: Any) -> Iterator[None]:
        """Lượt gọi trả phí đi qua costs.paid_call; lượt gọi miễn phí đi thẳng."""
        if getattr(adapter, "paid", False):
            with costs.paid_call(self.td.costs_path, item.request_id, adapter.name, getattr(adapter, "model", None)):
                yield
        else:
            yield

    def scene_ids(self) -> list[str]:
        return [scene.id for scene in self.plan.scenes] if self.plan else []
