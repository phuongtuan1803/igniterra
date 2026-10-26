"""Contract ComfyUI Workflows (09 · ComfyUI workflow và lượt chạy): revision workflow, manifest dependency, run.

- Revision workflow bất biến: ``workflow_id`` + ``revision`` + SHA-256 của file gốc. Thứ duy nhất đổi được là
  manifest gắn vào (khi manifest đang gắn chưa được duyệt).
- Trạng thái revision suy từ manifest: chưa có manifest → ``needs_review`` (không xác định được dependency, không
  đoán, G06); có manifest → trạng thái của manifest (``pending_review`` | ``approved`` | ``revoked``).
- Run: ``checking`` → ``submitting`` (``prompt_id`` đã ghi trước ``POST /prompt``) → ``queued``/``running`` →
  ``downloading`` → ``succeeded`` chỉ khi mọi output đã tải và kiểm (G11). Kết quả submit không rõ là
  ``submit_unknown``: tra ``/queue`` và ``/history/{prompt_id}``, không submit lại (G08).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

WorkflowStatus = Literal["needs_review", "pending_review", "approved", "revoked"]
RunStatus = Literal["checking", "submitting", "submit_unknown", "queued", "running", "downloading", "output_failed",
                    "succeeded", "failed", "blocked", "abandoned"]

# Rental đang ``busy`` vì run ở các trạng thái này (GPU còn chạy hoặc có thể còn chạy prompt).
RUN_EXECUTING = ("checking", "submitting", "submit_unknown", "queued", "running")
# Output chưa tải và kiểm: destroy rental phải cảnh báo và cần xác nhận riêng (G11).
RUN_UNRESOLVED = (*RUN_EXECUTING, "downloading", "output_failed")
RUN_TERMINAL = ("succeeded", "failed", "blocked", "abandoned")

# Chuyển trạng thái hợp lệ; cùng trạng thái (``queued→queued``) là cập nhật tiến độ có CAS theo version.
# ``abandoned`` chỉ do destroy rental đã xác nhận bỏ output chưa kiểm (``GPUService.destroy``).
RUN_TRANSITIONS: dict[str, set[str]] = {
    "checking": {"submitting", "blocked", "abandoned"},
    "submitting": {"queued", "running", "submit_unknown", "downloading", "failed", "abandoned"},
    "submit_unknown": {"submit_unknown", "queued", "running", "downloading", "failed", "abandoned"},
    "queued": {"queued", "running", "downloading", "failed", "abandoned"},
    "running": {"running", "downloading", "failed", "abandoned"},
    "downloading": {"downloading", "succeeded", "output_failed", "failed", "abandoned"},
    "output_failed": {"downloading", "abandoned"},
    "succeeded": set(),
    "failed": set(),
    "blocked": set(),
    "abandoned": set(),
}


class ComfyManifestRecord(BaseModel):
    """Manifest dependency trong ``comfy_manifests`` (khóa SHA-256 của JSON chuẩn hóa)."""

    manifest_sha256: str
    status: Literal["pending_review", "approved", "revoked"]
    manifest_json: str
    note: str | None = None
    created_at: datetime
    approved_at: datetime | None = None


class WorkflowRevision(BaseModel):
    workflow_id: str
    revision: int
    name: str
    format: Literal["gui", "api"]
    source_sha256: str                   # SHA-256 của file gốc đã nhập
    api_sha256: str | None = None        # SHA-256 của file API (chính file gốc khi format = api)
    class_types: list[str]
    draft: dict                          # dependency trích được, ``gaps`` (lý do needs_review), ``hints``
    manifest_sha256: str | None = None
    manifest_source: str | None = None   # extracted | operator
    manifest_status: str | None = None
    version: int = 0
    note: str | None = None
    created_at: datetime

    @property
    def status(self) -> WorkflowStatus:
        return self.manifest_status if self.manifest_sha256 and self.manifest_status else "needs_review"

    @property
    def runnable(self) -> bool:
        return self.api_sha256 is not None


class Run(BaseModel):
    run_id: str
    workflow_id: str
    revision: int
    workflow_sha256: str                 # SHA-256 của prompt API đã gửi
    manifest_sha256: str
    rental_id: str
    prompt_id: str | None = None
    status: RunStatus
    version: int = 0
    error_code: str | None = None
    detail: str | None = None
    queue_state: str | None = None       # running | pending theo /queue
    current_node: str | None = None
    last_event: str | None = None
    last_event_at: datetime | None = None
    lookup_misses: int = 0
    download_attempts: int = 0
    submitted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class RunOutput(BaseModel):
    run_id: str
    node: str
    subfolder: str
    filename: str
    folder_type: str
    status: Literal["pending", "verified", "failed"]
    path: str | None = None              # tương đối dưới data_dir: comfyui/runs/<run_id>/…
    bytes: int | None = None
    sha256: str | None = None
    media_type: str | None = None
    error: str | None = None
    verified_at: datetime | None = None


class RunEvent(BaseModel):
    run_id: str
    ts_utc: datetime
    transition: str
    error_code: str | None = None
    detail: str | None = None
