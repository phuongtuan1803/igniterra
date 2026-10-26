"""Vòng đời thuê GPU (09 · Vòng đời thuê). Web và worker chỉ gọi lớp này; provider chỉ qua ``GPUProvider``.

Cùng nguyên tắc với ``Unconfirmed`` của pipeline (ADR-0006): ghi intent vào SQLite trước mutation; kết quả
không rõ thì đối soát theo label ``igniterra:<rental_id>``, không tự gọi create lần nữa. Khóa CAS theo
``rental_id`` bằng cột ``version``.

Lát A: kiểm offer và báo giá, create, ``create_unknown`` → tìm theo label. Lát B: stop, destroy và
``destroy_unknown`` (G09), đối soát khi khởi động lại và báo orphan (G10), chi phí ước tính. Lát C: manifest
provisioning đã duyệt (``gpu_manifests``), ``WEB_PASSWORD`` riêng mỗi rental, theo dõi các bước provisioning tới
``ready`` có bằng chứng (host chạy, kết nối có xác thực, ``/.provisioning_complete``, ``/system_stats``, smoke ra
file), ``provision_failed`` rồi destroy theo ``on_failure`` (G05, nửa disk/dependency của G07). Lát D: runtime
ComfyUI của rental ``ready`` cho ``studio.comfyui.service`` (``comfy_runtime``), ``ready⇄busy`` một run mỗi instance,
và destroy cảnh báo khi còn run có output chưa tải/kiểm (G11). Lát E: guardrail idle TTL, ``max_runtime_minutes``,
``max_hourly_usd`` do worker thực thi (``enforce_limits``, ``tick``, ``start_guard``); SHA-256 model kiểm qua kênh
ComfyUI có xác thực thay cho SSH (lý do ở docstring của ``studio.gpu.provisioning``).

Guardrail không bao giờ vượt G11: destroy tự động gọi ``_destroy`` không có ``accept_unverified_output``. Rental vượt
giới hạn mà còn run có output chưa tải/kiểm thì KHÔNG destroy; ghi sự kiện ``limit_blocked``, log ERROR mỗi lượt, và
trang ``/gpu`` hiện nó ở mục "vượt giới hạn" (``over_limits``) để người vận hành tải output hoặc xác nhận bỏ rồi
destroy. Máy vẫn tính phí trong lúc đó: đó là đánh đổi có chủ ý, mất output đã trả tiền cũng là mất tiền và không
lấy lại được, còn phí chạy thêm thì người vận hành dừng được ngay khi thấy cảnh báo.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from studio import config as studio_config
from studio.comfyui import runtime as comfy
from studio.comfyui.contracts import RUN_EXECUTING, RUN_UNRESOLVED
from studio.gpu import adapters, costs, limits, provisioning
from studio.gpu.adapters.base import GPUError, GPUProvider, GPUUnknown
from studio.gpu.contracts import (
    STEP_KEYS,
    TRANSITIONS,
    CostSample,
    GPUEvent,
    Instance,
    InstanceSpec,
    ManifestRecord,
    Offer,
    OfferFilters,
    Orphan,
    ProvisionStep,
    Quote,
    Rental,
    label_for,
)
from studio.gpu.limits import Breach, Enforcement, Limits
from studio.gpu.probe import InstanceProbe, PortalProbe, ProbeUnavailable
from studio.gpu.provisioning import NON_RETRYABLE, PROVISION_STEPS, ProvisioningManifest
from studio.store import Store

QUOTE_TTL_S = 300
PROVISION_TIMEOUT_S = 3600             # quá hạn mà chưa ready → provision_failed, destroy
GUARD_INTERVAL_S = 60                  # chu kỳ guardrail trong worker (poll + enforce_limits + reconcile destroy)
PASSWORD_FILE = "web_password"
MANIFEST_FILE = "provisioning.yaml"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
HOST_DOWN = ("exited", "stopped", "offline", "error", "destroying", "destroyed")
RENTAL_ID = re.compile(r"r_[0-9a-f]{12}")
STOPPED = ("stopped", "exited")        # actual_status coi như đã dừng máy
log = logging.getLogger("studio.gpu")


class GPUServiceError(Exception):
    """Từ chối trước khi gọi mutation của provider (offer không đạt, báo giá hết hạn, rental_id trùng…)."""


class UnverifiedOutput(GPUServiceError):
    """Destroy bị giữ lại: rental còn run có output chưa tải và kiểm (G11). Gọi lại với ``accept_unverified_output``
    sau khi người vận hành xác nhận bỏ output đó."""

    def __init__(self, rental_id: str, runs: list[dict]):
        self.rental_id, self.runs = rental_id, runs
        listed = ", ".join(f"{r['run_id']} ({r['status']})" for r in runs)
        super().__init__(f"{rental_id} còn {len(runs)} run có output chưa tải/kiểm: {listed}. Destroy sẽ mất output "
                         "trên máy; tải output trước, hoặc xác nhận bỏ output rồi destroy")


class RentalUnreachable(GPUServiceError):
    """Chưa tới được runtime ComfyUI của rental (chưa có cổng, kết nối lỗi). GET thử lại ở lượt sau."""


class _Wait(Exception):
    """Bước provisioning chưa xong; thử lại ở lượt poll sau (GET an toàn)."""


class _Fail(Exception):
    """Bước provisioning hỏng hẳn: ``provision_failed`` rồi destroy theo ``on_failure``."""

    def __init__(self, step: str, code: str, detail: str):
        super().__init__(f"{step}: {code}")
        self.step, self.code, self.detail = step, code, detail


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
                 clock: Callable[[], datetime] = utc_now, data_dir: Path | None = None,
                 probe_factory: Callable[[Instance, str], InstanceProbe] | None = None, portal: dict | None = None,
                 provision_timeout_s: float = PROVISION_TIMEOUT_S, limits: Limits | None = None):
        self.store = store
        self.provider = provider
        self.image = image
        self.quote_ttl_s = quote_ttl_s
        self.clock = clock
        self.limits = limits or Limits()
        # data/gpu/<rental_id>/: provisioning.yaml và web_password (quyền 600, không vào SQLite/HTML).
        self.data_dir = Path(data_dir) if data_dir else Path(store.path).parent / "gpu"
        self.probe_factory = probe_factory or (lambda instance, token: PortalProbe.for_instance(instance, token, portal))
        self.provision_timeout_s = provision_timeout_s
        self._secrets: set[str] = {p.read_text(encoding="utf-8").strip()
                                   for p in self.data_dir.glob(f"r_*/{PASSWORD_FILE}") if p.is_file()}
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

    def rent(self, rental_id: str, quote: Quote, filters: OfferFilters, manifest_sha256: str | None = None) -> Rental:
        """Thuê một offer đã báo giá. Mọi kiểm tra chạy trước khi ghi intent và gọi ``create_instance``.

        Manifest provisioning phải đã duyệt (mặc định: bản duyệt gần nhất) và vừa disk yêu cầu (G07, chặn trước
        khi trả tiền). Lúc thuê sinh ``WEB_PASSWORD`` ngẫu nhiên, ghi ``data/gpu/<rental_id>/`` quyền 600.
        """
        if not RENTAL_ID.fullmatch(rental_id):
            raise GPUServiceError(f"rental_id lạ: {rental_id!r}")
        problems = self.check_quote(quote)
        if problems:
            raise GPUServiceError("; ".join(problems))
        offer = next((o for o in self.provider.search_offers(filters) if o.offer_id == quote.offer_id), None)
        if offer is None:
            raise GPUServiceError(f"offer {quote.offer_id} không còn hoặc không khớp yêu cầu")
        problems = check_offer(offer, filters) + self.check_quote(quote, offer)
        hourly = costs.hourly(offer, filters.disk_gb)
        if self.limits.max_hourly_usd is not None and hourly > self.limits.max_hourly_usd:
            problems.append(f"giá ước tính ${hourly:.3f}/giờ (compute + disk) > max_hourly_usd "
                            f"${self.limits.max_hourly_usd:.3f}")
        if problems:
            raise GPUServiceError(f"offer {offer.offer_id} bị loại: " + "; ".join(problems))
        record = self.manifest(manifest_sha256) if manifest_sha256 else self.approved_manifest()
        if record is None or record.status != "approved":
            raise GPUServiceError("chưa có manifest provisioning đã duyệt; thêm và duyệt manifest trước khi thuê")
        manifest = self._load_manifest(record)
        if manifest.required_disk_gb() > filters.disk_gb:
            raise GPUServiceError(f"manifest cần ~{manifest.required_disk_gb():g} GB disk > {filters.disk_gb:g} GB "
                                  "yêu cầu; tăng disk trước khi thuê")
        yaml_text = provisioning.render(manifest, record.manifest_sha256)
        password = secrets.token_urlsafe(24)
        try:
            spec = InstanceSpec(label=label_for(rental_id), image=self.image, disk_gb=filters.disk_gb,
                                onstart=_onstart(yaml_text, provisioning.checksum_script(manifest)),
                                env={"WEB_PASSWORD": password, "ENABLE_AUTH": "true", "ENABLE_HTTPS": "true",
                                     "PORTAL_CONFIG": provisioning.PORTAL_CONFIG,
                                     "PROVISIONING_MANIFEST": provisioning.REMOTE_MANIFEST})
        except ValidationError as exc:
            raise GPUServiceError(f"cấu hình [gpu] image: {exc.errors()[0]['msg']}") from None
        try:
            self.store.add_rental(rental_id, self.provider_key, spec.label, offer.model_dump_json(), STEP_KEYS,
                                  disk_gb=spec.disk_gb, manifest_sha256=record.manifest_sha256)
        except sqlite3.IntegrityError:
            raise GPUServiceError(f"{rental_id} đã được dùng; không tạo lại (bấm hai lần?)") from None
        self._write_private(rental_id, MANIFEST_FILE, yaml_text)
        self._write_private(rental_id, PASSWORD_FILE, password)
        self._secrets.add(password)

        op_id = uuid.uuid4().hex
        # env có WEB_PASSWORD: chỉ băm tên biến, không băm giá trị bí mật.
        request_hash = hashlib.sha256(json.dumps({"offer_id": offer.offer_id, "spec": spec.model_dump(exclude={"env"}),
                                                  "env_keys": sorted(spec.env)}, sort_keys=True).encode()).hexdigest()
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
        if not self._move(rental, "provisioning", provider_instance_id=instance.provider_instance_id,
                          provision_deadline=self._provision_deadline()):
            raise GPUServiceError(f"{rental_id} đã đổi trạng thái ở nơi khác")
        self._confirm_create(rental_id, op_id)
        return self.rental(rental_id)

    def reconcile(self, rental_id: str) -> Rental:
        """Đối soát rental với provider theo trạng thái; không bao giờ gọi lại mutation có kết quả không rõ.

        - ``create_unknown``: tìm theo label (G04).
        - ``destroying``/``destroy_unknown``: chờ instance biến mất khỏi cả get và list mới ``destroyed`` (G09).
        - stop chưa rõ kết quả: xem ``actual_status`` của instance.
        - ``provisioning``: kiểm tiếp các bước provisioning (``check_provisioning``).
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
        if rental.status == "provisioning":
            return self.check_provisioning(rental_id)
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
        if not self._move(rental, "provisioning", provider_instance_id=matches[0].provider_instance_id,
                          provision_deadline=self._provision_deadline()):
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
        for op in self.store.gpu_operations(rental_id, "destroy"):
            if op["status"] in ("sent", "accepted", "unknown"):        # mọi lượt chưa rõ đã có kết quả: instance mất
                self.store.finish_gpu_operation(op["op_id"], "confirmed")
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

    def destroy(self, rental_id: str, *, accept_unverified_output: bool = False) -> Rental:
        """Hủy instance (G09). Ghi operation và chuyển ``destroying`` trước khi gọi provider.

        Provider chỉ nhận yêu cầu; ``destroyed`` chỉ khi reconcile không còn thấy instance. Lỗi hoặc timeout →
        ``destroy_unknown``, giữ record, reconcile. Từ ``destroy_unknown`` chỉ gửi destroy lại khi lượt trước bị
        provider từ chối chắc chắn (``GPUError``, không phải ``GPUUnknown``); kết quả không rõ thì chỉ reconcile.

        G11: rental còn run ComfyUI có output chưa tải và kiểm (``RUN_UNRESOLVED``) thì từ chối bằng
        ``UnverifiedOutput`` trước khi ghi gì; chỉ đi tiếp khi người vận hành xác nhận bỏ output
        (``accept_unverified_output``), lúc đó các run đó thành ``abandoned``. Không đường tự động nào (on_failure,
        idle TTL ở lát E) truyền cờ này.

        Lượt kiểm đầu hàm chỉ là đường tắt để không ghi operation vô ích. Chốt thật nằm ở ``Store.begin_destroy``:
        điều kiện "không còn run chưa kiểm" nằm trong cùng câu UPDATE với CAS (status, version) ``→destroying``.
        CAS (status, version) một mình không đủ: ``busy→destroying`` hợp lệ, nên run giành máy ``ready→busy`` sau
        lượt kiểm đầu mà trước lượt đọc rental vẫn qua được CAS. Có xác nhận thì các run bị bỏ là đúng tập có lúc CAS,
        trong cùng giao dịch.
        """
        return self._destroy(rental_id, accept_unverified_output=accept_unverified_output)

    def _destroy(self, rental_id: str, *, accept_unverified_output: bool = False, idle_before: str | None = None,
                 reason: str | None = None) -> Rental:
        """Thân của ``destroy``. ``idle_before``/``reason`` chỉ guardrail dùng: điều kiện hạn idle đã qua nằm trong câu
        CAS, và lý do ghi vào sự kiện ``→destroying``. Guardrail không bao giờ truyền ``accept_unverified_output``."""
        unresolved = self.store.comfy_runs(rental_id=rental_id, statuses=RUN_UNRESOLVED)
        if unresolved and not accept_unverified_output:
            raise UnverifiedOutput(rental_id, unresolved)
        rental = self.rental(rental_id)
        if reason is not None:
            if rental.provider_key != self.provider_key or rental.label != label_for(rental_id):
                raise GPUServiceError("guardrail: provider hoặc label của rental không khớp; cần người vận hành")
            if idle_before is not None and rental.status != "ready":
                raise GPUServiceError("guardrail: rental không còn idle")
            if rental.provider_instance_id:
                instance = self.provider.get_instance(rental.provider_instance_id)
                if instance is not None and instance.label != rental.label:
                    raise GPUServiceError("guardrail: label instance không khớp; không tự destroy")
        if rental.status == "destroy_unknown":
            operations = self.store.gpu_operations(rental_id, "destroy")
            # Không dựa vào thứ tự: còn bất kỳ lượt nào chưa rõ (unknown, accepted, sent) thì chỉ reconcile.
            if not operations or any(op["status"] != "rejected" for op in operations):
                raise GPUServiceError("destroy trước chưa rõ kết quả; reconcile, không gửi destroy lần nữa")
        elif "destroying" not in TRANSITIONS[rental.status]:
            raise GPUServiceError(f"không destroy được rental ở trạng thái {rental.status}")
        if not rental.provider_instance_id:
            raise GPUServiceError("rental chưa gắn instance; reconcile trước")
        op_id = self._start_operation(rental_id, "destroy", rental.provider_instance_id)
        detail = "người vận hành xác nhận destroy, bỏ output chưa tải/kiểm" if accept_unverified_output else None
        if self.store.begin_destroy(rental_id, rental.version, expect=rental.status, unresolved=RUN_UNRESOLVED,
                                    abandon_detail=detail, idle_before=idle_before,
                                    detail=self._clean(reason) if reason else None) is None:
            self.store.finish_gpu_operation(op_id, "rejected")       # chưa gửi gì tới provider
            if not accept_unverified_output:
                # CAS thua vì run mới chen vào (hoặc cùng lúc với đổi trạng thái): báo đúng run đang có, không danh
                # sách cũ. Gọi lại kèm xác nhận mới destroy được, nên lý do này đứng trước.
                current = self.store.comfy_runs(rental_id=rental_id, statuses=RUN_UNRESOLVED)
                if current:
                    raise UnverifiedOutput(rental_id, current)
            raise GPUServiceError(f"{rental_id} đã đổi trạng thái ở nơi khác")
        self._sample_cost(rental_id)
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
            if rental.status in ("create_unknown", "provisioning", "destroying", "destroy_unknown") or any(
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

    # ------------------------------------------------------------ manifest provisioning (lát C)

    def propose_manifest(self, manifest: ProvisioningManifest, note: str | None = None) -> ManifestRecord:
        """Lưu manifest ``pending_review`` sau khi kiểm (repo GitHub và HF ghim commit, ``on_failure: destroy``)."""
        problems = provisioning.validate(manifest)
        if problems:
            raise GPUServiceError("manifest không đạt: " + "; ".join(problems))
        sha = manifest.sha256()
        self.store.add_manifest(sha, manifest.canonical_json(), note)
        return self.manifest(sha)

    def approve_manifest(self, manifest_sha256: str) -> ManifestRecord:
        """Duyệt manifest đã đọc; kiểm lại nội dung và SHA-256 trước khi đổi trạng thái."""
        record = self.manifest(manifest_sha256)
        self._load_manifest(record)
        if record.status == "approved":
            return record
        if not self.store.set_manifest_status(manifest_sha256, "approved", expect="pending_review"):
            raise GPUServiceError(f"manifest {manifest_sha256[:12]} đang {record.status}; không duyệt được")
        return self.manifest(manifest_sha256)

    def manifest(self, manifest_sha256: str) -> ManifestRecord:
        return ManifestRecord(**self.store.manifest(manifest_sha256))

    def manifests(self) -> list[ManifestRecord]:
        return [ManifestRecord(**row) for row in self.store.manifests()]

    def approved_manifest(self) -> ManifestRecord | None:
        return next((m for m in self.manifests() if m.status == "approved"), None)

    def manifest_view(self, record: ManifestRecord) -> ProvisioningManifest:
        """Nội dung manifest để hiện trên trang (không có bí mật)."""
        return ProvisioningManifest.model_validate_json(record.manifest_json)

    def manifest_yaml(self, manifest_sha256: str) -> str:
        record = self.manifest(manifest_sha256)
        return provisioning.render(self._load_manifest(record), record.manifest_sha256)

    def _load_manifest(self, record: ManifestRecord) -> ProvisioningManifest:
        """Manifest từ SQLite; SHA-256 phải khớp nội dung (không ai sửa sau khi duyệt) và vẫn đạt kiểm tra."""
        try:
            manifest = ProvisioningManifest.model_validate_json(record.manifest_json)
        except ValidationError as exc:
            raise GPUServiceError(f"manifest {record.manifest_sha256[:12]} hỏng: {exc.errors()[0]['msg']}") from None
        if manifest.sha256() != record.manifest_sha256:
            raise GPUServiceError(f"manifest {record.manifest_sha256[:12]} không khớp SHA-256 đã lưu")
        problems = provisioning.validate(manifest)
        if problems:
            raise GPUServiceError("manifest không đạt: " + "; ".join(problems))
        return manifest

    # ------------------------------------------------------------ bí mật và file cục bộ của rental

    def rental_dir(self, rental_id: str) -> Path:
        if not RENTAL_ID.fullmatch(rental_id):
            raise GPUServiceError(f"rental_id lạ: {rental_id!r}")
        return self.data_dir / rental_id

    def _write_private(self, rental_id: str, name: str, text: str) -> Path:
        """Ghi file quyền 600 trong thư mục 700; mở bằng ``O_CREAT`` với mode ngay từ đầu, không chmod sau."""
        folder = self.rental_dir(rental_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder, 0o700)
        path = folder / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(path, 0o600)
        return path

    def _password(self, rental_id: str) -> str | None:
        path = self.rental_dir(rental_id) / PASSWORD_FILE
        if not path.is_file():
            return None
        password = path.read_text(encoding="utf-8").strip()
        self._secrets.add(password)
        return password or None

    def _provision_deadline(self) -> str:
        return (self.clock() + timedelta(seconds=self.provision_timeout_s)).isoformat(timespec="seconds")

    def _idle_deadline(self) -> str | None:
        if self.limits.idle_ttl_minutes is None:
            return None
        return (self.clock() + timedelta(minutes=self.limits.idle_ttl_minutes)).isoformat(timespec="seconds")

    # ------------------------------------------------------------ provisioning → ready (lát C)

    def poll(self) -> list[Rental]:
        """Kiểm mọi rental đang ``provisioning`` (worker hoặc CLI gọi định kỳ). Chỉ GET; mutation duy nhất là
        submit smoke (ghi ``prompt_id`` trước) và destroy khi provisioning hỏng."""
        results = []
        for rental in self.rentals():
            if rental.status != "provisioning":
                continue
            try:                           # một rental lỗi không làm bỏ qua các rental khác trong lượt
                results.append(self.check_provisioning(rental.rental_id))
            except (GPUServiceError, GPUError) as exc:
                log.warning("poll %s: %s", rental.rental_id, self._clean(str(exc)))
                self.store.add_gpu_event(rental.rental_id, "provisioning", "poll_error", self._clean(str(exc)))
                results.append(self.rental(rental.rental_id))
        return results

    def check_provisioning(self, rental_id: str) -> Rental:
        """Đưa rental ``provisioning`` qua từng bước tới ``ready`` khi đủ bằng chứng (09 · s4.b2).

        Mỗi lượt gọi chỉ đi tiếp tới bước chưa xong rồi trả về; gọi lại an toàn. Bước hỏng hẳn (hết
        ``max_retries`` của manifest, lỗi không thử lại được như thiếu disk hay xung đột dependency, quá hạn)
        → ``provision_failed`` và destroy theo ``on_failure`` (G05, G07).
        """
        rental = self.rental(rental_id)
        if rental.status != "provisioning":
            return rental
        if not rental.manifest_sha256:
            self.store.add_gpu_event(rental_id, "provisioning", "no_manifest",
                                     "rental tạo trước lát C, không có manifest; không kiểm được provisioning")
            return rental
        try:
            try:
                manifest = self._load_manifest(self.manifest(rental.manifest_sha256))
            except (KeyError, GPUServiceError) as exc:
                raise _Fail("host_boot", "manifest_invalid", str(exc)) from None
            self._advance(rental, manifest)
        except _Wait:
            return self.rental(rental_id)
        except _Fail as failure:
            return self._fail_provisioning(rental, failure)
        return self.rental(rental_id)

    def _advance(self, rental: Rental, manifest: ProvisioningManifest) -> None:
        rental_id = rental.rental_id
        steps = {row["step_key"]: row for row in self.store.provision_steps(rental_id)}
        if rental.provision_deadline and self.clock() > _aware(rental.provision_deadline):
            current = next((k for k in STEP_KEYS if steps[k]["status"] not in ("done", "skipped")), "ready")
            raise _Fail(current, "provision_timeout",
                        f"quá {self.provision_timeout_s:g}s chưa ready (bước {current})")

        # host_boot: provider báo máy chạy. Host running chưa phải ready.
        self._start(steps, rental_id, "host_boot")
        try:
            instance = self.provider.get_instance(rental.provider_instance_id) if rental.provider_instance_id else None
        except GPUError as exc:
            self.store.add_gpu_event(rental_id, "provisioning", "lookup_failed", self._clean(str(exc)))
            raise _Wait from None
        if instance is None:
            raise _Fail("host_boot", "instance_missing", f"provider không còn instance {rental.provider_instance_id}")
        if instance.actual_status != "running":
            if instance.actual_status in HOST_DOWN:
                raise _Fail("host_boot", "host_not_running", f"instance ở trạng thái {instance.actual_status}")
            raise _Wait
        self._finish(steps, rental_id, "host_boot")

        # transport_ready: portal và cổng ComfyUI từ chối request không token, nhận Bearer WEB_PASSWORD.
        self._start(steps, rental_id, "transport_ready")
        password = self._password(rental_id)
        if not password:
            raise _Fail("transport_ready", "secret_missing", f"thiếu {PASSWORD_FILE} của rental")
        try:
            probe = self.probe_factory(instance, password)
            check = probe.transport()
        except ProbeUnavailable:
            raise _Wait from None
        if not check.portal_auth_required or not check.comfyui_auth_required:
            where = "Instance Portal" if not check.portal_auth_required else "cổng ComfyUI"
            raise _Fail("transport_ready", "auth_open", f"{where} trả lời request không có token")
        if not check.token_accepted:
            raise _Fail("transport_ready", "auth_rejected", "Instance Portal không nhận WEB_PASSWORD của rental")
        self._finish(steps, rental_id, "transport_ready")

        # runtime_setup, custom_nodes, models: theo báo cáo của provisioner.
        try:
            report = probe.report()
        except ProbeUnavailable:
            raise _Wait from None
        for key in PROVISION_STEPS:
            self._sync_step(steps, rental_id, key, manifest, report)
        if not report.complete:
            raise _Wait                    # chưa có /.provisioning_complete

        # models: tải xong chưa đủ; SHA-256 từng file phải khớp manifest (không SSH, xem studio.gpu.provisioning).
        if steps["models"]["status"] not in ("done", "skipped"):
            self._verify_models(steps, rental_id, manifest, report, probe.comfy)

        # health_smoke: /system_stats rồi một workflow smoke ra file.
        self._start(steps, rental_id, "health_smoke")
        try:
            stats = probe.comfy.system_stats()
        except comfy.ComfyUIAuthError as exc:
            raise _Fail("health_smoke", "auth_rejected", str(exc)) from None
        except comfy.ComfyUIError:
            raise _Wait from None
        smoke = self._smoke(rental_id, probe.comfy, manifest.on_failure.max_retries)
        self._finish(steps, rental_id, "health_smoke")

        system = stats.get("system") or {}
        evidence = {"host": instance.actual_status, "auth": "không token → từ chối; Bearer WEB_PASSWORD → 200",
                    "provisioning_complete": True, "comfyui_version": system.get("comfyui_version"),
                    "devices": [d.get("name") for d in stats.get("devices") or [] if isinstance(d, dict)],
                    "smoke": smoke, "manifest_sha256": manifest.sha256()}
        self.store.add_gpu_event(rental_id, "ready_evidence", None, self._clean(json.dumps(evidence, ensure_ascii=False)))
        self._finish(steps, rental_id, "ready")
        if not self._move(self.rental(rental_id), "ready"):
            raise _Wait

    def _sync_step(self, steps: dict, rental_id: str, key: str, manifest: ProvisioningManifest,
                   report: provisioning.ProvisioningReport) -> None:
        """Chép trạng thái một bước provisioner vào ``gpu_provision_steps``; ``_Wait`` nếu chưa xong."""
        step = report.steps.get(key)
        empty = (key == "custom_nodes" and not manifest.custom_nodes) or (key == "models" and not manifest.downloads)
        if empty:                          # manifest không có gì cho bước này
            if steps[key]["status"] != "skipped":
                self.store.set_provision_step(rental_id, key, "skipped")
                steps[key] = {**steps[key], "status": "skipped"}
            return
        if step is None:
            if report.complete:
                if key == "models":        # provisioner không báo bước models: vẫn phải kiểm SHA-256 sau đó
                    if steps[key]["status"] not in ("running", "done"):
                        self._running(steps, rental_id, key, {})
                else:
                    self._finish(steps, rental_id, key)
                return
            raise _Wait
        fields = {"attempt": max(step.attempt, 1)}
        fields.update({k: v for k, v in (("bytes_done", step.bytes_done), ("bytes_total", step.bytes_total),
                                          ("log_ref", report.log_ref)) if v is not None})
        if step.status == "failed":
            code = provisioning.classify(key, step)
            detail = self._clean(step.error or code)
            if code in NON_RETRYABLE or step.final or step.attempt >= 1 + manifest.on_failure.max_retries:
                self.store.update_provision_step(rental_id, key, **fields)
                raise _Fail(key, code, detail)
            if steps[key]["attempt"] != step.attempt:        # provisioner sẽ thử lại; ghi một lần mỗi lượt
                self.store.add_gpu_event(rental_id, f"{key}_retry", code,
                                         f"lượt {step.attempt}/{1 + manifest.on_failure.max_retries}: {detail}")
            self._running(steps, rental_id, key, fields)
            raise _Wait
        if key == "models" and step.status != "done" and report.disk_free_gb is not None:
            remaining = manifest.download_bytes() - (step.bytes_done or 0)
            if report.disk_free_gb * provisioning.GB < remaining:
                raise _Fail(key, "disk_insufficient", f"còn {report.disk_free_gb:g} GB trống, model còn cần "
                                                      f"{remaining / provisioning.GB:.2f} GB")
        if step.status == "pending":
            raise _Wait
        if step.status == "running":
            self._running(steps, rental_id, key, fields)
            raise _Wait
        if key == "models":
            # Provisioner tải xong; bước chỉ ``done`` khi SHA-256 đã kiểm (``_verify_models``, sau provisioning_complete).
            if steps[key]["status"] != "done":
                self._running(steps, rental_id, key, fields)
            return
        self._finish(steps, rental_id, key, **fields)

    def _verify_models(self, steps: dict, rental_id: str, manifest: ProvisioningManifest,
                       report: provisioning.ProvisioningReport, runtime: comfy.ComfyUIRuntime) -> None:
        """SHA-256 từng model phải khớp manifest (giá trị LFS của Hugging Face) trước khi bước ``models`` xong.

        Nguồn: ``report.files`` nếu portal có trả, không thì file ``sha256sum`` do ``checksum_script`` ghi, đọc qua
        ``GET /view`` của cổng ComfyUI có xác thực. Lệch → ``_Fail``; thiếu → ``_Wait`` (script có thể đang băm file
        lớn), ghi ``checksum_pending`` một lần; quá ``provision_timeout_s`` thì ``provision_timeout`` như mọi bước khác.
        Không bao giờ coi model chưa kiểm là đạt.
        """
        def lookup(files: dict[str, str], dest: str) -> str | None:
            got = files.get(f"{provisioning.COMFYUI_DIR}/models/{dest}") or files.get(dest)
            return got.lower() if got else None

        hashes = {d.dest: lookup(report.files, d.dest) for d in manifest.downloads}
        if any(v is None for v in hashes.values()):
            try:
                data = runtime.view(provisioning.CHECKSUM_FILE, provisioning.CHECKSUM_SUBFOLDER, "output",
                                    max_bytes=provisioning.CHECKSUM_MAX_BYTES)
            except comfy.ComfyUIAuthError as exc:
                raise _Fail("models", "auth_rejected", str(exc)) from None
            except comfy.ComfyUIRejected:
                data = b""                 # 404: script chưa ghi xong
            except comfy.ComfyUIError:
                raise _Wait from None
            reported = provisioning.parse_sha256sum(data.decode("utf-8", "replace"))
            hashes = {dest: got or lookup(reported, dest) for dest, got in hashes.items()}
        for download in manifest.downloads:
            got = hashes[download.dest]
            if got is not None and got != download.sha256:
                raise _Fail("models", "checksum_mismatch", f"{download.dest}: SHA-256 {got[:12]}… ≠ {download.sha256[:12]}…")
        missing = sorted(dest for dest, got in hashes.items() if got is None)
        if missing:
            self._note_once(rental_id, "models", "checksum_pending",
                            "chưa có SHA-256 từ instance cho: " + ", ".join(missing) + "; chưa ready")
            raise _Wait
        self.store.add_gpu_event(rental_id, "models", None,
                                 f"SHA-256 khớp manifest: {len(hashes)} model ({', '.join(sorted(hashes))})")
        self._finish(steps, rental_id, "models")

    def _smoke(self, rental_id: str, runtime: comfy.ComfyUIRuntime, max_retries: int) -> dict:
        """Workflow smoke ``EmptyImage → SaveImage`` với ``prompt_id`` ghi vào ``gpu_operations`` trước khi submit.

        Không submit lại khi kết quả chưa rõ: tra ``/queue`` rồi ``/history/{prompt_id}``; chỉ khi không thấy ở cả
        hai mới coi lượt đó mất và submit lượt mới (tối đa ``1 + max_retries``).
        """
        operations = [op for op in self.store.gpu_operations(rental_id, "smoke") if op["status"] != "not_sent"]
        current = operations[-1] if operations and operations[-1]["status"] != "lost" else None
        if current is None:
            if len(operations) >= 1 + max_retries:
                raise _Fail("health_smoke", "smoke_lost", f"{len(operations)} lượt smoke không có kết quả")
            prompt_id = str(uuid.uuid4())
            request_hash = hashlib.sha256(json.dumps(comfy.smoke_prompt(), sort_keys=True).encode()).hexdigest()
            self.store.start_gpu_operation(prompt_id, rental_id, "smoke", request_hash)
            try:
                runtime.submit(comfy.smoke_prompt(), prompt_id)
            except comfy.ComfyUIUnknown as exc:
                self.store.finish_gpu_operation(prompt_id, "unknown")
                self.store.add_gpu_event(rental_id, "smoke", "smoke_submit_unknown", self._clean(str(exc)))
            except comfy.ComfyUIUnavailable:
                self.store.finish_gpu_operation(prompt_id, "not_sent")
                raise _Wait from None
            except comfy.ComfyUIError as exc:
                self.store.finish_gpu_operation(prompt_id, "rejected")
                raise _Fail("health_smoke", "smoke_rejected", str(exc)) from None
            else:
                self.store.finish_gpu_operation(prompt_id, "accepted")
            current = {"op_id": prompt_id, "status": self.store.gpu_operations(rental_id, "smoke")[-1]["status"]}
        prompt_id = current["op_id"]
        try:
            queued = runtime.in_queue(prompt_id)        # hỏi queue trước history: không lọt lượt vừa chạy xong
            entry = runtime.history(prompt_id)
        except comfy.ComfyUIError:
            raise _Wait from None
        if entry is None:
            if queued:
                if current["status"] == "unknown":
                    self.store.finish_gpu_operation(prompt_id, "accepted")
                raise _Wait
            self.store.finish_gpu_operation(prompt_id, "lost")
            self.store.add_gpu_event(rental_id, "smoke", "smoke_lost",
                                     f"prompt {prompt_id} không có trong /queue và /history")
            raise _Wait
        status = comfy.history_status(entry)
        if status == "running":
            raise _Wait
        if status == "error":
            self.store.finish_gpu_operation(prompt_id, "failed")
            messages = json.dumps((entry.get("status") or {}).get("messages") or [], ensure_ascii=False)[:500]
            raise _Fail("health_smoke", "smoke_failed", f"prompt {prompt_id}: {messages}")
        files = comfy.output_files(entry)
        if not files:
            raise _Fail("health_smoke", "smoke_no_output", f"prompt {prompt_id} xong nhưng không có file output")
        output = files[0]
        try:
            data = runtime.view(output["filename"], output["subfolder"], output["type"])
        except comfy.ComfyUIRejected as exc:
            raise _Fail("health_smoke", "smoke_no_output", str(exc)) from None
        except comfy.ComfyUIError:
            raise _Wait from None
        if not data.startswith(PNG_MAGIC):
            raise _Fail("health_smoke", "smoke_no_output", f"{output['filename']} không phải PNG ({len(data)} byte)")
        if current["status"] != "confirmed":
            self.store.finish_gpu_operation(prompt_id, "confirmed")
        return {"prompt_id": prompt_id, "file": output["filename"], "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    def _fail_provisioning(self, rental: Rental, failure: _Fail) -> Rental:
        """Bước hỏng → ``provision_failed`` (ghi nguyên nhân), các bước sau ``skipped``, rồi destroy theo manifest.

        Mọi manifest đã duyệt đều có ``on_failure.action = destroy``: instance hỏng không được để chạy tính phí.
        """
        rental_id = rental.rental_id
        current = self.rental(rental_id)
        if current.status != "provisioning":
            # Bên khác (nút Destroy, poll/reconcile song song) đã đưa rental đi tiếp: không ghi đè, trả bản hiện tại.
            self.store.add_gpu_event(rental_id, "provisioning", "state_changed",
                                     f"phát hiện {failure.code} ở {failure.step} nhưng rental đã {current.status}")
            return current
        detail = self._clean(failure.detail)
        self.store.set_provision_step(rental_id, failure.step, "failed", failure.code)
        for key in STEP_KEYS[STEP_KEYS.index(failure.step) + 1:]:
            row = next(r for r in self.store.provision_steps(rental_id) if r["step_key"] == key)
            if row["status"] in ("pending", "running", "unknown"):
                self.store.set_provision_step(rental_id, key, "skipped")
        try:
            moved = self._move(current, "provision_failed", error_code=failure.code, detail=f"{failure.step}: {detail}")
        except GPUServiceError:
            moved = False
        if not moved:                      # CAS thua: bên khác đổi trạng thái giữa lúc đọc và ghi
            return self.rental(rental_id)
        log.warning("%s provision_failed ở %s (%s); destroy theo on_failure", rental_id, failure.step, failure.code)
        try:
            return self._destroy(rental_id, reason=f"on_failure: {failure.code}")
        except (GPUServiceError, GPUError) as exc:
            self.store.add_gpu_event(rental_id, "on_failure_destroy", "destroy_refused", self._clean(str(exc)))
            return self.rental(rental_id)

    def _start(self, steps: dict, rental_id: str, key: str) -> None:
        if steps[key]["status"] == "pending":
            # Chỉ chuyển từ pending trong SQL: hai lượt poll song song không đếm lượt thử hai lần.
            self.store.set_provision_step(rental_id, key, "running", only_from="pending")
            steps[key] = {**steps[key], "status": "running", "attempt": steps[key]["attempt"] + 1}

    def _running(self, steps: dict, rental_id: str, key: str, fields: dict) -> None:
        if steps[key]["status"] != "running":
            self.store.set_provision_step(rental_id, key, "running", **fields)
        else:
            self.store.update_provision_step(rental_id, key, **fields)
        steps[key] = {**steps[key], "status": "running", **fields}

    def _finish(self, steps: dict, rental_id: str, key: str, **fields) -> None:
        if steps[key]["status"] != "done":
            if steps[key]["status"] == "pending" and "attempt" not in fields:
                fields["attempt"] = 1
            self.store.set_provision_step(rental_id, key, "done", **fields)
            steps[key] = {**steps[key], "status": "done", **fields}

    # ------------------------------------------------------------ chạy workflow trên rental ready (lát D)

    def comfy_runtime(self, rental_id: str) -> comfy.ComfyUIRuntime:
        """Client ComfyUI của rental ``ready``/``busy``, qua cổng có xác thực (Bearer ``WEB_PASSWORD`` của rental).

        ``studio.comfyui.service`` gọi hàm này; web không bao giờ cầm runtime trực tiếp.
        """
        rental = self.rental(rental_id)
        if rental.status not in ("ready", "busy"):
            raise GPUServiceError(f"{rental_id} đang {rental.status}, không phải ready/busy")
        password = self._password(rental_id)
        if not password or not rental.provider_instance_id:
            raise GPUServiceError(f"{rental_id} thiếu instance hoặc {PASSWORD_FILE}")
        try:
            instance = self.provider.get_instance(rental.provider_instance_id)
        except GPUError as exc:
            raise RentalUnreachable(f"{rental_id}: {self._clean(str(exc))[:300]}") from None
        if instance is None:
            raise RentalUnreachable(f"provider không còn instance {rental.provider_instance_id}")
        try:
            runtime = self.probe_factory(instance, password).comfy
        except ProbeUnavailable as exc:
            raise RentalUnreachable(f"{rental_id}: {exc}") from None
        deadline = self._idle_deadline()
        if deadline is not None:           # studio đang dùng rental (chạy, theo dõi, tải output): lùi hạn idle
            self.store.touch_rental_idle(rental_id, deadline)
        return runtime

    def claim_for_run(self, rental_id: str, run_id: str) -> bool:
        """``ready→busy`` bằng CAS: tối đa một run trên một instance. False khi rental không ``ready`` hoặc thua race."""
        rental = self.rental(rental_id)
        return rental.status == "ready" and self._move(rental, "busy", detail=f"run {run_id}")

    def release_after_run(self, rental_id: str, run_id: str) -> bool:
        """``busy→ready`` khi run đã xong phần chạy trên GPU.

        Điều kiện "không còn run nào khác của rental đang chạy" nằm trong cùng câu UPDATE với CAS (status, version):
        run vừa ghi ``checking`` mà chưa kịp giành máy không bị nhả nhầm, và lượt nhả muộn của run cũ không nhả máy
        đang giữ cho run mới.
        """
        rental = self.rental(rental_id)
        if rental.status != "busy":
            return False
        moved = self.store.release_rental(rental_id, rental.version, run_id, RUN_EXECUTING,
                                          idle_deadline=self._idle_deadline())
        if moved:
            self._sample_cost(rental_id)
        return moved

    def unverified_runs(self, rental_id: str) -> list[dict]:
        """Run ComfyUI của rental còn output chưa tải/kiểm: trang rental cảnh báo trước khi destroy (G11)."""
        return self.store.comfy_runs(rental_id=rental_id, statuses=RUN_UNRESOLVED)

    def redact(self, text: str) -> str:
        """Che ``WEB_PASSWORD`` của mọi rental và khóa API trước khi ghi sự kiện hay hiện lỗi."""
        return self._clean(text)

    # ------------------------------------------------------------ guardrail: idle TTL, thời gian thuê, chi phí (lát E)

    def breaches(self, rental: Rental) -> list[Breach]:
        """Giới hạn ``rental`` đang vượt theo đồng hồ và chi phí ước tính hiện tại (``studio.gpu.limits``)."""
        return limits.check(rental, self.cost(rental.rental_id), self.limits, self.clock())

    def over_limits(self) -> list[dict]:
        """Rental đang vượt giới hạn, cho trang ``/gpu``. ``unverified_runs`` khác rỗng nghĩa là guardrail không tự
        destroy được (G11): người vận hành phải tải output hoặc xác nhận bỏ rồi destroy, máy vẫn tính phí tới lúc đó."""
        rows = []
        for rental in self.rentals():
            found = self.breaches(rental)
            if found:
                rows.append({"rental": rental, "breaches": found,
                             "unverified_runs": self.unverified_runs(rental.rental_id)})
        return rows

    def enforce_limits(self) -> list[Enforcement]:
        """Một lượt guardrail: rental ``provisioning``/``provision_failed``/``ready``/``busy`` vượt giới hạn thì destroy.

        Destroy đi qua đúng đường của nút Destroy (operation ghi trước, CAS ``→destroying`` kèm điều kiện không còn
        run chưa kiểm, ``destroy_unknown`` khi lỗi; G09, G11), không bao giờ kèm ``accept_unverified_output``. Rental
        ``destroying``/``destroy_unknown`` không gửi destroy lần nữa (``tick`` reconcile chúng bằng GET). Một rental lỗi
        không làm bỏ qua các rental khác.
        """
        results = []
        for rental in self.rentals():
            if rental.status not in limits.ENFORCED:
                continue
            try:
                result = self._enforce(rental)
            except (GPUServiceError, GPUError) as exc:
                message = self._clean(str(exc))
                log.warning("guardrail %s: %s", rental.rental_id, message)
                self.store.add_gpu_event(rental.rental_id, "limit", "enforce_error", message)
                continue
            if result is not None:
                results.append(result)
        return results

    def _enforce(self, rental: Rental) -> Enforcement | None:
        found = self.breaches(rental)
        if not found:
            return None
        rental_id = rental.rental_id
        self._password(rental_id)  # rental có thể vừa được tạo bởi tiến trình web sau khi guard khởi động
        kinds = ",".join(b.kind for b in found)
        summary = self._clean("; ".join(b.detail for b in found))
        # Chỉ vượt idle: điều kiện "hạn idle đã qua" đi cùng câu CAS, hoạt động chen giữa (không tăng version) làm
        # destroy thua thay vì hủy máy vừa được dùng.
        idle_only = all(b.kind == "idle_ttl" for b in found)
        log.warning("GPU %s (%s) vượt giới hạn [%s]: %s; destroy", rental_id, rental.status, kinds, summary)
        try:
            self._destroy(rental_id, idle_before=self.clock().isoformat(timespec="seconds") if idle_only else None,
                          reason=f"guardrail [{kinds}]: {summary}")
        except GPUServiceError as exc:     # gồm UnverifiedOutput
            error = exc
        else:
            current = self.rental(rental_id)
            return Enforcement(rental_id=rental_id, status=current.status, breaches=found, outcome="destroy_sent")
        # Destroy không đi. Đọc lại: bên khác đã đổi trạng thái (nút Destroy, run mới giành máy) thì không còn vượt.
        current = self.rental(rental_id)
        still = self.breaches(current)
        if not still:
            log.info("GPU %s: không còn vượt giới hạn sau khi đọc lại (%s)", rental_id, current.status)
            return Enforcement(rental_id=rental_id, status=current.status, breaches=found, outcome="cleared")
        summary = self._clean("; ".join(b.detail for b in still))
        runs = [r["run_id"] for r in self.unverified_runs(rental_id)]
        if runs:
            log.error("GPU %s VƯỢT GIỚI HẠN nhưng KHÔNG tự destroy: còn %d run có output chưa tải/kiểm (%s); máy vẫn "
                      "tính phí. Tải output hoặc xác nhận bỏ output rồi destroy trên /gpu/instances/%s. %s",
                      rental_id, len(runs), ", ".join(runs), rental_id, summary)
            self._note_once(rental_id, "limit_blocked", "unverified_output",
                            f"{summary}; còn run có output chưa tải/kiểm: {', '.join(runs)}; không tự destroy (G11), "
                            "cần người vận hành")
            return Enforcement(rental_id=rental_id, status=current.status, breaches=still,
                               outcome="blocked_unverified_output", runs=runs)
        message = self._clean(str(error))[:300]
        log.error("GPU %s VƯỢT GIỚI HẠN nhưng destroy bị từ chối: %s. %s", rental_id, message, summary)
        self._note_once(rental_id, "limit_blocked", "destroy_refused", f"{summary}; destroy bị từ chối: {message}")
        return Enforcement(rental_id=rental_id, status=current.status, breaches=still, outcome="refused", detail=message)

    def tick(self) -> dict:
        """Một lượt guardrail của worker (``start_guard``) và ``gpu poll``: poll provisioning, thực thi giới hạn, rồi
        reconcile các destroy chưa xác nhận (chỉ GET, không gửi destroy lần nữa; G09)."""
        for rental in self.rentals():
            if rental.status == "create_unknown":
                self.reconcile(rental.rental_id)
        polled = self.poll()
        enforced = self.enforce_limits()
        for rental in self.rentals():
            if rental.status in ("destroying", "destroy_unknown"):
                try:
                    self.reconcile(rental.rental_id)
                except (GPUServiceError, GPUError) as exc:
                    log.warning("reconcile %s: %s", rental.rental_id, self._clean(str(exc)))
        return {"polled": polled, "enforced": enforced}

    def _note_once(self, rental_id: str, transition: str, code: str, detail: str) -> None:
        """Sự kiện lặp mỗi lượt poll (cùng ``transition`` và mã với sự kiện cuối) chỉ ghi một lần: ``gpu_events`` chỉ
        thêm, không được phình theo số lượt worker."""
        events = self.store.gpu_events(rental_id)
        if events and (events[-1]["transition"], events[-1]["error_code"]) == (transition, code):
            return
        self.store.add_gpu_event(rental_id, transition, code, self._clean(detail))

    # ------------------------------------------------------------ chi phí ước tính

    def cost(self, rental_id: str) -> dict:
        """Chi phí ước tính tới hiện tại (không phải hóa đơn); giả định ghi ở ``studio.gpu.costs``."""
        return {**costs.summary(self._cost_samples(rental_id), self.clock()),
                "dph_total_includes_disk": self.rental(rental_id).offer.dph_total_includes_disk}

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
        if status == "ready":
            fields.setdefault("idle_deadline", self._idle_deadline())     # idle TTL tính từ lúc vào ready
        moved = self.store.transition_rental(rental.rental_id, rental.version, status, expect=rental.status,
                                             error_code=error_code, detail=self._clean(detail) if detail else None,
                                             **fields)
        if moved:
            self._sample_cost(rental.rental_id)
        return moved

    def _clean(self, text: str) -> str:
        text = studio_config.redact(text)
        for secret in self._secrets:
            if secret and len(secret) >= 6:
                text = text.replace(secret, "<WEB_PASSWORD>")
        return text[:1000]

    # ------------------------------------------------------------ đọc

    def rental(self, rental_id: str) -> Rental:
        return _rental(self.store.rental(rental_id))

    def rentals(self) -> list[Rental]:
        return [_rental(row) for row in self.store.rentals() if row["provider_key"] == self.provider_key]

    def timeline(self, rental_id: str) -> dict:
        rental = self.rental(rental_id)
        record = None
        if rental.manifest_sha256:
            try:
                record = self.manifest(rental.manifest_sha256)
            except KeyError:
                pass
        return {"rental": rental, "manifest": record, "manifest_view": self.manifest_view(record) if record else None,
                "steps": [ProvisionStep(**row) for row in self.store.provision_steps(rental_id)],
                "events": [GPUEvent(**{k: row[k] for k in GPUEvent.model_fields}) for row in self.store.gpu_events(rental_id)],
                "operations": self.store.gpu_operations(rental_id),
                "cost": self.cost(rental_id),
                "unverified_runs": self.unverified_runs(rental_id),
                "limits": self.limits, "breaches": self.breaches(rental),
                "idle_deadline": limits.idle_deadline(rental, self.limits)
                if rental.status in limits.IDLE_STATES else None}


def _rental(row: dict) -> Rental:
    data = {k: v for k, v in row.items() if k != "offer_json"}
    return Rental(**data, offer=Offer.model_validate_json(row["offer_json"]))


def _orphan(instance: Instance, reason: str) -> Orphan:
    return Orphan(provider_instance_id=instance.provider_instance_id, label=instance.label,
                  actual_status=instance.actual_status, dph_total=instance.dph_total, reason=reason)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _onstart(yaml_text: str, checksum_script: str | None = None) -> str:
    """Ghi manifest đã duyệt lên instance cho provisioner; base64 nên không có dấu nháy, không chèn được lệnh.

    Có model thì ghi thêm script SHA-256 (``provisioning.checksum_script``, cũng base64) và chạy nền: nó chờ
    ``/.provisioning_complete`` rồi mới tính, nên không chặn provisioner.
    """
    encoded = base64.b64encode(yaml_text.encode()).decode()
    target = provisioning.REMOTE_MANIFEST
    command = f"mkdir -p {os.path.dirname(target)} && printf '%s' '{encoded}' | base64 -d > {target}"
    if checksum_script:
        script = provisioning.REMOTE_CHECKSUM_SCRIPT
        encoded = base64.b64encode(checksum_script.encode()).decode()
        command += (f" && printf '%s' '{encoded}' | base64 -d > {script} && chmod 700 {script}"
                    f" && (nohup sh {script} >/dev/null 2>&1 &)")
    return command + " && exec " + provisioning.BOOT_COMMAND


def from_config(store: Store, config: dict) -> GPUService | None:
    """Service theo ``[gpu]``; ``provider = "none"`` (mặc định, D-05 chưa mở) trả None."""
    provider = adapters.get(config)
    if provider is None:
        return None
    gpu = config.get("gpu", {})
    try:
        guard = Limits(**gpu.get("limits", {}))
    except ValidationError as exc:
        raise GPUServiceError(f"cấu hình [gpu.limits]: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}") from None
    return GPUService(store, provider, image=gpu.get("image", ""), quote_ttl_s=float(gpu.get("quote_ttl_s", QUOTE_TTL_S)),
                      data_dir=Path(config["data_dir"]) / "gpu" if config.get("data_dir") else None,
                      portal=gpu.get("portal"),
                      provision_timeout_s=float(gpu.get("provision_timeout_s", PROVISION_TIMEOUT_S)), limits=guard)


def start_guard(store: Store, config: dict, *, stop: threading.Event | None = None) -> threading.Thread | None:
    """Luồng nền trong worker hiện có (không process mới; 09 · s2.b3): ``recover()`` một lần rồi ``tick()`` mỗi
    ``[gpu] guard_interval_s`` giây. None khi ``[gpu] provider = "none"``.

    Guardrail mềm: worker không chạy thì không có gì destroy máy vượt giới hạn; lưới cuối là số dư nạp trước nhỏ trên
    tài khoản provider (09 · s4.b2).
    """
    gpu = from_config(store, config)
    if gpu is None:
        return None
    try:
        interval = float(config.get("gpu", {}).get("guard_interval_s", GUARD_INTERVAL_S))
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError
    except (ValueError, TypeError):
        raise GPUServiceError("[gpu] guard_interval_s phải là số hữu hạn > 0") from None
    stop = stop or threading.Event()

    def loop() -> None:
        try:
            gpu.recover()
        except Exception as exc:  # noqa: BLE001 — guardrail không được chết vì lỗi lúc khởi động
            log.error("guardrail GPU khi khởi động: %s", gpu.redact(str(exc)))
        while not stop.is_set():
            try:
                gpu.tick()
            except Exception as exc:  # noqa: BLE001 — một lượt lỗi không dừng guardrail
                log.error("guardrail GPU: %s", gpu.redact(str(exc)))
            if stop.wait(interval):
                return

    thread = threading.Thread(target=loop, name="gpu-guard", daemon=True)
    thread.start()
    log.info("guardrail GPU chạy mỗi %gs (%s)", interval, gpu.limits.model_dump())
    return thread
