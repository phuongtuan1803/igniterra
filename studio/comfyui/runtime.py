"""Client HTTP tối thiểu cho một process ComfyUI đang chạy trên instance thuê (09 · ComfyUI workflow và lượt chạy).

Chỉ nói chuyện qua HTTP với ComfyUI (GPL-3.0) chạy như process riêng; không import hay vendor mã ComfyUI. Mọi
request đi qua cổng có xác thực của Instance Portal (Caddy) với ``Authorization: Bearer <WEB_PASSWORD>``.

Lát C dùng phần cần cho health + smoke: ``GET /system_stats``, ``POST /prompt`` kèm ``prompt_id`` do client
sinh, ``GET /history/{prompt_id}``, ``GET /queue``, ``GET /view`` (đọc vào bộ nhớ, có trần dung lượng). Lát D mở
rộng chính lớp này: ``GET /object_info`` để kiểm ``class_type`` trước khi chạy, vị trí trong ``/queue``, sự kiện
cuối trong ``/history``, và ``download()`` stream ``/view`` ra đĩa (file tạm rồi đổi tên, kiểm ``Content-Length``,
tính SHA-256). Đường dẫn đích và loại media do ``studio.comfyui.runs`` kiểm. Theo dõi chỉ bằng poll ``/queue`` +
``/history``; WebSocket ``/ws`` chưa làm (09 · s6 cho phép poll khi không có ``/ws``).

Ánh xạ lỗi cùng nghĩa với ``Retryable``/``Unconfirmed`` của OmniRoute và ``GPUError``/``GPUUnknown`` của GPU:

- ``ComfyUIUnavailable``: chưa tới được ComfyUI (không mở được kết nối, GET timeout, 5xx/502 khi backend chưa
  lên). GET gọi lại an toàn ở lượt poll sau.
- ``ComfyUIUnknown``: ``POST /prompt`` có thể đã tới (read timeout, mất kết nối giữa chừng, 5xx, response lạ).
  Không submit lại mù; tra ``/history/{prompt_id}`` và ``/queue`` (G08).
- ``ComfyUIAuthError``: 401/403. ``ComfyUIRejected``: 4xx khác, ví dụ 400 ``node_errors`` của ``/prompt``.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from urllib.parse import quote

import requests

SMOKE_PREFIX = "igniterra_smoke"
VIEW_MAX_BYTES = 10 * 1024 * 1024
DOWNLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024       # output video lớn nhất nhận về (2 GiB)
WORKFLOW_MAX_BYTES = 5 * 1024 * 1024               # workflow JSON đọc từ userdata của ComfyUI gốc
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


def last_event(entry: dict) -> tuple[str | None, int | None, str | None]:
    """Sự kiện cuối trong ``status.messages`` của mục history: (tên, timestamp ms, node). Không có thì None."""
    messages = (entry.get("status") or {}).get("messages") or []
    for item in reversed(messages):
        if isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict):
            name, data = str(item[0]), item[1]
            node = data.get("node_id") or data.get("node")
            if node is None and data.get("nodes"):
                node = ",".join(str(n) for n in data["nodes"])
            stamp = data.get("timestamp")
            return name, int(stamp) if isinstance(stamp, (int, float)) else None, str(node) if node else None
    return None, None, None


def check_output_ref(filename: str, subfolder: str, folder_type: str) -> None:
    """Tên file, subfolder, type của output phải sạch trước khi gọi ``/view`` hoặc ghi ra đĩa."""
    if not SAFE_NAME.fullmatch(filename) or filename in (".", ".."):
        raise ComfyUIRejected(f"tên file output lạ: {filename!r}")
    if not SAFE_SUBFOLDER.fullmatch(subfolder) or ".." in subfolder.split("/") or "." in subfolder.split("/"):
        raise ComfyUIRejected(f"subfolder output lạ: {subfolder!r}")
    if folder_type not in FOLDER_TYPES:
        raise ComfyUIRejected(f"type output lạ: {folder_type!r}")


def _clean_userdata_path(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return bool(path) and not path.startswith("/") and all(part not in ("", ".", "..") for part in parts)


class Download:
    """Kết quả ``download()``: file đã ghi đủ byte, SHA-256 tính khi stream, ``Content-Type`` server báo."""

    def __init__(self, path: Path, size: int, sha256: str, content_type: str, head: bytes):
        self.path, self.size, self.sha256, self.content_type, self.head = path, size, sha256, content_type, head


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

    def busy_elsewhere(self) -> bool:
        """Hàng đợi có job (đang chạy hoặc chờ), bất kể ai gửi: dấu hiệu máy đang được dùng trong ComfyUI gốc."""
        data = self.queue()
        return any(data.get(key) for key in ("queue_running", "queue_pending"))

    def history_head(self) -> str | None:
        """``prompt_id`` mới nhất trong ``/history`` (``max_items=1``); None khi chưa chạy gì. Đổi giữa hai lượt đọc nghĩa
        là đã có job chạy xong trong lúc đó."""
        data = self._json(self._get("/history", {"max_items": 1}), "GET /history")
        keys = [key for key in data if isinstance(key, str)]
        return keys[-1] if keys else None

    def saved_workflows(self) -> list[dict]:
        """Workflow người dùng đã Save trong ComfyUI gốc: ``GET /api/userdata?dir=workflows`` (thư mục
        ``user/default/workflows``). Mỗi mục: ``path`` (tương đối trong thư mục đó), ``size``, ``modified`` (epoch giây)."""
        response = self._get("/api/userdata", {"dir": "workflows", "recurse": "true", "split": "false",
                                               "full_info": "true"})
        try:
            data = response.json()
        except ValueError:
            raise ComfyUIUnavailable(f"GET /api/userdata: không phải JSON: {response.text[:300]}") from None
        if not isinstance(data, list):
            raise ComfyUIUnavailable(f"GET /api/userdata: cần list, nhận {type(data).__name__}")
        items = []
        for item in data:
            entry = item if isinstance(item, dict) else {"path": item}
            path = str(entry.get("path") or "")
            if not path.endswith(".json") or not _clean_userdata_path(path):
                continue
            items.append({"path": path, "size": entry.get("size"), "modified": entry.get("modified")})
        return sorted(items, key=lambda i: -(i["modified"] or 0))

    def read_saved_workflow(self, path: str, max_bytes: int = WORKFLOW_MAX_BYTES) -> bytes:
        """Nội dung một workflow đã Save: ``GET /api/userdata/workflows%2F<path>``, có trần dung lượng."""
        if not path.endswith(".json") or not _clean_userdata_path(path):
            raise ComfyUIRejected(f"đường dẫn workflow lạ: {path!r}")
        response = self._get("/api/userdata/" + quote(f"workflows/{path}", safe=""), stream=True)
        chunks, size = [], 0
        try:
            for chunk in response.iter_content(64 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ComfyUIRejected(f"workflow {path} lớn hơn {max_bytes} byte")
                chunks.append(chunk)
        except requests.exceptions.RequestException as exc:
            raise ComfyUIUnavailable(f"GET /api/userdata {path}: {exc}") from None
        finally:
            response.close()
        return b"".join(chunks)

    def in_queue(self, prompt_id: str) -> bool:
        return self.queue_state(prompt_id) is not None

    def queue_state(self, prompt_id: str) -> str | None:
        """``running`` | ``pending`` | None theo vị trí của ``prompt_id`` trong ``/queue``.

        ``/queue`` trả ``queue_running``/``queue_pending`` là list ``[number, prompt_id, prompt, …]``.
        """
        data = self.queue()
        for key, state in (("queue_running", "running"), ("queue_pending", "pending")):
            for item in data.get(key) or []:
                if isinstance(item, list) and len(item) > 1 and item[1] == prompt_id:
                    return state
        return None

    def object_info(self) -> dict:
        """``GET /object_info``: mọi node (``class_type``) runtime này nạp được, kèm định nghĩa input."""
        data = self._json(self._get("/object_info"), "GET /object_info")
        if not data:
            raise ComfyUIUnavailable("/object_info rỗng: ComfyUI chưa nạp xong node")
        return data

    def upload_image(self, path: Path) -> str:
        """Upload ảnh tham chiếu bằng tên SHA-256; ghi đè cùng nội dung nên retry trước submit an toàn.

        Chỉ gửi PNG/JPEG/WebP, tối đa 64 MiB. Response phải trả đúng tên và thư mục do studio chọn.
        """
        from studio.comfyui.runs import sniff

        if path.stat().st_size > 64 * 1024 * 1024:
            raise ComfyUIRejected("ảnh tham chiếu lớn hơn 64 MiB")
        data = path.read_bytes()
        kind = sniff(data[:64])
        suffix, media = {"png": ("png", "image/png"), "jpeg": ("jpg", "image/jpeg"),
                         "webp": ("webp", "image/webp")}.get(kind, (None, None))
        if suffix is None:
            raise ComfyUIRejected("ảnh tham chiếu phải là PNG, JPEG hoặc WebP")
        # LoadImage.INPUT_TYPES liệt kê file ở input root, không liệt kê thư mục con.
        name, folder = f"igniterra_ref_{hashlib.sha256(data).hexdigest()}.{suffix}", ""
        try:
            response = requests.post(f"{self.base_url}/upload/image", files={"image": (name, data, media)},
                                     data={"type": "input", "subfolder": folder, "overwrite": "true"},
                                     headers=self._headers(), timeout=(self.connect_timeout, self.timeout),
                                     verify=self.verify, allow_redirects=False)
        except requests.exceptions.RequestException as exc:
            raise ComfyUIUnavailable(f"upload ảnh tham chiếu: {exc}") from None
        self._check(response, "POST /upload/image", unknown=False)
        result = self._json(response, "POST /upload/image")
        if result.get("name") != name or result.get("subfolder") != folder or result.get("type") != "input":
            raise ComfyUIRejected("upload trả đường dẫn khác tên SHA-256 và thư mục đã chọn")
        return f"{folder}/{name}" if folder else name

    def view(self, filename: str, subfolder: str = "", folder_type: str = "output",
             max_bytes: int = VIEW_MAX_BYTES) -> bytes:
        """``GET /view`` vào bộ nhớ, có trần dung lượng. Tên file và subfolder phải sạch (không ``..``, ``/`` đầu)."""
        check_output_ref(filename, subfolder, folder_type)
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

    def download(self, filename: str, subfolder: str, folder_type: str, dest: Path,
                 max_bytes: int = DOWNLOAD_MAX_BYTES) -> Download:
        """``GET /view`` stream ra ``dest``: ghi file tạm cùng thư mục, đủ byte theo ``Content-Length`` mới đổi tên.

        ``dest`` do người gọi kiểm (realpath/commonpath trong thư mục run). Mất kết nối giữa chừng hoặc thiếu byte là
        ``ComfyUIUnavailable`` (tải lại được); quá trần là ``ComfyUIRejected``. Không bao giờ để lại file dở dang.
        """
        check_output_ref(filename, subfolder, folder_type)
        response = self._get("/view", {"filename": filename, "subfolder": subfolder, "type": folder_type}, stream=True)
        expected = response.headers.get("content-length")
        temp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
        digest, size, head = hashlib.sha256(), 0, b""
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ComfyUIRejected(f"output {filename} lớn hơn {max_bytes} byte")
                    if len(head) < 64:
                        head += chunk[:64 - len(head)]
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if expected is not None and expected.isdigit() and int(expected) != size:
                raise ComfyUIUnavailable(f"GET /view {filename}: nhận {size} byte, Content-Length {expected}")
            os.replace(temp, dest)
        except requests.exceptions.RequestException as exc:
            raise ComfyUIUnavailable(f"GET /view {filename}: {exc}") from None
        finally:
            response.close()
            if temp.exists():
                temp.unlink()
        return Download(dest, size, digest.hexdigest(), response.headers.get("content-type", ""), head)
