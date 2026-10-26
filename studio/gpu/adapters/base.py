"""GPUProvider Protocol và lỗi chung (09 · GPUProvider và adapter Vast.ai).

Service chỉ thấy Protocol này. Lỗi mutation chia hai loại: ``GPUError`` (chắc chắn không có tác dụng phía
provider) và ``GPUUnknown`` (request có thể đã tới provider; không retry mù, phải reconcile).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from studio.gpu.contracts import Capabilities, Instance, InstanceSpec, Offer, OfferFilters


class GPUError(Exception):
    """Provider từ chối hoặc chưa gửi được; không có thay đổi phía provider."""


class GPUUnknown(GPUError):
    """Timeout hoặc mất response sau khi gửi: kết quả không rõ, tìm theo label trước khi làm tiếp."""


@runtime_checkable
class GPUProvider(Protocol):
    key: str

    def capabilities(self) -> Capabilities: ...

    def search_offers(self, filters: OfferFilters) -> list[Offer]: ...

    def create_instance(self, offer: Offer, spec: InstanceSpec) -> Instance: ...

    def get_instance(self, instance_id: str) -> Instance | None: ...        # None: không còn

    def list_owned_instances(self) -> list[Instance]: ...                    # chỉ label tiền tố igniterra:

    def stop_instance(self, instance_id: str) -> None: ...

    def destroy_instance(self, instance_id: str) -> None: ...               # chỉ là yêu cầu; xác nhận bằng get/list
