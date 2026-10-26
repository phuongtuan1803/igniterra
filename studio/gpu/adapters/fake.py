"""FakeGPUProvider: bản giả trong bộ nhớ, tất định, 0 USD, không gọi mạng (09 · Kiểm chứng G01–G12).

Mô phỏng lỗi bằng ``fail[method] = mode`` (dùng một lần):

- ``create_instance``: ``timeout_after_send`` (instance đã tạo, response mất → ``GPUUnknown``),
  ``timeout_before_send`` (request không tới provider nhưng client cũng chỉ thấy timeout → ``GPUUnknown``),
  ``reject`` (provider từ chối chắc chắn → ``GPUError``).
- ``destroy_instance`` (G09): ``timeout_after_send`` (đã hủy, response mất), ``timeout_before_send`` (chưa hủy,
  client chỉ thấy timeout), ``reject`` (chưa hủy, ``GPUError``), ``linger`` (nhận yêu cầu, instance còn ở
  ``destroying`` tới khi gọi ``finish_destroying``, như API thật chỉ trả "destroying").
- ``stop_instance``: ``timeout_after_send``, ``timeout_before_send``, ``reject``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from studio.gpu.adapters.base import GPUError, GPUUnknown
from studio.gpu.contracts import LABEL_PREFIX, Capabilities, Instance, InstanceSpec, Offer, OfferFilters

# offer_id, gpu, VRAM GB, disk GB, reliability, cuda, verified, USD/giờ
OFFERS = [
    ("1001", "RTX 3060", 12, 20, 0.97, 12.4, True, 0.06),   # rẻ nhất nhưng thiếu VRAM và disk (G02)
    ("1002", "RTX 4090", 24, 120, 0.99, 12.4, True, 0.35),
    ("1003", "RTX 4090", 24, 40, 0.99, 12.4, True, 0.29),   # đủ VRAM, thiếu disk
    ("1004", "A100 SXM4", 80, 200, 0.995, 12.2, True, 1.10),
    ("1005", "RTX 3090", 24, 150, 0.90, 12.0, False, 0.18), # chưa verified, reliability thấp
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FakeGPUProvider:
    key = "fake"

    def __init__(self, config: dict | None = None, *, clock: Callable[[], datetime] = utc_now,
                 honor_filters: bool = True):
        self.clock = clock
        self.honor_filters = honor_filters      # False: provider trả cả offer không khớp bộ lọc
        self.prices = {row[0]: row[7] for row in OFFERS}
        self.instances: dict[str, Instance] = {}
        self.specs: dict[str, InstanceSpec] = {}    # spec đã nhận (env có WEB_PASSWORD; chỉ test đọc)
        self.calls: list[tuple] = []
        self.fail: dict[str, str] = {}
        self._next_id = 1

    def capabilities(self) -> Capabilities:
        return Capabilities(supports_stop=True, price_components=("compute", "storage", "inet_up", "inet_down"))

    def search_offers(self, filters: OfferFilters) -> list[Offer]:
        self.calls.append(("search_offers",))
        stamp = self.clock()
        offers = [Offer(provider_key=self.key, offer_id=oid, gpu_name=gpu, gpu_ram_gb=vram, disk_space_gb=disk,
                        reliability=rel, cuda_max_good=cuda, verified=verified, dph_total=self.prices[oid],
                        storage_cost=0.10, inet_up_cost=0.01, inet_down_cost=0.01, quoted_at=stamp)
                  for oid, gpu, vram, disk, rel, cuda, verified, _ in OFFERS]
        if self.honor_filters:
            offers = [o for o in offers if o.gpu_ram_gb >= filters.min_gpu_ram_gb and o.disk_space_gb >= filters.disk_gb
                      and (o.reliability or 0) >= filters.min_reliability and (o.verified or not filters.verified_only)
                      and (filters.max_dph is None or o.dph_total <= filters.max_dph)]
        return sorted(offers, key=lambda o: o.dph_total)[:filters.limit]

    def create_instance(self, offer: Offer, spec: InstanceSpec) -> Instance:
        self.calls.append(("create_instance", offer.offer_id, spec.label))
        mode = self.fail.pop("create_instance", None)
        if mode == "reject":
            raise GPUError(f"offer {offer.offer_id} không còn cho thuê")
        if mode == "timeout_before_send":
            raise GPUUnknown("timeout khi tạo instance")
        instance = Instance(provider_instance_id=f"fake-{self._next_id}", label=spec.label, actual_status="loading",
                            dph_total=self.prices[offer.offer_id])
        self._next_id += 1
        self.instances[instance.provider_instance_id] = instance
        self.specs[instance.provider_instance_id] = spec
        if mode == "timeout_after_send":
            raise GPUUnknown("timeout khi tạo instance")
        return instance

    def get_instance(self, instance_id: str) -> Instance | None:
        self.calls.append(("get_instance", instance_id))
        return self.instances.get(instance_id)

    def list_owned_instances(self) -> list[Instance]:
        self.calls.append(("list_owned_instances",))
        return [i for i in self.instances.values() if (i.label or "").startswith(LABEL_PREFIX)]

    def stop_instance(self, instance_id: str) -> None:
        self.calls.append(("stop_instance", instance_id))
        mode = self.fail.pop("stop_instance", None)
        if mode == "reject":
            raise GPUError(f"provider từ chối stop {instance_id}")
        if mode == "timeout_before_send":
            raise GPUUnknown("timeout khi stop instance")
        if instance_id not in self.instances:
            raise GPUError(f"không có instance {instance_id}")
        self.instances[instance_id] = self.instances[instance_id].model_copy(update={"actual_status": "stopped"})
        if mode == "timeout_after_send":
            raise GPUUnknown("timeout khi stop instance")

    def destroy_instance(self, instance_id: str) -> None:
        self.calls.append(("destroy_instance", instance_id))
        mode = self.fail.pop("destroy_instance", None)
        if mode == "reject":
            raise GPUError(f"provider từ chối destroy {instance_id}")
        if mode == "timeout_before_send":
            raise GPUUnknown("timeout khi destroy instance")
        if mode == "linger" and instance_id in self.instances:
            self.instances[instance_id] = self.instances[instance_id].model_copy(update={"actual_status": "destroying"})
            return
        self.instances.pop(instance_id, None)
        if mode == "timeout_after_send":
            raise GPUUnknown("timeout khi destroy instance")

    # ------------------------------------------------------------ chỉ dành cho test

    def boot(self, instance_id: str, ports: dict[str, str] | None = None, status: str = "running") -> None:
        """Host lên (``actual_status``) và có cổng ``"8188/tcp" → "host:port"`` như provider báo."""
        update = {"actual_status": status}
        if ports is not None:
            update["ports"] = ports
        self.instances[instance_id] = self.instances[instance_id].model_copy(update=update)

    def finish_destroying(self) -> None:
        """Instance ``destroying`` biến mất (provider hủy xong)."""
        for instance_id in [k for k, v in self.instances.items() if v.actual_status == "destroying"]:
            del self.instances[instance_id]

    def destroyed(self) -> int:
        return sum(call[0] == "destroy_instance" for call in self.calls)

    def reprice(self, offer_id: str, dph_total: float) -> None:
        self.prices[offer_id] = dph_total

    def created(self) -> int:
        return sum(call[0] == "create_instance" for call in self.calls)
