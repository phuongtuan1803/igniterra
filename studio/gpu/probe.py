"""Đọc tiến độ provisioning và kiểm xác thực của instance qua Instance Portal (09 · Provisioning trên instance).

Không dùng SSH ở lát C: s5.b2 cho phép đọc tiến độ qua Instance Portal (TLS + auth sẵn trong base-image), nên
mọi bằng chứng ``ready`` đi qua một kênh HTTP có Bearer ``WEB_PASSWORD``: portal cho tiến độ và
``/.provisioning_complete``, cổng ComfyUI (sau Caddy) cho ``/system_stats`` và smoke.

Mặc định đọc ``/capabilities`` của base-image: ``provisioning.status`` xác nhận sentinel tổng thể, không có tiến
độ từng bước. Báo cáo chi tiết ``ProvisioningReport`` vẫn được nhận nếu dùng endpoint riêng qua ``status_path``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import requests
from pydantic import BaseModel

from studio.comfyui.runtime import ComfyUIAuthError, ComfyUIError, ComfyUIRuntime
from studio.gpu.contracts import Instance
from studio.gpu.provisioning import ProvisioningReport

DEFAULTS = {"scheme": "https", "portal_port": "1111/tcp", "comfyui_port": "8188/tcp",
            "status_path": "/capabilities", "timeout_s": 15, "verify_tls": True}


class ProbeUnavailable(Exception):
    """Chưa tới được instance (chưa có cổng, kết nối lỗi, 5xx). Thử lại ở lượt poll sau."""


class TransportCheck(BaseModel):
    portal_auth_required: bool          # portal từ chối request không token
    comfyui_auth_required: bool         # cổng ComfyUI từ chối request không token (không AUTH_EXCLUDE)
    token_accepted: bool                # portal nhận Bearer WEB_PASSWORD


@runtime_checkable
class InstanceProbe(Protocol):
    comfy: ComfyUIRuntime

    def transport(self) -> TransportCheck: ...

    def report(self) -> ProvisioningReport: ...


class PortalProbe:
    def __init__(self, portal_url: str, comfy_url: str, token: str, *, status_path: str = DEFAULTS["status_path"],
                 timeout_s: float = 15, verify: bool | str = True):
        self.portal_url = portal_url.rstrip("/")
        self.status_path = status_path
        self.token = token
        self.timeout = float(timeout_s)
        self.verify = verify
        self.comfy = ComfyUIRuntime(comfy_url, token, timeout_s=timeout_s, verify=verify)
        self._anonymous = ComfyUIRuntime(comfy_url, None, timeout_s=timeout_s, verify=verify)

    def __repr__(self) -> str:
        return f"PortalProbe({self.portal_url!r})"

    @classmethod
    def for_instance(cls, instance: Instance, token: str, settings: dict | None = None) -> PortalProbe:
        """URL theo cổng provider báo cho instance (``"8188/tcp" → "host:port"``)."""
        cfg = {**DEFAULTS, **(settings or {})}
        portal, comfy = instance.ports.get(cfg["portal_port"]), instance.ports.get(cfg["comfyui_port"])
        if not portal or not comfy or portal.endswith(":") or comfy.endswith(":"):
            raise ProbeUnavailable(f"instance {instance.provider_instance_id} chưa có cổng "
                                   f"{cfg['portal_port']}/{cfg['comfyui_port']}")
        scheme = cfg["scheme"]
        return cls(f"{scheme}://{portal}", f"{scheme}://{comfy}", token, status_path=cfg["status_path"],
                   timeout_s=float(cfg["timeout_s"]), verify=cfg["verify_tls"])

    def _status(self, token: str | None) -> requests.Response:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = requests.get(f"{self.portal_url}{self.status_path}", headers=headers,
                                    timeout=(min(self.timeout, 10), self.timeout), verify=self.verify,
                                    allow_redirects=False)
        except requests.exceptions.RequestException as exc:
            raise ProbeUnavailable(f"Instance Portal: {exc}") from None
        if response.status_code >= 500:
            raise ProbeUnavailable(f"Instance Portal HTTP {response.status_code}")
        return response

    @staticmethod
    def _denied(response: requests.Response) -> bool:
        location = response.headers.get("location", "").lower()
        return response.status_code in (401, 403) or (300 <= response.status_code < 400 and "login" in location)

    def transport(self) -> TransportCheck:
        portal_auth = self._denied(self._status(None))
        try:
            self._anonymous.system_stats()
            comfy_auth = False
        except ComfyUIAuthError:
            comfy_auth = True
        except ComfyUIError:
            # Caddy kiểm auth trước khi proxy: request không token mà không bị 401 thì cổng chưa sẵn sàng
            # (502 khi backend chưa lên vẫn coi là chưa rõ, không phải "mở").
            raise ProbeUnavailable("cổng ComfyUI chưa trả lời request không token") from None
        accepted = self._status(self.token)
        return TransportCheck(portal_auth_required=portal_auth, comfyui_auth_required=comfy_auth,
                              token_accepted=accepted.status_code == 200)

    def report(self) -> ProvisioningReport:
        response = self._status(self.token)
        if response.status_code != 200:
            raise ProbeUnavailable(f"Instance Portal HTTP {response.status_code} khi đọc tiến độ")
        try:
            data = response.json()
            if "provisioning" in data:
                status = data["provisioning"]["status"]
                data = {"complete": status == "complete", "steps": {
                    "runtime_setup": {"status": "failed", "final": True, "error_code": "provision_failed",
                                      "error": "Instance Portal báo provisioning failed"}
                } if status == "failed" else {},
                        "log_ref": data["provisioning"].get("log_file")}
            return ProvisioningReport.model_validate(data)
        except (ValueError, KeyError, TypeError) as exc:
            raise ProbeUnavailable(f"tiến độ provisioning không đọc được: {str(exc)[:300]}") from None
