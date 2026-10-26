"""Guardrail mềm của một rental: idle TTL, ``max_runtime_minutes``, ``max_hourly_usd`` (09 · Vòng đời thuê, s4.b2).

Đây là giới hạn do worker thực thi, không phải giới hạn phía provider: khi worker không chạy hoặc mất kết nối với
provider, tiền vẫn chạy. Lưới an toàn cuối là số dư nạp trước nhỏ trên tài khoản provider.

- **Idle TTL** (``idle_ttl_minutes``): rental ``ready`` không có hoạt động (run giành máy, run nhả máy, studio gọi
  runtime ComfyUI của rental) quá hạn ``gpu_rentals.idle_deadline``. ``busy`` không tính idle: run đang chạy.
- **Thời gian thuê** (``max_runtime_minutes``): đồng hồ tường từ lúc tạo rental (lúc gửi create; sớm hơn lúc
  provider bắt đầu tính, nên nghiêng về an toàn).
- **Giá theo giờ** (``max_hourly_usd``): compute + disk ước tính; chặn lúc thuê, và báo vượt khi cấu hình bị siết lại.
- **Ngân sách** (``max_total_usd``, mặc định ``max_hourly_usd × max_runtime_minutes / 60``): chi phí ước tính cộng
  dồn từ ``gpu_cost_samples``.

Hàm ở đây thuần (không I/O): service dùng để thực thi, trang ``/gpu`` dùng để hiện cùng một kết quả.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from studio.gpu.contracts import Rental

# Trạng thái guardrail xét. ``stopped`` không có: người vận hành đã chủ động dừng máy (chỉ còn phí disk), tự destroy
# sẽ mất dữ liệu trên đĩa. ``destroying``/``destroy_unknown`` đi đường reconcile (G09), không gửi destroy lần nữa.
ENFORCED = ("provisioning", "provision_failed", "ready", "busy")
IDLE_STATES = ("ready",)

BreachKind = Literal["idle_ttl", "max_runtime", "hourly_rate", "budget"]


class Limits(BaseModel):
    """Giới hạn từ ``[gpu.limits]``; ``None`` tắt riêng giới hạn đó (không khuyến nghị)."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    idle_ttl_minutes: float | None = Field(default=30, gt=0)
    max_runtime_minutes: float | None = Field(default=240, gt=0)
    max_hourly_usd: float | None = Field(default=2.0, gt=0)
    max_total_usd: float | None = Field(default=None, gt=0)

    def budget_usd(self) -> float | None:
        if self.max_total_usd is not None:
            return self.max_total_usd
        if self.max_hourly_usd is not None and self.max_runtime_minutes is not None:
            return round(self.max_hourly_usd * self.max_runtime_minutes / 60, 4)
        return None


class Breach(BaseModel):
    kind: BreachKind
    detail: str


class Enforcement(BaseModel):
    """Kết quả một lượt guardrail cho một rental vượt giới hạn.

    ``outcome``: ``destroy_sent`` (đã CAS ``→destroying`` và gửi destroy), ``blocked_unverified_output`` (G11: còn
    run có output chưa tải/kiểm, không tự destroy, cần người vận hành), ``refused`` (destroy bị từ chối vì lý do
    khác, ví dụ chưa gắn instance), ``cleared`` (đọc lại thì không còn vượt: có hoạt động mới hoặc bên khác đã xử lý).
    """

    rental_id: str
    status: str
    breaches: list[Breach]
    outcome: Literal["destroy_sent", "blocked_unverified_output", "refused", "cleared"]
    runs: list[str] = Field(default_factory=list)
    detail: str | None = None


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def idle_deadline(rental: Rental, limits: Limits) -> datetime | None:
    """Hạn idle của rental; rental chưa có ``idle_deadline`` (tạo trước lát E) tính từ lần đổi trạng thái cuối."""
    if limits.idle_ttl_minutes is None:
        return None
    if rental.idle_deadline is not None:
        return aware(rental.idle_deadline)
    return aware(rental.updated_at) + timedelta(minutes=limits.idle_ttl_minutes)


def check(rental: Rental, cost: dict, limits: Limits, now: datetime) -> list[Breach]:
    """Các giới hạn rental đang vượt; rỗng là trong giới hạn. ``cost`` là ``GPUService.cost()`` (ước tính)."""
    if rental.status not in ENFORCED:
        return []
    breaches = []
    deadline = idle_deadline(rental, limits)
    if rental.status in IDLE_STATES and deadline is not None and now >= deadline:
        idle_for = (now - deadline).total_seconds() / 60 + (limits.idle_ttl_minutes or 0)
        breaches.append(Breach(kind="idle_ttl", detail=f"không hoạt động ~{idle_for:.0f} phút ≥ idle TTL "
                                                      f"{limits.idle_ttl_minutes:g} phút"))
    if limits.max_runtime_minutes is not None:
        minutes = (now - aware(rental.created_at)).total_seconds() / 60
        if minutes >= limits.max_runtime_minutes:
            breaches.append(Breach(kind="max_runtime", detail=f"đã thuê {minutes:.0f} phút ≥ max_runtime_minutes "
                                                              f"{limits.max_runtime_minutes:g}"))
    rate = float(cost.get("compute_per_h", 0)) + float(cost.get("disk_per_h", 0))
    if limits.max_hourly_usd is not None and rate > limits.max_hourly_usd:
        breaches.append(Breach(kind="hourly_rate", detail=f"giá ước tính ${rate:.3f}/giờ > max_hourly_usd "
                                                          f"${limits.max_hourly_usd:.3f}"))
    budget = limits.budget_usd()
    accrued = float(cost.get("accrued_est", 0))
    if budget is not None and accrued >= budget:
        breaches.append(Breach(kind="budget", detail=f"chi phí ước tính ${accrued:.3f} ≥ ngân sách ${budget:.3f}"))
    return breaches
