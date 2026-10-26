"""Client HTTP tối thiểu cho một process ComfyUI đang chạy trên instance thuê (09 · ComfyUI workflow và lượt chạy).

Chỉ nói chuyện qua HTTP với ComfyUI (GPL-3.0) chạy như process riêng; không import hay vendor mã ComfyUI. Mọi
request đi qua cổng có xác thực của Instance Portal (Caddy) với ``Authorization: Bearer <WEB_PASSWORD>``.

Lát C chỉ dùng phần cần cho health + smoke: ``GET /system_stats``, ``POST /prompt`` kèm ``prompt_id`` do client
sinh, ``GET /history/{prompt_id}``, ``GET /queue``, ``GET /view`` (đọc vào bộ nhớ, có trần dung lượng). Lát D mở
rộng chính lớp này: ``/object_info`` kiểm ``class_type`` trước khi chạy, WebSocket ``/ws``, tải output ra đĩa
qua ``safe_path`` và kiểm SHA-256/loại media.

Ánh xạ lỗi cùng nghĩa với ``Retryable``/``Unconfirmed`` của OmniRoute và ``GPUError``/``GPUUnknown`` của GPU:

- ``ComfyUIUnavailable``: chưa tới được ComfyUI (không mở được kết nối, GET timeout, 5xx/502 khi backend chưa
  lên). GET gọi lại an toàn ở lượt poll sau.
- ``ComfyUIUnknown``: ``POST /prompt`` có thể đã tới (read timeout, mất kết nối giữa chừng, 5xx, response lạ).
  Không submit lại mù; tra ``/history/{prompt_id}`` và ``/queue`` (G08).
- ``ComfyUIAuthError``: 401/403. ``ComfyUIRejected``: 4xx khác, ví dụ 400 ``node_errors`` của ``/prompt``.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import requests

SMOKE_PREFIX = "igniterra_smoke"
VIEW_MAX_BYTES = 10 * 1024 * 1024
SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}")
SAFE_SUBFOLDER = re.compile(r"(?:[A-Za-z0-9._-]{1,100}(?:/[A-Za-z0-9._-]{1,100}){0,4})?")
FOLDER_TYPES = ("output", "temp")


class ComfyUIError(Exception):
    """Lỗi chung của runtime ComfyUI."""


class ComfyUIUnavailable(ComfyUIError):
    """Chưa tới được ComfyUI; request chắc chắn không có tác dụng (GET thì gọi lại được)."""


class ComfyUIUnknown(ComfyUIError):
    """Mutation có thể đã tới ComfyUI; tra theo ``prompt_id``, không gửi lại."""


class ComfyUIAuthError(ComfyUIError):
    """401/403: thiếu hoặc sai token."""


class ComfyUIRejected(ComfyUIError):
    """ComfyUI từ chối chắc chắn (4xx), ví dụ workflow lỗi ``node_errors``."""


def smoke_prompt() -> dict:
    """Workflow smoke dạng API: ``EmptyImage`` → ``SaveImage``. Hai node lõi, không cần model, ra một file PNG."""
    return {
        "1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 0}},
        "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": SMOKE_PREFIX}},
    }


def output_files(entry: dict) -> list[dict]:
    """File output trong một mục ``/history``: ``[{"filename", "subfolder", "type", "node"}]``."""
    files = []
    for node_id, output in (entry.get("outputs") or {}).items():
        if not isinstance(output, dict):
            continue
        for kind in ("images", "gifs", "videos", "audio"):
            for item in output.get(kind) or []:
                if isinstance(item, dict) and item.get("filename"):
                    files.append({"filename": str(item["filename"]), "subfolder": str(item.get("subfolder") or ""),
                                  "type": str(item.get("type") or "output"), "node": str(node_id)})
    return files


def history_status(entry: dict) -> str:
    """``success`` | ``error`` | ``running`` theo ``status`` của mục history."""
    status = entry.get("status") or {}
    text = str(status.get("status_str") or "")
    if text == "error":
        return "error"
    if status.get("completed") or text == "success":
        return "success"
    return "running"


class ComfyUIRuntime:
    def __init__(self, base_url: str, token: str | None, *, timeout_s: float = 30, connect_timeout_s: float = 10,
                 verify: bool | str = True):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = float(timeout_s)
        self.connect_timeout = float(connect_timeout_s)
        self.verify = verify

    def __repr__(self) -> str:                     # không lộ token trong log
        return f"ComfyUIRuntime({self.base_url!r})"

    # ------------------------------------------------------------ HTTP

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get(self, path: str, params: dict | None = None, *, stream: bool = False) -> requests.Response:
        try:
            response = requests.get(f"{self.base_url}{path}", params=params, headers=self._headers(),
                                    timeout=(self.connect_timeout, self.timeout), verify=self.verify, stream=stream,
                                    allow_redirects=False)
        except requests.exceptions.RequestException as exc:
            raise ComfyUIUnavailable(f"GET {path}: {exc}") from None
        self._check(response, f"GET {path}", unknown=False)
        return response

    def _check(self, response: requests.Response, what: str, *, unknown: bool) -> None:
        status = response.status_code
        if status in (401, 403) or (300 <= status < 400 and "login" in response.headers.get("location", "").lower()):
            raise ComfyUIAuthError(f"HTTP {status} {what}: cần xác thực")
        if status >= 500:
            error = ComfyUIUnknown if unknown else ComfyUIUnavailable
            raise error(f"HTTP {status} {what}: {response.text[:300]}")
        if 400 <= status < 500:
            raise ComfyUIRejected(f"HTTP {status} {what}: {response.text[:600]}")
        if status != 200:
            error = ComfyUIUnknown if unknown else ComfyUIUnavailable
            raise error(f"HTTP {status} {what}")

    def _json(self, response: requests.Response, what: str, *, unknown: bool = False) -> dict:
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            error = ComfyUIUnknown if unknown else ComfyUIUnavailable
            raise error(f"{what}: response không phải JSON object: {response.text[:300]}")
        return data

    # ------------------------------------------------------------ API

    def system_stats(self) -> dict:
        """``GET /system_stats``: phiên bản ComfyUI, Python, thiết bị (VRAM). Bằng chứng runtime đã lên."""
        data = self._json(self._get("/system_stats"), "GET /system_stats")
        if not isinstance(data.get("system"), dict):
            raise ComfyUIUnavailable(f"/system_stats thiếu 'system': {str(data)[:300]}")
        return data

    def submit(self, prompt: dict, prompt_id: str, client_id: str = "igniterra") -> dict:
        """``POST /prompt`` với ``prompt_id`` đã ghi vào SQLite trước khi gọi.

        ComfyUI cũ bỏ qua ``prompt_id`` của client và tự sinh: khi id trả về khác, báo lỗi để ghim bản ComfyUI
        có tính năng này (09 · s6), không chấp nhận id lạ.
        """
        what = "POST /prompt"
        try:
            response = requests.post(f"{self.base_url}/prompt", json={"prompt": prompt, "prompt_id": prompt_id,
                                                                      "client_id": client_id},
                                     headers=self._headers(), timeout=(self.connect_timeout, self.timeout),
                                     verify=self.verify, allow_redirects=False)
        except requests.exceptions.ConnectTimeout as exc:
            raise ComfyUIUnavailable(f"{what}: {exc}") from None
        except requests.exceptions.ConnectionError as exc:
            text = str(exc)
            if "Failed to establish a new connection" in text or "Connection refused" in text:
                raise ComfyUIUnavailable(f"{what}: {exc}") from None
            raise ComfyUIUnknown(f"mất kết nối sau khi gửi {what}: {exc}") from None
        except requests.exceptions.ReadTimeout:
            raise ComfyUIUnknown(f"quá {self.timeout:g}s không có phản hồi sau khi gửi {what}") from None
        except requests.exceptions.RequestException as exc:
            raise ComfyUIUnknown(f"lỗi HTTP sau khi gửi {what}: {exc}") from None
        self._check(response, what, unknown=True)
        data = self._json(response, what, unknown=True)
        returned = data.get("prompt_id")
        if returned != prompt_id:
            raise ComfyUIRejected(f"ComfyUI trả prompt_id {returned!r}, đã gửi {prompt_id!r}: "
                                  "bản ComfyUI này không nhận prompt_id do client đặt")
        return data

    def history(self, prompt_id: str) -> dict | None:
        """Mục ``/history/{prompt_id}``; None khi ComfyUI chưa có (``{}``)."""
        data = self._json(self._get(f"/history/{quote(prompt_id, safe='')}"), "GET /history")
        entry = data.get(prompt_id)
        return entry if isinstance(entry, dict) else None

    def queue(self) -> dict:
        return self._json(self._get("/queue"), "GET /queue")

    def in_queue(self, prompt_id: str) -> bool:
        """``/queue`` trả ``queue_running``/``queue_pending`` là list ``[number, prompt_id, prompt, …]``."""
        data = self.queue()
        for key in ("queue_running", "queue_pending"):
            for item in data.get(key) or []:
                if isinstance(item, list) and len(item) > 1 and item[1] == prompt_id:
                    return True
        return False

    def view(self, filename: str, subfolder: str = "", folder_type: str = "output",
             max_bytes: int = VIEW_MAX_BYTES) -> bytes:
        """``GET /view`` vào bộ nhớ, có trần dung lượng. Tên file và subfolder phải sạch (không ``..``, ``/`` đầu)."""
        if not SAFE_NAME.fullmatch(filename) or filename in (".", ".."):
            raise ComfyUIRejected(f"tên file output lạ: {filename!r}")
        if not SAFE_SUBFOLDER.fullmatch(subfolder) or ".." in subfolder.split("/"):
            raise ComfyUIRejected(f"subfolder output lạ: {subfolder!r}")
        if folder_type not in FOLDER_TYPES:
            raise ComfyUIRejected(f"type output lạ: {folder_type!r}")
        response = self._get("/view", {"filename": filename, "subfolder": subfolder, "type": folder_type}, stream=True)
        chunks, size = [], 0
        try:
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ComfyUIRejected(f"output {filename} lớn hơn {max_bytes} byte")
                chunks.append(chunk)
        except requests.exceptions.RequestException as exc:
            raise ComfyUIUnavailable(f"GET /view {filename}: {exc}") from None
        finally:
            response.close()
        return b"".join(chunks)
