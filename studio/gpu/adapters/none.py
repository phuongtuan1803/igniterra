"""NoRentalProvider: ``[gpu] provider = "fixed"``, chỉ dùng máy cố định (VPS có GPU), không thuê máy nào.

Không gọi mạng, không có offer, không tạo hay hủy được instance. Máy cố định đi qua ``GPUService.add_fixed_host``
(IGN-UX giai đoạn 1), không qua adapter.
"""

from __future__ import annotations

from studio.gpu.adapters.base import GPUError
from studio.gpu.contracts import Capabilities, Instance, InstanceSpec, Offer, OfferFilters


class NoRentalProvider:
    key = "none"

    def __init__(self, config: dict | None = None):
        pass

    def capabilities(self) -> Capabilities:
        return Capabilities(supports_stop=False)

    def search_offers(self, filters: OfferFilters) -> list[Offer]:
        return []

    def create_instance(self, offer: Offer, spec: InstanceSpec) -> Instance:
        raise GPUError("[gpu] provider = \"fixed\": không thuê máy; chọn \"vast\" để thuê")

    def get_instance(self, instance_id: str) -> Instance | None:
        return None

    def list_owned_instances(self) -> list[Instance]:
        return []

    def stop_instance(self, instance_id: str) -> None:
        raise GPUError("không có máy thuê để stop")

    def destroy_instance(self, instance_id: str) -> None:
        raise GPUError("không có máy thuê để destroy")
