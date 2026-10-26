"""VastGPUProvider: client REST nhỏ trên ``requests`` cho Vast.ai (09 · GPUProvider và adapter Vast.ai).

Không cài gói ``vastai``/``vastai-sdk`` (ghim ``pillow==12.2.0``, xung đột ``moviepy`` của MPT); endpoint đọc
từ ``vast.py`` của vast-cli (MIT) làm tài liệu tham chiếu. Khóa ở biến môi trường ``VAST_API_KEY`` (Bearer);
``base_url`` và ``timeout_s`` ở ``[gpu.vast]`` của studio.toml.

Ánh xạ lỗi cùng nghĩa với ``Retryable``/``Unconfirmed`` của OmniRoute:

- ``GPUError``: chắc chắn provider chưa làm gì — không mở được kết nối, 429 (từ chối trước khi xử lý),
  4xx, hoặc body ``success: false``.
- ``GPUUnknown``: request có thể đã tới provider — read timeout, mất kết nối giữa chừng, 5xx, response lạ.
  Service không retry mù mà reconcile theo label.

Adapter không tự retry; GET được service gọi lại khi reconcile.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime

import requests

from studio.gpu.adapters.base import GPUError, GPUUnknown
from studio.gpu.contracts import LABEL_PREFIX, Capabilities, Instance, InstanceSpec, Offer, OfferFilters

BASE_URL = "https://console.vast.ai/api/v0"
GPU_RAM_MB_PER_GB = 1000            # API lưu gpu_ram theo MB; vast-cli nhân 1000 khi lọc, chia 1000 khi hiện


def utc_now() -> datetime:
    return datetime.now(UTC)


class VastGPUProvider:
    key = "vast"

    def __init__(self, config: dict | None = None, *, clock: Callable[[], datetime] = utc_now):
        section = (config or {}).get("gpu", {}).get("vast", {})
        self.base_url = str(section.get("base_url") or BASE_URL).rstrip("/")
        self.timeout = float(section.get("timeout_s", 30))
        self.connect_timeout = float(section.get("connect_timeout_s", 10))
        self.clock = clock
        self.api_key = os.environ.get("VAST_API_KEY", "")
        if not self.api_key:
            raise GPUError("thiếu VAST_API_KEY (đặt trong .env)")

    # ------------------------------------------------------------ HTTP

    def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                 not_found: bool = False) -> dict | None:
        """Gọi API; ``not_found=True`` thì 404 trả None thay vì lỗi (instance không còn)."""
        try:
            response = requests.request(method, f"{self.base_url}{path}", json=json, params=params,
                                        headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
                                        timeout=(self.connect_timeout, self.timeout))
        except requests.exceptions.ConnectTimeout as exc:
            raise GPUError(f"không kết nối được Vast.ai: {exc}") from None
        except requests.exceptions.ConnectionError as exc:
            # Lỗi lúc mở kết nối thì chắc chắn chưa gửi; mất kết nối giữa chừng thì provider có thể đã làm.
            text = str(exc)
            if "Failed to establish a new connection" in text or "Connection refused" in text:
                raise GPUError(f"không kết nối được Vast.ai: {exc}") from None
            raise GPUUnknown(f"mất kết nối sau khi gửi {method} {path}: {exc}") from None
        except requests.exceptions.ReadTimeout:
            raise GPUUnknown(f"quá {self.timeout:g}s không có phản hồi sau khi gửi {method} {path}") from None
        except requests.exceptions.RequestException as exc:
            raise GPUUnknown(f"lỗi HTTP sau khi gửi {method} {path}: {exc}") from None
        body = response.text[:600]
        status = response.status_code
        if status == 404 and not_found:
            return None
        if status == 429:
            raise GPUError(f"429 Vast.ai giới hạn tần suất, chưa xử lý {method} {path}: {body}")
        if 400 <= status < 500:
            raise GPUError(f"HTTP {status} {method} {path}: {body}")
        if status >= 500:
            raise GPUUnknown(f"HTTP {status} sau khi gửi {method} {path}: {body}")
        if status not in (200, 201, 202):
            raise GPUUnknown(f"HTTP {status} {method} {path}: {body}")
        try:
            data = response.json()
        except ValueError:
            raise GPUUnknown(f"response không phải JSON ({method} {path}): {body}") from None
        if not isinstance(data, dict):
            raise GPUUnknown(f"response lạ ({method} {path}): {body}")
        if data.get("success") is False:
            # Provider trả lời rõ là không làm (offer đã có người thuê, instance không thuộc tài khoản…).
            raise GPUError(f"Vast.ai từ chối {method} {path}: {data.get('msg') or data.get('error') or body}")
        return data

    # ------------------------------------------------------------ Protocol

    def capabilities(self) -> Capabilities:
        return Capabilities(supports_stop=True, price_components=("compute", "storage", "inet_up", "inet_down"))

    def search_offers(self, filters: OfferFilters) -> list[Offer]:
        """``POST /bundles/``. Service vẫn kiểm lại từng offer (G02); bộ lọc ở đây chỉ để giảm số dòng.

        ``allocated_storage`` = dung lượng yêu cầu: vast-cli gửi trường này "để tính giá", gợi ý ``dph_total``
        trả về có thể đã gồm disk. Chưa xác nhận; xem ``studio.gpu.costs``.
        """
        query: dict = {
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "gpu_ram": {"gte": filters.min_gpu_ram_gb * GPU_RAM_MB_PER_GB},
            "disk_space": {"gte": filters.disk_gb},
            "reliability": {"gte": filters.min_reliability},
            "order": [["dph_total", "asc"]],
            "type": "on-demand",
            "limit": filters.limit,
            "allocated_storage": filters.disk_gb,
        }
        if filters.verified_only:
            query["verified"] = {"eq": True}
        if filters.min_cuda is not None:
            query["cuda_max_good"] = {"gte": filters.min_cuda}
        if filters.max_dph is not None:
            query["dph_total"] = {"lte": filters.max_dph}
        data = self._request("POST", "/bundles/", json=query)
        quoted_at = self.clock()
        offers = []
        for row in data.get("offers") or []:
            try:
                offers.append(self._offer(row, quoted_at))
            except (KeyError, TypeError, ValueError):
                continue                                # dòng thiếu trường bắt buộc: bỏ, không đoán
        return offers

    def create_instance(self, offer: Offer, spec: InstanceSpec) -> Instance:
        """``PUT /asks/{offer_id}/``. Luôn gửi label chủ sở hữu và ``cancel_unavail``; image đã ghim digest.

        Lời gọi này không có idempotency key: timeout → ``GPUUnknown``, service tìm theo label.
        """
        if not spec.label.startswith(LABEL_PREFIX):
            raise GPUError(f"label {spec.label!r} không có tiền tố {LABEL_PREFIX}")
        body = {
            "client_id": "me",
            "image": spec.image,
            "disk": spec.disk_gb,
            "label": spec.label,
            "cancel_unavail": True,         # không có cờ này Vast tạo instance stopped vẫn tính phí disk
            "runtype": "ssh_direc ssh_proxy",
            "env": {},
        }
        if spec.onstart:
            body["onstart"] = spec.onstart
        data = self._request("PUT", f"/asks/{int(offer.offer_id)}/", json=body)
        contract = data.get("new_contract")
        if contract in (None, ""):
            raise GPUUnknown(f"create không trả new_contract: {str(data)[:300]}")
        return Instance(provider_instance_id=str(contract), label=spec.label, actual_status="loading",
                        dph_total=offer.dph_total)

    def get_instance(self, instance_id: str) -> Instance | None:
        """``GET /instances/{id}/``; ``{"instances": null}`` hoặc 404 nghĩa là không còn."""
        data = self._request("GET", f"/instances/{int(instance_id)}/", params={"owner": "me"}, not_found=True)
        row = (data or {}).get("instances")
        if isinstance(row, list):
            row = row[0] if row else None
        return self._instance(row) if row else None

    def list_owned_instances(self) -> list[Instance]:
        """``GET /instances/``, chỉ giữ instance có label tiền tố ``igniterra:`` (G04, G09, G10)."""
        data = self._request("GET", "/instances/", params={"owner": "me"})
        rows = data.get("instances") or []
        return [self._instance(row) for row in rows
                if isinstance(row, dict) and str(row.get("label") or "").startswith(LABEL_PREFIX)]

    def stop_instance(self, instance_id: str) -> None:
        """``PUT /instances/{id}/`` ``{"state": "stopped"}``. Instance stopped vẫn tính phí storage."""
        self._request("PUT", f"/instances/{int(instance_id)}/", json={"state": "stopped"})

    def destroy_instance(self, instance_id: str) -> None:
        """``DELETE /instances/{id}/``. API chỉ trả "destroying"; đã mất hẳn hay chưa xác nhận bằng get/list (G09)."""
        self._request("DELETE", f"/instances/{int(instance_id)}/", json={})

    # ------------------------------------------------------------ ánh xạ JSON

    def _offer(self, row: dict, quoted_at: datetime) -> Offer:
        verified = row.get("verified")
        if not isinstance(verified, bool):
            verified = row.get("verification") == "verified"
        reliability = row.get("reliability2", row.get("reliability"))
        return Offer(provider_key=self.key, offer_id=str(row["id"]), gpu_name=str(row["gpu_name"]),
                     num_gpus=int(row.get("num_gpus") or 1), gpu_ram_gb=float(row["gpu_ram"]) / GPU_RAM_MB_PER_GB,
                     disk_space_gb=float(row["disk_space"]), reliability=_float(reliability),
                     cuda_max_good=_float(row.get("cuda_max_good")), verified=verified,
                     rentable=bool(row.get("rentable", True)), dph_total=float(row["dph_total"]),
                     storage_cost=_float(row.get("storage_cost")), inet_up_cost=_float(row.get("inet_up_cost")),
                     inet_down_cost=_float(row.get("inet_down_cost")), geolocation=row.get("geolocation"),
                     quoted_at=quoted_at)

    @staticmethod
    def _instance(row: dict) -> Instance:
        host = row.get("public_ipaddr") or ""
        ports = {}
        for container_port, bindings in (row.get("ports") or {}).items():
            if isinstance(bindings, list) and bindings and isinstance(bindings[0], dict):
                ports[str(container_port)] = f"{host}:{bindings[0].get('HostPort', '')}".lstrip(":")
        return Instance(provider_instance_id=str(row["id"]), label=row.get("label"),
                        actual_status=str(row.get("actual_status") or row.get("cur_state") or "unknown"),
                        dph_total=_float(row.get("dph_total")), ports=ports)


def _float(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
