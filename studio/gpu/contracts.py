"""Contract GPU thuê (09 · Vòng đời thuê): Offer, Rental, ProvisionStep, GPUEvent.

Trường offer theo tên của Vast.ai (``dph_total``, ``gpu_ram``, ``disk_space``…) nhưng đổi sang đơn vị rõ
(GB, USD/giờ); adapter chịu trách nhiệm quy đổi. Giá ở đây là ước tính, không phải hóa đơn.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

LABEL_PREFIX = "igniterra:"

RentalStatus = Literal["draft", "creating", "create_unknown", "create_failed", "provisioning", "provision_failed",
                       "ready", "busy", "stopped", "destroying", "destroy_unknown", "destroyed"]
# Chuyển trạng thái hợp lệ (sơ đồ s4.b1). ``create_failed``: provider từ chối chắc chắn, không có instance.
# Lát B thêm ngoài sơ đồ: ``stopped`` (route stop của s7.b3; vẫn tính phí storage, chỉ còn đường destroy),
# ``provisioning→destroying`` (hủy khi provisioning treo; lát C chưa có thì rental dừng ở provisioning) và
# ``destroy_unknown→destroying`` (chỉ khi provider đã từ chối chắc chắn lượt destroy trước; service kiểm).
# Lát D thêm ``busy→destroying``: run treo không được giữ máy tính phí mãi; destroy khi còn run chưa có output đã
# kiểm luôn cần xác nhận riêng (G11, ``GPUService.destroy``).
TRANSITIONS: dict[str, set[str]] = {
    "draft": {"creating"},
    "creating": {"provisioning", "create_unknown", "create_failed"},
    "create_unknown": {"provisioning", "create_failed"},
    "provisioning": {"ready", "provision_failed", "stopped", "destroying"},
    "ready": {"busy", "stopped", "destroying"},
    "busy": {"ready", "destroying"},
    "provision_failed": {"stopped", "destroying"},
    "stopped": {"destroying"},
    "destroying": {"destroyed", "destroy_unknown"},
    "destroy_unknown": {"destroyed", "destroying"},
    "create_failed": set(),
    "destroyed": set(),
}
# Trạng thái có thể đang tính phí hoặc chưa rõ: trang GPU báo cần chú ý.
ATTENTION = ("create_unknown", "provision_failed", "destroy_unknown")
BILLABLE = ("creating", "create_unknown", "provisioning", "provision_failed", "ready", "busy", "stopped",
            "destroying", "destroy_unknown")
# Đang chạy máy (tính cả compute); ``stopped`` chỉ còn disk.
COMPUTE_BILLABLE = tuple(s for s in BILLABLE if s != "stopped")

STEP_KEYS = ("instance_create", "host_boot", "transport_ready", "runtime_setup", "custom_nodes", "models",
             "health_smoke", "ready")
StepStatus = Literal["pending", "running", "done", "failed", "skipped", "unknown"]


def label_for(rental_id: str) -> str:
    """Label chủ sở hữu, khóa đối soát với provider (create không có idempotency key)."""
    return f"{LABEL_PREFIX}{rental_id}"


class Capabilities(BaseModel):
    supports_stop: bool
    price_components: tuple[str, ...] = ("compute",)   # compute, storage, inet_up, inet_down
    display_name: str = ""              # tên hiện trên trang /gpu (cột Loại)
    web_username: str | None = None     # user Basic Auth của ComfyUI gốc trên máy thuê; mật khẩu là WEB_PASSWORD
    self_signed_tls: bool = False       # cổng web của máy dùng chứng chỉ tự ký: trình duyệt cảnh báo lần đầu


class OfferFilters(BaseModel):
    """Yêu cầu tối thiểu; dùng để lọc khi tìm và để kiểm lại offer ngay trước create."""

    min_gpu_ram_gb: float = 16
    disk_gb: float = 60                 # dung lượng yêu cầu; offer phải có ít nhất chừng này
    min_reliability: float = 0.95
    min_cuda: float | None = None
    verified_only: bool = True
    max_dph: float | None = None        # USD/giờ
    limit: int = 20


class Offer(BaseModel):
    provider_key: str
    offer_id: str
    gpu_name: str
    num_gpus: int = 1
    gpu_ram_gb: float
    disk_space_gb: float
    reliability: float | None = None
    cuda_max_good: float | None = None
    verified: bool = False
    rentable: bool = True
    dph_total: float                    # USD/giờ theo báo giá; xem cờ gồm disk bên dưới, chưa gồm băng thông
    dph_total_includes_disk: bool = False  # adapter xác nhận báo giá đã gồm disk; snapshot cũ giữ mặc định bảo thủ
    storage_cost: float | None = None   # USD/GB-tháng
    inet_up_cost: float | None = None   # USD/GB
    inet_down_cost: float | None = None
    geolocation: str | None = None
    quoted_at: datetime                 # thời điểm provider báo giá (UTC)


class Quote(BaseModel):
    """Báo giá người vận hành đã thấy và xác nhận trên trang; service kiểm hạn và giá trước create."""

    offer_id: str
    dph_total: float
    quoted_at: datetime
    confirmed: bool = False


class InstanceSpec(BaseModel):
    label: str
    image: str
    disk_gb: float
    cancel_unavail: bool = True         # không có cờ này Vast tạo instance stopped vẫn tính phí disk
    onstart: str | None = None
    # Biến môi trường của container; có ``WEB_PASSWORD`` nên không hiện trong repr và không vào request_hash.
    env: dict[str, str] = Field(default_factory=dict, repr=False)

    @field_validator("image")
    @classmethod
    def pinned(cls, value: str) -> str:
        if "@sha256:" not in value:
            raise ValueError("image phải ghim digest (…@sha256:…)")
        return value

    @field_validator("env")
    @classmethod
    def auth_kept(cls, value: dict[str, str]) -> dict[str, str]:
        """Giữ ``ENABLE_AUTH`` của base-image; không bao giờ mở cổng không xác thực (09 · s5.b2, G12)."""
        if "AUTH_EXCLUDE" in value:
            raise ValueError("không đặt AUTH_EXCLUDE: cổng ComfyUI phải có xác thực")
        if str(value.get("ENABLE_AUTH", "true")).lower() not in ("true", "1"):
            raise ValueError("ENABLE_AUTH phải bật")
        return value


class Instance(BaseModel):
    """Instance phía provider; ``actual_status`` giữ nguyên chữ của provider."""

    provider_instance_id: str
    label: str | None = None
    actual_status: str
    dph_total: float | None = None
    ports: dict[str, str] = Field(default_factory=dict)


class Orphan(BaseModel):
    """Instance có label ``igniterra:`` mà không khớp rental nào còn sống (G10). Chỉ báo, không tự xóa."""

    provider_instance_id: str
    label: str | None = None
    actual_status: str
    dph_total: float | None = None
    reason: str


class CostSample(BaseModel):
    """Mẫu chi phí ước tính (không phải hóa đơn): tốc độ tính từ ``ts`` và tổng ước tính tới ``ts``."""

    rental_id: str
    ts: datetime
    compute_per_h: float
    disk_per_h: float
    accrued_est: float


class Rental(BaseModel):
    rental_id: str
    provider_key: str
    provider_instance_id: str | None = None
    label: str
    offer: Offer
    status: RentalStatus
    version: int = 0
    disk_gb: float | None = None        # dung lượng đã yêu cầu lúc create (tính chi phí disk)
    manifest_sha256: str | None = None  # manifest provisioning đã duyệt gắn lúc thuê (lát C)
    provision_deadline: datetime | None = None  # quá hạn mà chưa ready → provision_failed, destroy
    idle_deadline: datetime | None = None
    destroy_confirmed_at: datetime | None = None
    native_activity_at: datetime | None = None   # lần cuối worker thấy job trong ComfyUI gốc (giai đoạn 2)
    native_history_head: str | None = None       # prompt mới nhất trong /history ở lượt đọc trước
    created_at: datetime
    updated_at: datetime


class ProvisionStep(BaseModel):
    rental_id: str
    step_key: str
    attempt: int = 0
    status: StepStatus = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_code: str | None = None
    bytes_done: int | None = None
    bytes_total: int | None = None      # không có tổng thì UI không hiện phần trăm
    log_ref: str | None = None


class ManifestRecord(BaseModel):
    """Manifest provisioning trong ``gpu_manifests``: ``pending_review`` → ``approved``; chỉ bản approved được thuê."""

    manifest_sha256: str
    status: Literal["pending_review", "approved", "revoked"]
    manifest_json: str
    note: str | None = None
    created_at: datetime
    approved_at: datetime | None = None


class GPUEvent(BaseModel):
    """Chỉ thêm; ``detail`` đã che bí mật trước khi ghi."""

    rental_id: str
    ts_utc: datetime
    transition: str                     # "creating→create_unknown" hoặc tên sự kiện
    error_code: str | None = None
    detail: str | None = None
