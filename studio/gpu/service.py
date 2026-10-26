"""Vòng đời thuê GPU (09 · Vòng đời thuê). Web và worker chỉ gọi lớp này; provider chỉ qua ``GPUProvider``.

Cùng nguyên tắc với ``Unconfirmed`` của pipeline (ADR-0006): ghi intent vào SQLite trước mutation; kết quả
không rõ thì đối soát theo label ``igniterra:<rental_id>``, không tự gọi create lần nữa. Khóa CAS theo
``rental_id`` bằng cột ``version``.

Lát A: kiểm offer và báo giá, create, ``create_unknown`` → tìm theo label. Reconcile đầy đủ, stop/destroy,
chi phí ước tính và provisioning thuộc lát B–C.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable

from pydantic import ValidationError

from studio import config as studio_config
from studio.gpu import adapters
from studio.gpu.adapters.base import GPUError, GPUProvider, GPUUnknown
from studio.gpu.contracts import (
    STEP_KEYS,
    TRANSITIONS,
    GPUEvent,
    InstanceSpec,
    Offer,
    OfferFilters,
    ProvisionStep,
    Quote,
    Rental,
    label_for,
)
from studio.store import Store

QUOTE_TTL_S = 300
RENTAL_ID = re.compile(r"r_[0-9a-f]{12}")


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

    @property
    def provider_key(self) -> str:
        return self.provider.key

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
            self.store.add_rental(rental_id, self.provider_key, spec.label, offer.model_dump_json(), STEP_KEYS)
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
        """Đối soát ``create_unknown`` bằng label (G04): thấy đúng một instance thì gắn, không thấy thì giữ nguyên.

        Không bao giờ gọi create ở đây. Nhiều instance cùng label: không tự chọn, giữ ``create_unknown`` để người
        vận hành xử lý (cả hai có thể đang tính phí). Chuyển trạng thái là CAS theo (status, version): worker và
        nút Reconcile chạy cùng lúc thì chỉ một bên gắn instance, bên kia trả về bản ghi hiện tại.
        Đối soát các trạng thái khác (G09, G10) thuộc lát B.
        """
        rental = self.rental(rental_id)
        if rental.status != "create_unknown":
            return rental
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

    def _confirm_create(self, rental_id: str, op_id: str | None) -> None:
        if op_id:
            self.store.finish_gpu_operation(op_id, "confirmed")
        self.store.set_provision_step(rental_id, "instance_create", "done")

    def _move(self, rental: Rental, status: str, *, error_code: str | None = None, detail: str | None = None,
              **fields) -> bool:
        """CAS từ bản ghi đã đọc; False khi bên khác đã đổi trước. Chuyển trái TRANSITIONS là lỗi lập trình."""
        if status not in TRANSITIONS[rental.status]:
            raise GPUServiceError(f"chuyển trạng thái không hợp lệ: {rental.status}→{status}")
        return self.store.transition_rental(rental.rental_id, rental.version, status, expect=rental.status,
                                            error_code=error_code, detail=self._clean(detail) if detail else None,
                                            **fields)

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
                "operations": self.store.gpu_operations(rental_id)}


def _rental(row: dict) -> Rental:
    data = {k: v for k, v in row.items() if k != "offer_json"}
    return Rental(**data, offer=Offer.model_validate_json(row["offer_json"]))


def from_config(store: Store, config: dict) -> GPUService | None:
    """Service theo ``[gpu]``; ``provider = "none"`` (mặc định, D-05 chưa mở) trả None."""
    provider = adapters.get(config)
    if provider is None:
        return None
    gpu = config.get("gpu", {})
    return GPUService(store, provider, image=gpu.get("image", ""), quote_ttl_s=float(gpu.get("quote_ttl_s", QUOTE_TTL_S)))
