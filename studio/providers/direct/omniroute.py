"""OmniRouteLLM và OmniRouteImage qua gateway OmniRoute (API kiểu OpenAI) — 05 · Gate 2b qua OmniRoute.

Chỉ gọi model id đầy đủ (không combo/alias, nên không có fallback), không tự retry lượt gọi trả phí.
Khóa ở biến môi trường OMNIROUTE_API_KEY; base_url và model ở studio.toml [omniroute].
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import requests

from studio import costs
from studio.contracts import AssetCandidate
from studio.providers.base import ContentRefused, Fatal, QualityFail, Retryable, Unconfirmed

MEDIA_SUFFIX = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp"}


class _Client:
    def __init__(self, config: dict):
        section = config["omniroute"]
        self.base_url = section["base_url"].rstrip("/")
        self.timeout = float(section.get("timeout_s", 120))
        self.key = os.environ.get("OMNIROUTE_API_KEY", "")
        if not self.key:
            raise Fatal("thiếu OMNIROUTE_API_KEY (đặt trong .env)")

    def post(self, path: str, **kwargs) -> dict:
        try:
            response = requests.post(f"{self.base_url}{path}", headers={"Authorization": f"Bearer {self.key}"},
                                     timeout=(10, self.timeout), **kwargs)
        except requests.exceptions.ConnectTimeout as exc:
            raise Retryable(f"không kết nối được OmniRoute: {exc}") from None
        except requests.exceptions.ConnectionError as exc:
            # Lỗi ở bước mở kết nối thì chắc chắn chưa gửi; mất kết nối giữa chừng thì có thể đã tính phí.
            text = str(exc)
            if "Failed to establish a new connection" in text or "Connection refused" in text:
                raise Retryable(f"không kết nối được OmniRoute: {exc}") from None
            raise Unconfirmed(f"mất kết nối sau khi gửi: {exc}") from None
        except requests.exceptions.ReadTimeout:
            raise Unconfirmed(f"quá {self.timeout:g}s không có phản hồi sau khi gửi") from None
        except requests.exceptions.RequestException as exc:
            raise Unconfirmed(f"lỗi HTTP sau khi gửi: {exc}") from None
        body = response.text[:600]
        if response.status_code == 429:
            raise Retryable(f"429 từ chối trước khi xử lý: {body}")
        lowered = body.lower()
        if response.status_code == 400 and ("moderation" in lowered or "safety" in lowered):
            raise ContentRefused(f"provider chặn nội dung (moderation), cần diễn đạt lại visual: {body[:200]}")
        if response.status_code == 400 and "could not generate" in lowered:
            # Gemini trả 400 "could not generate an image (STOP)": không sinh gì, không phải lỗi cấu hình.
            raise ContentRefused(f"provider không sinh được ảnh, cần diễn đạt lại visual: {body[:200]}")
        if response.status_code in (400, 401, 403, 404, 422):
            raise Fatal(f"HTTP {response.status_code}: {body}")
        if response.status_code >= 500:
            raise Unconfirmed(f"HTTP {response.status_code} sau khi gửi: {body}")
        if response.status_code != 200:
            raise Unconfirmed(f"HTTP {response.status_code}: {body}")
        try:
            return response.json()
        except ValueError:
            raise Unconfirmed(f"response không phải JSON: {body}") from None


def _same_model(configured: str, returned: str | None) -> bool:
    return not returned or returned == configured or configured.endswith("/" + returned)


class OmniRouteLLM:
    name = "omniroute"
    paid = True

    def __init__(self, config: dict):
        self.client = _Client(config)
        self.model = config["omniroute"]["llm_model"]

    def generate_json(self, prompt: str, schema: dict, name: str) -> dict:
        data = self.client.post("/chat/completions", json={
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        })
        if not _same_model(self.model, data.get("model")):
            raise Fatal(f"gateway trả model {data.get('model')!r}, đã gọi {self.model!r} (fallback?)")
        usage = data.get("usage") or {}
        costs.report_usage(units={k: float(usage[k]) for k in ("prompt_tokens", "completion_tokens") if k in usage} or None,
                           model=data.get("model"))
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise Unconfirmed(f"response thiếu nội dung: {str(data)[:300]}") from None
        try:
            return json.loads(content)
        except (TypeError, ValueError):
            raise QualityFail(f"LLM không trả JSON hợp lệ: {str(content)[:200]}") from None


class OmniRouteImage:
    name = "omniroute"
    paid = True

    def __init__(self, config: dict):
        self.client = _Client(config)
        section = config["omniroute"]
        self.model = section["image_model"]
        self.max_refs = int(section.get("image_max_refs") or 1)
        self.est_usd = section.get("image_est_usd")

    def generate(self, prompt: str, refs: list[Path], size: str, out: Path) -> AssetCandidate:
        if refs:
            files = [("image[]", (Path(ref).name, Path(ref).read_bytes(), "image/png")) for ref in refs[: self.max_refs]]
            data = self.client.post("/images/edits", data={"model": self.model, "prompt": prompt, "n": "1", "size": size},
                                    files=files)
        else:
            data = self.client.post("/images/generations", json={"model": self.model, "prompt": prompt, "n": 1, "size": size})
        if not _same_model(self.model, data.get("model")):
            raise Fatal(f"gateway trả model {data.get('model')!r}, đã gọi {self.model!r}")
        costs.report_usage(units={"images": 1.0}, est_usd=self.est_usd, model=self.model)
        try:
            entry = data["data"][0]
        except (KeyError, IndexError, TypeError):
            raise Unconfirmed(f"response không có ảnh: {str(data)[:300]}") from None
        if entry.get("b64_json"):
            raw = base64.b64decode(entry["b64_json"])
        elif entry.get("url"):
            try:
                raw = requests.get(entry["url"], timeout=60).content
            except requests.RequestException as exc:
                raise Unconfirmed(f"không tải được ảnh đã sinh: {exc}") from None
        else:
            raise Unconfirmed(f"response không có ảnh: {str(entry)[:300]}")
        media_type = entry.get("media_type") or ("image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg")
        path = out.with_suffix(MEDIA_SUFFIX.get(media_type, ".png"))
        path.write_bytes(raw)
        return AssetCandidate(id="tmp", kind="image", path=str(path), provider=self.name, model=self.model,
                              ai_generated=True, source=f"omniroute:{self.model}", rights="AI-generated (điều khoản provider)")
