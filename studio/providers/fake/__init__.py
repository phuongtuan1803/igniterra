"""Bản giả cho mọi interface (NFR-04): test core không tốn tiền.

Hành vi điều khiển qua ``SCRIPT``: test đặt hàm hoặc giá trị theo khóa rồi ``reset()`` sau khi xong.
File media được tạo bằng FFmpeg hệ thống để renderer và hậu điều kiện chạy như thật.
"""

from __future__ import annotations

import hashlib
import math
import os
import subprocess
from pathlib import Path

from studio.contracts import AssetCandidate, AudioTrack, VoiceSpec
from studio.providers.base import Fatal

SCRIPT: dict = {}
CALLS: dict[str, list] = {}


def reset() -> None:
    SCRIPT.clear()
    CALLS.clear()


def _record(name: str, value) -> int:
    CALLS.setdefault(name, []).append(value)
    count = len(CALLS[name])
    # Giả lập worker chết giữa lượt gọi (T05, T06): STUDIO_FAKE_EXIT="image:3" thoát cứng ở lượt image thứ 3.
    target = os.environ.get("STUDIO_FAKE_EXIT", "")
    if target and target == f"{name}:{count}":
        os._exit(9)
    return count


def _hook(name: str, *args) -> None:
    hook = SCRIPT.get(name)
    if callable(hook):
        hook(len(CALLS.get(name, [])), *args)


def _ffmpeg(args: list[str]) -> None:
    result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise Fatal(f"fake ffmpeg: {result.stderr[-500:]}")


class FakeLLM:
    name, model, paid = "fake", "fake-llm", True

    def __init__(self, config: dict):
        self.config = config

    def generate_json(self, prompt: str, schema: dict, name: str) -> dict:
        _record("llm", prompt)
        _hook("llm", prompt)
        plan = SCRIPT.get("plan")
        if callable(plan):
            return plan(len(CALLS["llm"]), prompt, schema)
        if plan is not None:
            return plan
        raise Fatal("FakeLLM cần SCRIPT['plan']")


class FakeImage:
    name, model, paid = "fake", "fake-image", True

    def __init__(self, config: dict):
        self.config = config

    def generate(self, prompt: str, refs: list[Path], size: str, out: Path) -> AssetCandidate:
        _record("image", prompt)
        _hook("image", prompt)
        width, height = SCRIPT.get("image_size", (1080, 1920))
        color = "0x" + hashlib.sha256(prompt.encode()).hexdigest()[:6]
        path = out.with_suffix(".png")
        _ffmpeg(["-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}", "-frames:v", "1", str(path)])
        return AssetCandidate(id="tmp", kind="image", path=str(path), provider=self.name, model=self.model)


class FakeTTS:
    name, model, paid = "fake", "fake-tts", False

    def __init__(self, config: dict):
        self.config = config

    def synthesize(self, seg_id: str, text: str, voice: VoiceSpec, out: Path) -> AudioTrack:
        _record("tts", (seg_id, text))
        _hook("tts", seg_id, text)
        words = text.split()
        per_word = SCRIPT.get("seconds_per_word", 1 / voice.wps)
        duration = max(0.5, len(words) * per_word)
        _ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=330:duration={duration:.3f}", "-c:a", "libmp3lame", str(out)])
        timings = [{"start": round(i * per_word, 3), "end": round((i + 1) * per_word, 3), "text": w.strip(".,!?;:")}
                   for i, w in enumerate(words)]
        return AudioTrack(path=str(out), words=[] if SCRIPT.get("no_words") else timings,
                          provider=f"{self.name}:{voice.name}", model=self.model)


class FakeAltTTS(FakeTTS):
    """TTS giả thứ hai để kiểm đổi adapter chỉ bằng cấu hình (T07)."""

    name, model = "fake_alt", "fake-alt-tts"


class FakeAligner:
    name = "fake"

    def __init__(self, config: dict):
        self.config = config

    def align(self, audio: Path, script: str) -> list[dict]:
        _record("align", str(audio))
        words = script.split()
        span = 0.3
        return [{"start": i * span, "end": (i + 1) * span, "text": w} for i, w in enumerate(words)]


class FakeVideo:
    name, model, paid = "fake", "fake-video", True

    def __init__(self, config: dict):
        self.config = config

    def submit(self, image: Path, prompt: str, seconds: int) -> str:
        number = _record("video_submit", (str(image), seconds))
        _hook("video_submit", prompt)
        return f"job-{number}"

    def poll(self, job_id: str, out: Path) -> AssetCandidate | None:
        count = _record("video_poll", job_id)
        _hook("video_poll", job_id)
        if count < SCRIPT.get("video_polls_before_ready", 2):
            return None
        seconds = math.ceil(SCRIPT.get("video_seconds", 12))
        _ffmpeg(["-f", "lavfi", "-i", f"testsrc2=s=1080x1920:r=30:d={seconds}", "-pix_fmt", "yuv420p", str(out)])
        return AssetCandidate(id="tmp", kind="clip", path=str(out), provider=self.name, model=self.model)
