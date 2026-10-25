"""Ghép prompt xác định (04 · Series Bible và ghép prompt). Không gọi LLM.

Đổi nội dung mẫu thì tăng *_PROMPT_VERSION để input_hash đổi và bước liên quan chạy lại.
"""

from __future__ import annotations

import json

from studio.contracts import Brief, Scene, ScenePlan, SeriesBible

PLAN_PROMPT_VERSION = "plan-v1"
KEYFRAME_PROMPT_VERSION = "kf-v1"


def word_count(text: str) -> int:
    return len(text.split())


def plan_schema(brief: Brief, bible: SeriesBible) -> dict:
    """JSON schema cho structured output: shot là enum từ Series Bible, shows là enum từ must_show."""
    camera = bible.camera
    shows = [item.what for item in brief.must_show]
    scene = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "narration", "visual", "shot", "characters", "shows"],
        "properties": {
            "id": {"type": "string", "pattern": "^s[0-9]{2}$"},
            "narration": {"type": "string"},
            "visual": {"type": "string"},
            "shot": {
                "type": "object",
                "additionalProperties": False,
                "required": ["size", "angle", "movement"],
                "properties": {key: {"type": "string", "enum": sorted(camera[key])} for key in ("size", "angle", "movement")},
            },
            "characters": {"type": "array", "items": {"type": "string"}},
            "shows": {"type": "array", "items": {"type": "string", "enum": shows} if shows else {"type": "string"}},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "cast", "scenes"],
        "properties": {
            "title": {"type": "string"},
            "cast": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "description"],
                    "properties": {"key": {"type": "string"}, "description": {"type": "string"}},
                },
            },
            "scenes": {"type": "array", "minItems": brief.scenes[0], "maxItems": brief.scenes[1], "items": scene},
        },
    }


def plan_instructions(brief: Brief, bible: SeriesBible, feedback: list[str]) -> str:
    voice = bible.narrator
    lo, hi = brief.target_s
    words_lo, words_hi = round(lo * voice.wps), round(hi * voice.wps)
    words_mid = round((lo + hi) / 2 * voice.wps)
    must_show = "\n".join(
        f"- \"{item.what}\"" + (f" (chỉ được xuất hiện từ scene {item.not_before} trở đi)" if item.not_before else "")
        for item in brief.must_show
    ) or "- (không có)"
    known = "\n".join(f"- {key}: {c.description}" for key, c in bible.characters.items()) or "- (series chưa có nhân vật; tự mô tả trong cast)"
    lines = [
        "Bạn viết kịch bản cho một video Short dọc 9:16 bằng tiếng Việt, kể một truyện khoa học viễn tưởng bí ẩn hư cấu.",
        "Trả về JSON đúng schema. Mỗi scene là một nhịp hình (một khung hình tĩnh có chuyển động camera nhẹ).",
        "",
        f"Tiêu đề: {brief.title}",
        f"Hook (phải nằm trong câu đầu tiên của scene đầu, nói ra trong 3 giây đầu): {brief.hook}",
        "Nhân vật: " + "; ".join(brief.characters),
        "Diễn biến theo thứ tự: " + " → ".join(brief.beats),
        f"Twist (lộ ra ở scene cuối, không hé lộ sớm hơn): {brief.twist}",
        "Hình bắt buộc (gắn vào trường shows của scene thể hiện nó; dùng đúng chuỗi):",
        must_show,
        "Điều cấm: " + "; ".join(brief.must_not or ["(không có)"]),
    ]
    if brief.notes:
        lines.append(f"Ghi chú: {brief.notes}")
    lines += [
        "",
        "Ràng buộc:",
        f"- Số scene: {brief.scenes[0]} đến {brief.scenes[1]}; id s01, s02, ... liên tiếp.",
        f"- Tổng lời đọc (narration của mọi scene) {words_lo}–{words_hi} từ tiếng Việt tách bằng khoảng trắng, nhắm khoảng {words_mid} từ"
        f" (giọng đọc {voice.wps} từ/giây, mục tiêu {lo:g}–{hi:g} giây, trần cứng {brief.max_s:g} giây). Đếm từ cẩn thận.",
        "- narration: câu văn nói tự nhiên, có dấu, dùng dấu câu (. , ? !) để ngắt; không ký hiệu, không chữ số, không tên tiếng Anh.",
        "- visual: tiếng Anh, mô tả một khung hình cụ thể (bối cảnh, ánh sáng, chủ thể, hành động), 1–2 câu;"
        " không mô tả phong cách chung, không yêu cầu chữ trong ảnh; nhân vật chỉ gọi bằng key trong cast.",
        "- Ưu tiên bối cảnh, đồ vật, bóng người hoặc nhân vật ở xa / quay lưng; tránh cận mặt.",
        "- Hình của scene không được làm lộ twist hoặc chi tiết của nhịp sau sớm hơn lời kể.",
        "- characters: danh sách key nhân vật xuất hiện trong khung hình của scene (rỗng nếu không có).",
        "- cast: mỗi nhân vật một key ngắn (chữ thường, không dấu) và mô tả ngoại hình cố định bằng tiếng Anh,"
        " đủ để vẽ nhất quán ở mọi scene (loại, dáng, trang phục, màu sắc). Nhân vật đã có trong series thì giữ key, bỏ khỏi cast.",
        "- shot: chọn khóa trong enum; movement khác nhau giữa các scene liền kề khi hợp lý.",
        "",
        "Nhân vật đã có trong series:",
        known,
    ]
    if feedback:
        lines += ["", "Lần trước bị từ chối vì:"] + [f"- {reason}" for reason in feedback]
    return "\n".join(lines)


def plan_from_llm(data: dict, brief: Brief, series: str | None) -> ScenePlan:
    cast = {item["key"]: item["description"] for item in data.get("cast", [])}
    return ScenePlan.model_validate({
        "revision": 1,
        "series": series,
        "title": data.get("title") or brief.title,
        "cast": cast,
        "scenes": [{**scene, "mode": "still"} for scene in data["scenes"]],
        "note": "plan",
    })


def check_plan(plan: ScenePlan, brief: Brief, bible: SeriesBible) -> list[str]:
    """Kiểm ScenePlan với Brief (04 · Kiểm tra plan). Trả danh sách lý do trượt."""
    problems: list[str] = []
    count = len(plan.scenes)
    if not brief.scenes[0] <= count <= brief.scenes[1]:
        problems.append(f"có {count} scene, cần {brief.scenes[0]}–{brief.scenes[1]}")
    expected = [f"s{i:02d}" for i in range(1, count + 1)]
    if [s.id for s in plan.scenes] != expected:
        problems.append(f"id scene phải là {', '.join(expected)}")
    for key in ("size", "angle", "movement"):
        for scene in plan.scenes:
            if getattr(scene.shot, key) not in bible.camera[key]:
                problems.append(f"{scene.id}: shot.{key}={getattr(scene.shot, key)!r} không có trong từ vựng camera")
    for scene in plan.scenes:
        for key in scene.characters:
            if key not in bible.characters and key not in plan.cast:
                problems.append(f"{scene.id}: nhân vật {key!r} không có trong series hay cast")
    for item in brief.must_show:
        index = [i for i, scene in enumerate(plan.scenes, 1) if item.what in scene.shows]
        if not index:
            problems.append(f"hình bắt buộc \"{item.what}\" không gắn với scene nào")
        elif item.not_before and min(index) < item.not_before:
            problems.append(f"hình \"{item.what}\" xuất hiện ở scene {min(index)}, sớm hơn scene {item.not_before} được phép")
    words = sum(word_count(scene.narration) for scene in plan.scenes)
    estimate = words / bible.narrator.wps
    lo, hi = brief.target_s
    if not lo <= estimate <= hi:
        problems.append(
            f"lời đọc {words} từ ≈ {estimate:.1f} giây, ngoài khoảng {lo:g}–{hi:g} giây"
            f" (cần {round(lo * bible.narrator.wps)}–{round(hi * bible.narrator.wps)} từ)"
        )
    return problems


def keyframe_prompt(scene: Scene, plan: ScenePlan, bible: SeriesBible, note: str | None = None) -> str:
    """style + nhân vật + cụm camera + visual + negative, theo thứ tự cố định."""
    parts = [bible.style.rstrip(".") + "."]
    for key in scene.characters:
        description = bible.characters[key].description if key in bible.characters else plan.cast.get(key)
        if description:
            parts.append(f"{key.capitalize()}: {description.rstrip('.')}.")
    camera = bible.camera
    shot = ", ".join([camera["size"][scene.shot.size], camera["angle"][scene.shot.angle], camera["movement"][scene.shot.movement]])
    parts.append(shot + ".")
    parts.append(scene.visual.rstrip(".") + ".")
    if note:
        parts.append(f"Correction: {note.rstrip('.')}.")
    parts.append(bible.negative.rstrip(".") + ".")
    return "\n".join(parts)


def apply_lexicon(text: str, bible: SeriesBible) -> str:
    for word, spoken in bible.lexicon.items():
        text = text.replace(word, spoken)
    return text


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
