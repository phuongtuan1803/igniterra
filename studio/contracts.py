"""Contract Pydantic của core (04 · Contract và dữ liệu). Không có hành vi ngoài kiểm tra."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCENE_ID = re.compile(r"^s\d{2}$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- Brief


class MustShow(_Model):
    what: str
    not_before: int | None = Field(default=None, ge=1, description="Nhịp (scene) sớm nhất được xuất hiện")


class Brief(_Model):
    schema_version: Literal[1] = 1
    id: str
    title: str
    language: str = "vi-VN"
    target_s: tuple[float, float] = (45, 58)
    max_s: float = 60
    scenes: tuple[int, int] = (4, 6)
    hook: str
    characters: list[str] = []
    beats: list[str]
    twist: str
    must_show: list[MustShow] = []
    must_not: list[str] = []
    notes: str = ""
    music: bool = False

    @model_validator(mode="after")
    def _ranges(self) -> Brief:
        lo, hi = self.target_s
        if not 0 < lo <= hi <= self.max_s:
            raise ValueError("target_s phải nằm trong (0, max_s]")
        if not 1 <= self.scenes[0] <= self.scenes[1] <= 99:
            raise ValueError("scenes phải là khoảng hợp lệ")
        return self


# ---------------------------------------------------------------- Series Bible


class VoiceSpec(_Model):
    provider: str = "mpt"
    name: str = "vi-VN-NamMinhNeural"
    rate: float = 1.0
    wps: float = Field(default=2.9, gt=0, description="Từ/giây đo được với giọng này")


class Character(_Model):
    description: str
    refs: list[str] = []          # đường dẫn tương đối theo thư mục series
    sheet: str | None = None      # character sheet (1 ảnh) cho model chỉ nhận 1 ảnh tham chiếu


DEFAULT_CAMERA = {
    "size": {"wide": "wide shot", "medium": "medium shot", "close_up": "close-up"},
    "angle": {"eye_level": "eye-level", "low": "low angle", "high": "high angle", "top_down": "top-down view"},
    "movement": {
        "static": "static camera",
        "push_in": "slow push-in",
        "pull_out": "slow pull-out",
        "pan_left": "slow pan left",
        "pan_right": "slow pan right",
    },
}


class SeriesBible(_Model):
    schema_version: Literal[1] = 1
    name: str = "default"
    style: str = (
        "cinematic sci-fi film still, moody low-key lighting, muted teal and amber palette, "
        "35mm film grain, atmospheric haze, vertical 9:16 composition"
    )
    negative: str = "no text, no letters, no captions, no logo, no watermark, no extra fingers"
    characters: dict[str, Character] = {}
    voices: dict[str, VoiceSpec] = {"narrator": VoiceSpec()}
    lexicon: dict[str, str] = {}
    camera: dict[str, dict[str, str]] = DEFAULT_CAMERA

    @field_validator("camera")
    @classmethod
    def _camera_keys(cls, value: dict) -> dict:
        missing = {"size", "angle", "movement"} - set(value)
        if missing:
            raise ValueError(f"camera thiếu khóa: {sorted(missing)}")
        return value

    @property
    def narrator(self) -> VoiceSpec:
        return self.voices.get("narrator") or next(iter(self.voices.values()))


# ---------------------------------------------------------------- ScenePlan


class Shot(_Model):
    size: str
    angle: str
    movement: str


class Scene(_Model):
    id: str
    narration: str
    visual: str
    shot: Shot
    characters: list[str] = []
    shows: list[str] = []
    mode: Literal["still", "motion"] = "still"
    target_s: float = 0
    start_from: Literal["prev_last_frame"] | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not SCENE_ID.match(value):
            raise ValueError("scene id phải dạng s01..s99")
        return value


class ScenePlan(_Model):
    schema_version: Literal[1] = 1
    revision: int = 1
    parent: int | None = None
    series: str | None = None
    title: str
    cast: dict[str, str] = {}   # mô tả nhân vật viết một lần khi series không có mẫu nhân vật
    scenes: list[Scene]
    note: str = ""              # ai tạo revision và vì sao

    def scene(self, scene_id: str) -> Scene:
        for scene in self.scenes:
            if scene.id == scene_id:
                return scene
        raise KeyError(scene_id)


# ---------------------------------------------------------------- Ứng viên, audio, chi phí


class AssetCandidate(_Model):
    id: str
    kind: Literal["image", "voice", "clip", "music"]
    path: str                   # tương đối theo thư mục task
    provider: str
    model: str | None = None
    plan_rev: int | None = None
    ai_generated: bool = True
    source: str | None = None
    rights: str | None = None
    sha256: str | None = None
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    from_ids: list[str] = []    # ứng viên đầu vào (ảnh nguồn, giọng) của clip
    words: list[WordTiming] = []
    created_at: str | None = None


class WordTiming(_Model):
    start: float
    end: float
    text: str


class AudioTrack(_Model):
    path: str
    words: list[WordTiming] = []
    provider: str
    model: str | None = None
    duration_s: float | None = None


class CostEvent(_Model):
    ts: str
    request_id: str
    status: Literal["pending", "ok", "failed", "unknown"]
    provider: str | None = None
    model: str | None = None
    units: dict[str, float] | None = None
    est_usd: float | None = None
    note: str | None = None


# ---------------------------------------------------------------- Phiếu duyệt

CHECKLIST = ("C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09")


class CheckItem(_Model):
    result: Literal["pass", "fail", "unknown"] = "unknown"
    timestamp: str | None = None   # mốc trong video, ví dụ "00:12"
    note: str = ""


class Review(_Model):
    schema_version: Literal[1] = 1
    checklist: dict[str, CheckItem]
    active_minutes: float | None = Field(default=None, ge=0)
    reviewer: str
    note: str = ""

    @field_validator("checklist")
    @classmethod
    def _all_items(cls, value: dict) -> dict:
        unknown = set(value) - set(CHECKLIST)
        if unknown:
            raise ValueError(f"tiêu chí lạ: {sorted(unknown)}")
        return {key: value.get(key, CheckItem()) for key in CHECKLIST}

    @property
    def accepted(self) -> bool:
        return all(item.result == "pass" for item in self.checklist.values())


AssetCandidate.model_rebuild()
