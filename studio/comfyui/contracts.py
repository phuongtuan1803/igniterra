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

import json
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
    kind: str | None = None              # nhãn loại (studio.comfyui.inputs.KINDS); None: revision cũ, chưa gắn
    inputs: list[dict] = []              # input mapping (S17); đổi mapping là revision mới
    inputs_approved_at: datetime | None = None

    @property
    def status(self) -> WorkflowStatus:
        """Manifest quyết định; mapping chưa duyệt kéo revision về ``pending_review`` dù manifest đã duyệt."""
        status = self.manifest_status if self.manifest_sha256 and self.manifest_status else "needs_review"
        if status == "approved" and self.inputs and self.inputs_approved_at is None:
            return "pending_review"
        return status

    @property
    def inputs_pending(self) -> bool:
        return bool(self.inputs) and self.inputs_approved_at is None

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
    inputs_json: str | None = None       # giá trị đã gửi theo mapping (provenance; S19 hiện văn bản gốc)
    group_id: str | None = None          # nhóm lượt tự kiểm (S18)
    attempt: int | None = None
    verdict_json: str | None = None      # kết quả chấm lượt này (model vision hoặc người duyệt)

    @property
    def inputs(self) -> dict:
        return json.loads(self.inputs_json) if self.inputs_json else {}

    @property
    def verdict(self) -> dict | None:
        return json.loads(self.verdict_json) if self.verdict_json else None

    @property
    def gpu_seconds(self) -> float | None:
        """Thời gian từ lúc gửi tới lúc xong: cơ sở của ước tính chi phí lượt (không phải hóa đơn)."""
        if self.submitted_at is None or self.finished_at is None:
            return None
        return max(0.0, (self.finished_at - self.submitted_at).total_seconds())


GroupStatus = Literal["running", "awaiting_review", "passed", "exhausted", "capped", "stopped"]
GROUP_OPEN = ("running", "awaiting_review")


class AttemptGroup(BaseModel):
    """Nhóm lượt tự kiểm và chạy lại (10 · Tự kiểm và chạy lại; S18). Mỗi lượt là một run thường."""

    group_id: str
    workflow_id: str
    revision: int
    rental_id: str
    inputs_json: str                     # input lượt 1; lượt sau chỉ đổi seed
    judge: Literal["vision", "manual"]
    max_attempts: int
    cap_usd: float
    est_attempt_usd: float               # ước tính mỗi lượt lúc bấm Chạy
    spent_usd: float = 0.0               # ước tính đã dùng: thời gian GPU × giá giờ + phí chấm
    attempts: int = 0
    status: GroupStatus
    stop_reason: str | None = None
    chosen_run_id: str | None = None
    version: int = 0
    created_at: datetime
    updated_at: datetime


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
