"""Chấm một lượt tự kiểm bằng model vision qua OmniRoute (10 · Q-UX-02; màn S18).

Bảng tiêu chí cố định, trả JSON: mặt, tay, giống nhân vật (khi có ảnh tham chiếu), lỗi hình. Igniterra tự tính
``passed`` từ các trường đó, không tin một cờ "đạt" do model tự đặt. Người dùng vẫn chọn kết quả cuối.

Model ở ``[comfyui.judge] model`` (studio.toml): để trống là chưa bật chấm tự động, form chỉ cho tự duyệt. Mỗi
lượt chấm là một lượt gọi trả phí; không tự retry, lỗi thì chuyển nhóm sang tự duyệt.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

RUBRIC = (
    "Bạn là người kiểm ảnh cho video ngắn. Chấm ảnh đầu tiên theo bảng tiêu chí cố định, trả JSON đúng schema. "
    "face_ok: mặt người hoặc nhân vật không méo, mắt và miệng bình thường (không có mặt thì true). "
    "hands_ok: bàn tay đủ năm ngón, không dính, không thừa tay (không thấy tay thì true). "
    "character_match: nếu có ảnh thứ hai là ảnh tham chiếu nhân vật, nhân vật trong ảnh đầu có giống không; "
    "không có ảnh tham chiếu thì null. artifacts: danh sách lỗi hình khác (chữ vô nghĩa, vật thể dính nhau, "
    "chi tiết thừa), rỗng nếu không có. summary: một câu tiếng Việt nói lỗi chính hoặc 'Đạt'."
)
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["face_ok", "hands_ok", "character_match", "artifacts", "summary"],
    "properties": {
        "face_ok": {"type": "boolean"},
        "hands_ok": {"type": "boolean"},
        "character_match": {"type": ["boolean", "null"]},
        "artifacts": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
}
MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class JudgeError(Exception):
    """Không chấm được (thiếu cấu hình, lỗi gọi, JSON sai): nhóm chuyển sang tự duyệt, không chạy lại."""


def passed(verdict: dict) -> bool:
    return (verdict.get("face_ok") is True and verdict.get("hands_ok") is True
            and verdict.get("character_match") is not False and not verdict.get("artifacts"))


def normalize(raw: dict, *, by: str, model: str | None = None) -> dict:
    try:
        verdict = {"face_ok": bool(raw["face_ok"]), "hands_ok": bool(raw["hands_ok"]),
                   "character_match": None if raw.get("character_match") is None else bool(raw["character_match"]),
                   "artifacts": [str(a)[:200] for a in raw.get("artifacts") or []][:10],
                   "summary": str(raw.get("summary") or "")[:300]}
    except (KeyError, TypeError) as exc:
        raise JudgeError(f"kết quả chấm thiếu trường {exc}") from None
    verdict["passed"] = passed(verdict)
    verdict["by"] = by
    if model:
        verdict["model"] = model
    return verdict


def manual(ok: bool, note: str = "") -> dict:
    """Kết quả người dùng tự duyệt một lượt."""
    return {"passed": bool(ok), "by": "manual", "summary": (note.strip() or ("Đạt" if ok else "Không đạt"))[:300]}


def _data_url(path: Path) -> str:
    media = MEDIA.get(path.suffix.lower())
    if media is None:
        raise JudgeError(f"{path.name}: chỉ chấm được ảnh PNG, JPEG, WebP")
    return f"data:{media};base64,{base64.b64encode(path.read_bytes()).decode()}"


class OmniRouteJudge:
    """Gọi ``/chat/completions`` của OmniRoute với ảnh output (và ảnh tham chiếu nếu có)."""

    def __init__(self, config: dict):
        section = (config.get("comfyui") or {}).get("judge") or {}
        self.model = str(section.get("model") or "")
        self.cost_usd = float(section.get("cost_usd", 0.002))
        self.config = config

    @property
    def enabled(self) -> bool:
        return bool(self.model) and "omniroute" in self.config

    def __call__(self, image: Path, reference: Path | None = None) -> dict:
        if not self.enabled:
            raise JudgeError("chưa chọn model vision ([comfyui.judge] model trong studio.toml)")
        from studio.providers.base import ProviderError
        from studio.providers.direct.omniroute import _Client, _same_model

        content = [{"type": "text", "text": RUBRIC}, {"type": "image_url", "image_url": {"url": _data_url(image)}}]
        if reference is not None:
            content.append({"type": "image_url", "image_url": {"url": _data_url(reference)}})
        try:
            data = _Client(self.config).post("/chat/completions", json={
                "model": self.model, "messages": [{"role": "user", "content": content}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "verdict", "strict": True,
                                                                            "schema": SCHEMA}}})
        except ProviderError as exc:
            raise JudgeError(f"gọi model chấm lỗi: {exc}") from None
        if not _same_model(self.model, data.get("model")):
            raise JudgeError(f"gateway trả model {data.get('model')!r}, đã gọi {self.model!r} (fallback?)")
        try:
            raw = json.loads(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError):
            raise JudgeError("model chấm không trả JSON hợp lệ") from None
        return normalize(raw, by="vision", model=self.model)
