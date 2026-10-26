"""Chi phí GPU ước tính, không phải hóa đơn (09 · GPUProvider, s3.b2; Dữ liệu, chi phí, s7.b1).

Ba thành phần: giá theo giờ ``dph_total`` (compute), disk (``storage_cost`` USD/GB-tháng × dung lượng yêu cầu)
và băng thông (``inet_up_cost``/``inet_down_cost`` USD/GB). Băng thông chưa tính vì không đo được số GB.

GIẢ ĐỊNH CHƯA KIỂM — ``dph_total`` có gồm disk không? Tài liệu vast-cli không nói rõ. vast-cli gửi
``allocated_storage`` khi tìm offer "để tính giá", gợi ý là CÓ gồm; ở đây tạm coi là KHÔNG gồm và cộng disk
riêng: nếu sai thì ước tính cao hơn thực chi (an toàn cho trần chi phí), không bao giờ thấp hơn. Chỉ một lượt
thuê thật ở lát E (so với hóa đơn) chốt được; khi đó đổi ``DPH_TOTAL_INCLUDES_DISK``. Hóa đơn provider là thực chi.
"""

from __future__ import annotations

from datetime import datetime

from studio.gpu.contracts import COMPUTE_BILLABLE, CostSample, Rental

DPH_TOTAL_INCLUDES_DISK = False
HOURS_PER_MONTH = 30 * 24          # đổi USD/GB-tháng sang USD/GB-giờ; tháng 30 ngày (chưa kiểm cách provider tính)
LABEL = "ước tính, không phải hóa đơn"


def rates(rental: Rental) -> tuple[float, float]:
    """(compute USD/giờ, disk USD/giờ) theo trạng thái hiện tại của rental.

    Compute tính khi máy có thể đang chạy (kể cả trạng thái chưa rõ); ``stopped`` chỉ còn disk; ``destroyed``,
    ``create_failed``, ``draft`` bằng 0.
    """
    offer = rental.offer
    disk = 0.0
    if not DPH_TOTAL_INCLUDES_DISK and offer.storage_cost and rental.disk_gb:
        disk = offer.storage_cost * rental.disk_gb / HOURS_PER_MONTH
    if rental.status in COMPUTE_BILLABLE:
        return offer.dph_total, disk
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
            "dph_total_includes_disk": DPH_TOTAL_INCLUDES_DISK, "samples": samples}
