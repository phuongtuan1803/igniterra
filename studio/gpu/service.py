"""Vòng đời thuê GPU (09 · Vòng đời thuê). Web và worker chỉ gọi lớp này; provider chỉ qua ``GPUProvider``.

Cùng nguyên tắc với ``Unconfirmed`` của pipeline (ADR-0006): ghi intent vào SQLite trước mutation; kết quả
không rõ thì đối soát theo label ``igniterra:<rental_id>``, không tự gọi create lần nữa. Khóa CAS theo
``rental_id`` bằng cột ``version``.

Lát A: kiểm offer và báo giá, create, ``create_unknown`` → tìm theo label. Lát B: stop, destroy và
``destroy_unknown`` (G09), đối soát khi khởi động lại và báo orphan (G10), chi phí ước tính. Provisioning thuộc
lát C; idle TTL do worker thực thi thuộc lát E.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable

from pydantic import ValidationError

from studio import config as studio_config
from studio.gpu import adapters, costs
from studio.gpu.adapters.base import GPUError, GPUProvider, GPUUnknown
from studio.gpu.contracts import (
    STEP_KEYS,
    TRANSITIONS,
    CostSample,
    GPUEvent,
    Instance,
    InstanceSpec,
    Offer,
    OfferFilters,
    Orphan,
    ProvisionStep,
    Quote,
    Rental,
    label_for,
)
from studio.store import Store

QUOTE_TTL_S = 300
RENTAL_ID = re.compile(r"r_[0-9a-f]{12}")
STOPPED = ("stopped", "exited")        # actual_status coi như đã dừng máy
log = logging.getLogger("studio.gpu")


class GPUServiceError(Exception):
    """Từ chối trước khi gọi mutation của provider (offer không đạt, báo giá hết hạn, rental_id trùng…)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def check_offer(offer: Offer, filters: OfferFilters) -> list[str]:
    """Kiểm lại offer theo yêu cầu; không tin bộ lọc phía provider (G02)."""
    problems = []
    if not offer.rentable:
        problems.append("offer không còn cho thuê")
    if filters.verified_only and not offer.verified:
        problems.append("host chưa verified")
    if offer.gpu_ram_gb < filters.min_gpu_ram_gb:
        problems.append(f"VRAM {offer.gpu_ram_gb:g} GB < {filters.min_gpu_ram_gb:g} GB")
    if offer.disk_space_gb < filters.disk_gb:
        problems.append(f"disk {offer.disk_space_gb:g} GB < {filters.disk_gb:g} GB")
    if (offer.reliability or 0) < filters.min_reliability:
        problems.append(f"reliability {offer.reliability} < {filters.min_reliability}")
    if filters.min_cuda is not None and (offer.cuda_max_good or 0) < filters.min_cuda:
        problems.append(f"CUDA {offer.cuda_max_good} < {filters.min_cuda}")
    if filters.max_dph is not None and offer.dph_total > filters.max_dph:
        problems.append(f"giá ${offer.dph_total:.3f}/giờ > ${filters.max_dph:.3f}/giờ")
    return problems


class GPUService:
    def __init__(self, store: Store, provider: GPUProvider, *, image: str = "", quote_ttl_s: float = QUOTE_TTL_S,
                 clock: Callable[[], datetime] = utc_now):
        self.store = store
        self.provider = provider
        self.image = image
        self.quote_ttl_s = quote_ttl_s
        self.clock = clock
        self.orphans: list[Orphan] = []             # kết quả find_orphans() gần nhất (trang /gpu hiện)
        self.orphans_checked_at: datetime | None = None
        self.orphans_error: str | None = None

    @property
    def provider_key(self) -> str:
        return self.provider.key

    @property
    def supports_stop(self) -> bool:
        return self.provider.capabilities().supports_stop

    def new_rental_id(self) -> str:
        """Sinh trước khi hiện form thuê; dùng làm nonce chống bấm hai lần."""
        return f"r_{secrets.token_hex(6)}"

    # ------------------------------------------------------------ offer, báo giá

    def search_offers(self, filters: OfferFilters) -> list[Offer]:
        return [offer for offer in self.provider.search_offers(filters) if not check_offer(offer, filters)]

    def check_quote(self, quote: Quote, offer: Offer | None = None) -> list[str]:
        """Báo giá phải được xác nhận, còn hạn và (khi có offer mới) cùng giá (G03)."""
        problems = []
        if not quote.confirmed:
            problems.append("báo giá chưa được xác nhận")
        quoted_at = quote.quoted_at if quote.quoted_at.tzinfo else quote.quoted_at.replace(tzinfo=timezone.utc)
        age = (self.clock() - quoted_at).total_seconds()
        if age > self.quote_ttl_s:
            problems.append(f"báo giá quá hạn ({age:.0f}s > {self.quote_ttl_s:.0f}s); tìm offer lại")
        elif age < -60:
            problems.append("thời điểm báo giá ở tương lai")
        if offer is not None and round(offer.dph_total, 6) != round(quote.dph_total, 6):
            problems.append(f"giá đổi ${quote.dph_total:.3f} → ${offer.dph_total:.3f}/giờ; xác nhận lại")
        return problems

    # ------------------------------------------------------------ create

    def rent(self, rental_id: str, quote: Quote, filters: OfferFilters) -> Rental:
        """Thuê một offer đã báo giá. Mọi kiểm tra chạy trước khi ghi intent và gọi ``create_instance``."""
        if not RENTAL_ID.fullmatch(rental_id):
            raise GPUServiceError(f"rental_id lạ: {rental_id!r}")
        problems = self.check_quote(quote)
        if problems:
            raise GPUServiceError("; ".join(problems))
        offer = next((o for o in self.provider.search_offers(filters) if o.offer_id == quote.offer_id), None)
        if offer is None:
            raise GPUServiceError(f"offer {quote.offer_id} không còn hoặc không khớp yêu cầu")
        problems = check_offer(offer, filters) + self.check_quote(quote, offer)
        if problems:
            raise GPUServiceError(f"offer {offer.offer_id} bị loại: " + "; ".join(problems))
        try:
            spec = InstanceSpec(label=label_for(rental_id), image=self.image, disk_gb=filters.disk_gb)
        except ValidationError as exc:
            raise GPUServiceError(f"cấu hình [gpu] image: {exc.errors()[0]['msg']}") from None
        try:
            self.store.add_rental(rental_id, self.provider_key, spec.label, offer.model_dump_json(), STEP_KEYS,
                                  disk_gb=spec.disk_gb)
        except sqlite3.IntegrityError:
            raise GPUServiceError(f"{rental_id} đã được dùng; không tạo lại (bấm hai lần?)") from None

        op_id = uuid.uuid4().hex
        request_hash = hashlib.sha256(json.dumps({"offer_id": offer.offer_id, "spec": spec.model_dump()},
                                                 sort_keys=True).encode()).hexdigest()
        self.store.start_gpu_operation(op_id, rental_id, "create", request_hash)
        if not self._move(self.rental(rental_id), "creating"):
            raise GPUServiceError(f"{rental_id} đã đổi trạng thái ở nơi khác")
        self.store.set_provision_step(rental_id, "instance_create", "running")
        try:
            instance = self.provider.create_instance(offer, spec)
        except GPUUnknown as exc:
            self.store.finish_gpu_operation(op_id, "unknown")
            self.store.set_provision_step(rental_id, "instance_create", "unknown", "create_timeout")
            if not self._move(self.rental(rental_id), "create_unknown", error_code="create_timeout", detail=str(exc)):
                raise GPUServiceError(f"{rental_id} đã đổi trạng thái ở nơi khác") from None
            return self.reconcile(rental_id)
        except GPUError as exc:
            self.store.finish_gpu_operation(op_id, "rejected")
            self.store.set_provision_step(rental_id, "instance_create", "failed", "create_rejected")
            if not self._move(self.rental(rental_id), "create_failed", error_code="create_rejected", detail=str(exc)):
                raise GPUServiceError(f"{rental_id} đã đổi trạng thái ở nơi khác") from None
            return self.rental(rental_id)
        if instance.label != spec.label:
            self.store.add_gpu_event(rental_id, "label_mismatch", "label_mismatch",
                                     f"provider trả label {instance.label!r}")
        rental = self.rental(rental_id)
        if not self._move(rental, "provisioning", provider_instance_id=instance.provider_instance_id):
            raise GPUServiceError(f"{rental_id} đã đổi trạng thái ở nơi khác")
        self._confirm_create(rental_id, op_id)
        return self.rental(rental_id)

    def reconcile(self, rental_id: str) -> Rental:
        """Đối soát rental với provider theo trạng thái; không bao giờ gọi lại mutation có kết quả không rõ.

        - ``create_unknown``: tìm theo label (G04).
        - ``destroying``/``destroy_unknown``: chờ instance biến mất khỏi cả get và list mới ``destroyed`` (G09).
        - stop chưa rõ kết quả: xem ``actual_status`` của instance.
        Chuyển trạng thái là CAS theo (status, version): worker và nút Reconcile chạy cùng lúc thì chỉ một bên ghi,
        bên kia trả về bản ghi hiện tại.
        """
        rental = self.rental(rental_id)
        if rental.status == "create_unknown":
            return self._reconcile_create(rental)
        if rental.status in ("destroying", "destroy_unknown"):
            return self._reconcile_destroy(rental)
        stops = self.store.gpu_operations(rental_id, "stop")
        if stops and stops[-1]["status"] == "unknown" and "stopped" in TRANSITIONS[rental.status]:
            return self._reconcile_stop(rental, stops[-1]["op_id"])
        return rental

    def _reconcile_create(self, rental: Rental) -> Rental:
        """Thấy đúng một instance theo label thì gắn, không thấy thì giữ nguyên; không bao giờ gọi create.

        Nhiều instance cùng label: không tự chọn, giữ ``create_unknown`` để người vận hành xử lý (cả hai có thể
        đang tính phí).
        """
        rental_id = rental.rental_id
        try:
            matches = [i for i in self.provider.list_owned_instances() if i.label == rental.label]
        except GPUError as exc:
            self.store.add_gpu_event(rental_id, "reconcile", "lookup_failed", self._clean(str(exc)))
            return rental
        if not matches:
            self.store.add_gpu_event(rental_id, "reconcile", "not_found",
                                     f"chưa thấy instance có label {rental.label}; không tạo lại")
            return rental
        if len(matches) > 1:
            ids = sorted(i.provider_instance_id for i in matches)
            self.store.add_gpu_event(rental_id, "reconcile", "duplicate_label",
                                     f"{len(ids)} instance cùng label {rental.label}: {', '.join(ids)}; "
                                     "không tự gắn, kiểm tra và hủy thủ công")
            return rental
        if not self._move(rental, "provisioning", provider_instance_id=matches[0].provider_instance_id):
            return self.rental(rental_id)          # bên khác đã đổi trạng thái trước
        operations = self.store.gpu_operations(rental_id, "create")
        self._confirm_create(rental_id, operations[-1]["op_id"] if operations else None)
        return self.rental(rental_id)

    def _reconcile_destroy(self, rental: Rental) -> Rental:
        """``destroyed`` chỉ khi cả ``get_instance`` lẫn ``list_owned_instances`` không còn thấy instance."""
        rental_id, instance_id = rental.rental_id, rental.provider_instance_id
        try:
            instance = self.provider.get_instance(instance_id) if instance_id else None
            owned = self.provider.list_owned_instances()
        except GPUError as exc:
            self.store.add_gpu_event(rental_id, "reconcile", "lookup_failed", self._clean(str(exc)))
            return rental
        listed = [i for i in owned if i.provider_instance_id == instance_id]
        if instance is not None or listed:
            status = (instance or listed[0]).actual_status
            self.store.add_gpu_event(rental_id, "reconcile", "still_present",
                                     f"instance {instance_id} vẫn còn ({status}); chưa coi là đã hủy")
            return rental
        if not self._move(rental, "destroyed", destroy_confirmed_at=self.clock().isoformat(timespec="seconds")):
            return self.rental(rental_id)
        operations = self.store.gpu_operations(rental_id, "destroy")
        if operations:
            self.store.finish_gpu_operation(operations[-1]["op_id"], "confirmed")
        others = sorted(i.provider_instance_id for i in owned if i.label == rental.label)
        if others:
            self.store.add_gpu_event(rental_id, "reconcile", "label_still_present",
                                     f"còn instance khác cùng label {rental.label}: {', '.join(others)}")
        return self.rental(rental_id)

    def _reconcile_stop(self, rental: Rental, op_id: str) -> Rental:
        try:
            instance = self.provider.get_instance(rental.provider_instance_id) if rental.provider_instance_id else None
        except GPUError as exc:
            self.store.add_gpu_event(rental.rental_id, "reconcile", "lookup_failed", self._clean(str(exc)))
            return rental
        if instance is None or instance.actual_status not in STOPPED:
            status = instance.actual_status if instance else "không thấy instance"
            self.store.add_gpu_event(rental.rental_id, "reconcile", "stop_unconfirmed",
                                     f"stop chưa xác nhận ({status}); không gửi stop lần nữa")
            return rental
        if self._move(rental, "stopped"):
            self.store.finish_gpu_operation(op_id, "confirmed")
        return self.rental(rental.rental_id)

    # ------------------------------------------------------------ stop, destroy

    def stop(self, rental_id: str) -> Rental:
        """Dừng máy. Instance stopped vẫn tính phí storage; chỉ destroy mới hết phí.

        Không có trạng thái trung gian ``stopping``: ghi operation trước khi gọi; kết quả không rõ thì giữ trạng
        thái cũ, operation ``unknown``, reconcile theo ``actual_status``; không gửi stop lần nữa.
        """
        rental = self.rental(rental_id)
        if not self.supports_stop:
            raise GPUServiceError(f"provider {self.provider_key} không hỗ trợ stop; dùng destroy")
        if "stopped" not in TRANSITIONS[rental.status] or not rental.provider_instance_id:
            raise GPUServiceError(f"không stop được rental ở trạng thái {rental.status}")
        if any(op["status"] == "unknown" for op in self.store.gpu_operations(rental_id, "stop")):
            raise GPUServiceError("lượt stop trước chưa rõ kết quả; reconcile trước, không gửi lại")
        op_id = self._start_operation(rental_id, "stop", rental.provider_instance_id)
        try:
            self.provider.stop_instance(rental.provider_instance_id)
        except GPUUnknown as exc:
            self.store.finish_gpu_operation(op_id, "unknown")
            self.store.add_gpu_event(rental_id, "stop", "stop_timeout", self._clean(str(exc)))
            return self.reconcile(rental_id)
        except GPUError as exc:
            self.store.finish_gpu_operation(op_id, "rejected")
            self.store.add_gpu_event(rental_id, "stop", "stop_rejected", self._clean(str(exc)))
            raise GPUServiceError(f"provider từ chối stop: {self._clean(str(exc))[:300]}") from None
        self.store.finish_gpu_operation(op_id, "confirmed")
        if not self._move(rental, "stopped"):
            self.store.add_gpu_event(rental_id, "stop", "state_changed",
                                     "instance đã stop nhưng rental đổi trạng thái ở nơi khác; reconcile")
        return self.rental(rental_id)

    def destroy(self, rental_id: str) -> Rental:
        """Hủy instance (G09). Ghi operation và chuyển ``destroying`` trước khi gọi provider.

        Provider chỉ nhận yêu cầu; ``destroyed`` chỉ khi reconcile không còn thấy instance. Lỗi hoặc timeout →
        ``destroy_unknown``, giữ record, reconcile. Từ ``destroy_unknown`` chỉ gửi destroy lại khi lượt trước bị
        provider từ chối chắc chắn (``GPUError``, không phải ``GPUUnknown``); kết quả không rõ thì chỉ reconcile.
        """
        rental = self.rental(rental_id)
        if rental.status == "destroy_unknown":
            operations = self.store.gpu_operations(rental_id, "destroy")
            if not operations or operations[-1]["status"] != "rejected":
                raise GPUServiceError("destroy trước chưa rõ kết quả; reconcile, không gửi destroy lần nữa")
        elif "destroying" not in TRANSITIONS[rental.status]:
            raise GPUServiceError(f"không destroy được rental ở trạng thái {rental.status}")
        if not rental.provider_instance_id:
            raise GPUServiceError("rental chưa gắn instance; reconcile trước")
        op_id = self._start_operation(rental_id, "destroy", rental.provider_instance_id)
        if not self._move(rental, "destroying"):
            self.store.finish_gpu_operation(op_id, "rejected")       # chưa gửi gì tới provider
            raise GPUServiceError(f"{rental_id} đã đổi trạng thái ở nơi khác")
        try:
            self.provider.destroy_instance(rental.provider_instance_id)
        except GPUError as exc:
            unknown = isinstance(exc, GPUUnknown)
            self.store.finish_gpu_operation(op_id, "unknown" if unknown else "rejected")
            self._move(self.rental(rental_id), "destroy_unknown",
                       error_code="destroy_timeout" if unknown else "destroy_rejected", detail=str(exc))
            return self.reconcile(rental_id)
        self.store.finish_gpu_operation(op_id, "accepted")
        return self.reconcile(rental_id)

    def _start_operation(self, rental_id: str, action: str, instance_id: str) -> str:
        op_id = uuid.uuid4().hex
        request_hash = hashlib.sha256(json.dumps({"action": action, "instance": instance_id}).encode()).hexdigest()
        self.store.start_gpu_operation(op_id, rental_id, action, request_hash)
        return op_id

    # ------------------------------------------------------------ khởi động lại, orphan (G10)

    def find_orphans(self) -> list[Orphan]:
        """Instance có label ``igniterra:`` không khớp rental nào còn sống. Chỉ báo và ghi log, không tự xóa."""
        rentals = {r.label: r for r in self.rentals()}
        orphans = []
        for instance in self.provider.list_owned_instances():
            rental = rentals.get(instance.label)
            if rental is None:
                reason = "không có rental trong SQLite"
            elif rental.status in ("destroyed", "create_failed"):
                reason = f"rental {rental.rental_id} đã {rental.status} nhưng instance vẫn còn"
            elif rental.provider_instance_id and rental.provider_instance_id != instance.provider_instance_id:
                reason = f"rental {rental.rental_id} gắn instance {rental.provider_instance_id}, không phải instance này"
            else:
                continue
            orphans.append(_orphan(instance, reason))
            log.warning("GPU orphan %s (label %s, %s): %s; không tự xóa", instance.provider_instance_id,
                        instance.label, instance.actual_status, reason)
        self.orphans, self.orphans_checked_at, self.orphans_error = orphans, self.clock(), None
        return orphans

    def recover(self) -> list[Orphan]:
        """Gọi khi khởi động: đối soát rental đang chờ kết quả rồi tìm orphan. Lỗi provider không chặn khởi động."""
        for rental in self.rentals():
            if rental.status in ("create_unknown", "destroying", "destroy_unknown") or any(
                    op["status"] == "unknown" for op in self.store.gpu_operations(rental.rental_id, "stop")):
                try:
                    self.reconcile(rental.rental_id)
                except GPUServiceError as exc:
                    log.warning("reconcile %s khi khởi động: %s", rental.rental_id, exc)
        try:
            return self.find_orphans()
        except GPUError as exc:
            self.orphans_error = self._clean(str(exc))[:300]
            self.orphans_checked_at = self.clock()
            log.warning("không kiểm được orphan GPU: %s", self.orphans_error)
            return []

    # ------------------------------------------------------------ chi phí ước tính

    def cost(self, rental_id: str) -> dict:
        """Chi phí ước tính tới hiện tại (không phải hóa đơn); giả định ghi ở ``studio.gpu.costs``."""
        return costs.summary(self._cost_samples(rental_id), self.clock())

    def _cost_samples(self, rental_id: str) -> list[CostSample]:
        return [CostSample(**{k: row[k] for k in CostSample.model_fields}) for row in self.store.cost_samples(rental_id)]

    def _sample_cost(self, rental_id: str) -> None:
        """Mẫu mới mỗi lần đổi trạng thái: cộng dồn theo tốc độ cũ tới nay, ghi tốc độ theo trạng thái mới."""
        rental = self.rental(rental_id)
        now = self.clock()
        samples = self._cost_samples(rental_id)
        compute, disk = costs.rates(rental)
        self.store.add_cost_sample(rental_id, now.isoformat(timespec="seconds"), compute, disk,
                                   round(costs.accrued(samples[-1] if samples else None, now), 6))

    def _confirm_create(self, rental_id: str, op_id: str | None) -> None:
        if op_id:
            self.store.finish_gpu_operation(op_id, "confirmed")
        self.store.set_provision_step(rental_id, "instance_create", "done")

    def _move(self, rental: Rental, status: str, *, error_code: str | None = None, detail: str | None = None,
              **fields) -> bool:
        """CAS từ bản ghi đã đọc; False khi bên khác đã đổi trước. Chuyển trái TRANSITIONS là lỗi lập trình."""
        if status not in TRANSITIONS[rental.status]:
            raise GPUServiceError(f"chuyển trạng thái không hợp lệ: {rental.status}→{status}")
        moved = self.store.transition_rental(rental.rental_id, rental.version, status, expect=rental.status,
                                             error_code=error_code, detail=self._clean(detail) if detail else None,
                                             **fields)
        if moved:
            self._sample_cost(rental.rental_id)
        return moved

    @staticmethod
    def _clean(text: str) -> str:
        return studio_config.redact(text)[:1000]

    # ------------------------------------------------------------ đọc

    def rental(self, rental_id: str) -> Rental:
        return _rental(self.store.rental(rental_id))

    def rentals(self) -> list[Rental]:
        return [_rental(row) for row in self.store.rentals()]

    def timeline(self, rental_id: str) -> dict:
        return {"rental": self.rental(rental_id),
                "steps": [ProvisionStep(**row) for row in self.store.provision_steps(rental_id)],
                "events": [GPUEvent(**{k: row[k] for k in GPUEvent.model_fields}) for row in self.store.gpu_events(rental_id)],
                "operations": self.store.gpu_operations(rental_id),
                "cost": self.cost(rental_id)}


def _rental(row: dict) -> Rental:
    data = {k: v for k, v in row.items() if k != "offer_json"}
    return Rental(**data, offer=Offer.model_validate_json(row["offer_json"]))


def _orphan(instance: Instance, reason: str) -> Orphan:
    return Orphan(provider_instance_id=instance.provider_instance_id, label=instance.label,
                  actual_status=instance.actual_status, dph_total=instance.dph_total, reason=reason)


def from_config(store: Store, config: dict) -> GPUService | None:
    """Service theo ``[gpu]``; ``provider = "none"`` (mặc định, D-05 chưa mở) trả None."""
    provider = adapters.get(config)
    if provider is None:
        return None
    gpu = config.get("gpu", {})
    return GPUService(store, provider, image=gpu.get("image", ""), quote_ttl_s=float(gpu.get("quote_ttl_s", QUOTE_TTL_S)))
