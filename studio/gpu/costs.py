"""Chi phí GPU ước tính, không phải hóa đơn (09 · GPUProvider, s3.b2; Dữ liệu, chi phí, s7.b1).

Ba thành phần: giá theo giờ ``dph_total`` (compute), disk (``storage_cost`` USD/GB-tháng × dung lượng yêu cầu)
và băng thông (``inet_up_cost``/``inet_down_cost`` USD/GB). Băng thông chưa tính vì không đo được số GB.

Lát E đã kiểm response thật: báo giá có ``dph_total = dph_base + storage_total_cost`` khi gửi
``allocated_storage``. Adapter đánh dấu ``Offer.dph_total_includes_disk``; khi có cờ thì tách disk ra khỏi
compute để không cộng hai lần. Snapshot cũ và provider chưa khai báo giữ cách cộng disk bảo thủ.
Số dư và báo giá không thay thế hóa đơn; băng thông chưa tính vì không đo được số GB.
"""

from __future__ import annotations

from datetime import datetime

from studio.gpu.contracts import COMPUTE_BILLABLE, CostSample, Offer, Rental

HOURS_PER_MONTH = 30 * 24          # đổi USD/GB-tháng sang USD/GB-giờ; tháng 30 ngày (chưa kiểm cách provider tính)
LABEL = "ước tính, không phải hóa đơn"


def disk_per_h(offer: Offer, disk_gb: float | None) -> float:
    if not offer.storage_cost or not disk_gb:
        return 0.0
    return offer.storage_cost * disk_gb / HOURS_PER_MONTH


def hourly(offer: Offer, disk_gb: float | None) -> float:
    """Giá ước tính USD/giờ khi máy chạy (compute + disk); dùng để chặn offer vượt ``max_hourly_usd`` lúc thuê."""
    return offer.dph_total if offer.dph_total_includes_disk else offer.dph_total + disk_per_h(offer, disk_gb)


def rates(rental: Rental) -> tuple[float, float]:
    """(compute USD/giờ, disk USD/giờ) theo trạng thái hiện tại của rental.

    Compute tính khi máy có thể đang chạy (kể cả trạng thái chưa rõ); ``stopped`` chỉ còn disk; ``destroyed``,
    ``create_failed``, ``draft`` bằng 0.
    """
    offer = rental.offer
    disk = disk_per_h(offer, rental.disk_gb)
    if rental.status in COMPUTE_BILLABLE:
        compute = max(offer.dph_total - disk, 0) if offer.dph_total_includes_disk else offer.dph_total
        return compute, disk
    if rental.status == "stopped":
        return 0.0, disk
    return 0.0, 0.0


def accrued(last: CostSample | None, now: datetime) -> float:
    """Tổng ước tính tới ``now``: tổng ở mẫu cuối + tốc độ của mẫu cuối × số giờ từ đó."""
    if last is None:
        return 0.0
    hours = max((now - last.ts).total_seconds(), 0.0) / 3600
    return last.accrued_est + (last.compute_per_h + last.disk_per_h) * hours


def summary(samples: list[CostSample], now: datetime) -> dict:
    last = samples[-1] if samples else None
    return {"label": LABEL, "accrued_est": accrued(last, now),
            "compute_per_h": last.compute_per_h if last else 0.0, "disk_per_h": last.disk_per_h if last else 0.0,
            "samples": samples}
