"""Bước subtitles: mốc từng từ của TTS (hoặc SubtitleAligner) → cue theo câu, tương đối với scene.

Mốc tuyệt đối chỉ chốt ở render, từ thời lượng scene mà renderer đo (07 · runbook lệch phụ đề).
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path

from studio import prompt
from studio.steps.base import Ctx, Item, Result
from studio.taskdir import commit_file

SUBTITLE_VERSION = "sub-v1"
MAX_CHARS = 42          # một cue tối đa ~2 dòng ở cỡ chữ mặc định
WRAP_CHARS = 24


def applies(ctx: Ctx, scene: str) -> bool:
    return True


def _norm(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFC", text).lower() if ch.isalnum())


def split_lines(text: str) -> list[str]:
    """Tách theo dấu câu, rồi chia đều câu dài theo ranh giới từ (mỗi đoạn không quá MAX_CHARS)."""
    lines: list[str] = []
    for part in re.split(r"(?<=[.,!?;:…])\s+", text.strip()):
        part = part.strip().rstrip(".,;:")
        if not _norm(part):
            continue
        words = part.split()
        chunks = max(1, math.ceil(len(part) / MAX_CHARS))
        while True:
            target = len(part) / chunks
            pieces, current = [], []
            for word in words:
                if current and len(" ".join(current)) >= target and len(pieces) < chunks - 1:
                    pieces.append(" ".join(current))
                    current = []
                current.append(word)
            pieces.append(" ".join(current))
            if all(len(piece) <= MAX_CHARS for piece in pieces) or chunks >= len(words):
                break
            chunks += 1
        lines.extend(pieces)
    return lines


def group_cues(text: str, words: list[dict]) -> list[dict]:
    """Gom mốc từ thành cue theo dòng phụ đề bằng cách khớp chữ đã chuẩn hóa."""
    lines = split_lines(text)
    cues: list[dict] = []
    index = 0
    for line in lines:
        target = len(_norm(line))
        taken: list[dict] = []
        consumed = 0
        while index < len(words) and consumed < target:
            consumed += len(_norm(words[index]["text"]))
            taken.append(words[index])
            index += 1
        if not taken:
            break
        cues.append({"start": round(taken[0]["start"], 3), "end": round(taken[-1]["end"], 3), "text": line})
    if len(cues) != len(lines):
        raise ValueError(f"không khớp mốc từ với lời đọc ({len(cues)}/{len(lines)} dòng)")
    return cues


def inputs(ctx: Ctx, scene: str) -> dict:
    voices = {}
    for scene_id in ctx.scene_ids():
        selected = ctx.td.selected(scene_id, "voice")
        voices[scene_id] = [selected.id, selected.sha256] if selected else None
    return {"voices": voices, "aligner": ctx.adapter_id("subtitles"), "version": SUBTITLE_VERSION}


def run(ctx: Ctx, item: Item) -> Result:
    result: dict[str, dict] = {}
    for scene_id in ctx.scene_ids():
        voice = ctx.td.selected(scene_id, "voice")
        text = prompt.apply_lexicon(ctx.plan.scene(scene_id).narration, ctx.bible)
        words = [w.model_dump() for w in voice.words]
        source = "tts"
        if not words:
            words = ctx.adapter("subtitles").align(ctx.td.abs(voice.path), text)
            source = "aligner"
        # Phụ đề hiển thị narration gốc (trước lexicon) nhưng thời gian theo lời đọc thật.
        cues = group_cues(text, words)
        shown = split_lines(ctx.plan.scene(scene_id).narration)
        if len(shown) == len(cues):
            for cue, line in zip(cues, shown):
                cue["text"] = line
        result[scene_id] = {"voice": voice.id, "duration_s": voice.duration_s, "source": source, "cues": cues}
    tmp = ctx.td.tmp_path("subtitles", ".json")
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    final = ctx.td.root / "subtitles" / f"cues_a{item.attempt}.json"
    commit_file(tmp, final)
    return Result(output=final)


# ---------------------------------------------------------------- ASS


def _ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, rest = divmod(centis, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _wrap(text: str) -> str:
    if len(text) <= WRAP_CHARS:
        return text
    words = text.split()
    best, best_gap = 1, None
    for i in range(1, len(words)):
        gap = abs(len(" ".join(words[:i])) - len(" ".join(words[i:])))
        if best_gap is None or gap < best_gap:
            best, best_gap = i, gap
    return " ".join(words[:best]) + r"\N" + " ".join(words[best:])


def build_ass(cues_by_scene: dict, offsets: dict[str, float], render: dict) -> str:
    width, height = render["width"], render["height"]
    size = render.get("font_size", 66)
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{render.get('font', 'Be Vietnam Pro')},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,4,1,2,90,90,{round(height * 0.24)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for scene_id, entry in cues_by_scene.items():
        offset = offsets[scene_id]
        for cue in entry["cues"]:
            text = _wrap(cue["text"].replace("{", "(").replace("}", ")"))
            events.append(f"Dialogue: 0,{_ass_time(offset + cue['start'])},{_ass_time(offset + cue['end'])},Default,,0,0,0,,{text}")
    return header + "\n".join(events) + "\n"


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
