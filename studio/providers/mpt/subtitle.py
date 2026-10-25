"""MptSubtitleAligner: faster-whisper của MPT (``subtitle.create`` rồi ``subtitle.correct``).

Chỉ dùng khi TTS không trả mốc từ. Trả cue theo đoạn whisper; bước subtitles gom lại theo câu.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from studio.providers.base import Retryable
from studio.providers.mpt import ensure_home

_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _seconds(value: str) -> float:
    h, m, s, ms = _TIME.match(value.strip()).groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt(text: str) -> list[dict]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.strip().splitlines()
        if len(lines) >= 3 and "-->" in lines[1]:
            start, end = lines[1].split("-->")
            cues.append({"start": _seconds(start), "end": _seconds(end), "text": " ".join(lines[2:])})
    return cues


class MptSubtitleAligner:
    name = "mpt"

    def __init__(self, config: dict):
        ensure_home(config)

    def align(self, audio: Path, script: str) -> list[dict]:
        from app.services import subtitle as mpt_subtitle

        with tempfile.TemporaryDirectory() as folder:
            srt = Path(folder) / "aligned.srt"
            mpt_subtitle.create(audio_file=str(audio), subtitle_file=str(srt))
            if not srt.is_file():
                raise Retryable("faster-whisper không tạo được phụ đề")
            mpt_subtitle.correct(str(srt), script)
            return parse_srt(srt.read_text(encoding="utf-8"))
