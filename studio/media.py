"""FFmpeg/ffprobe hệ thống: đo file và kiểm hậu điều kiện (03 · Hậu điều kiện).

Mọi lệnh gọi bằng danh sách đối số, không qua shell.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from studio.providers.base import Fatal, QualityFail

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def check_tools() -> dict[str, str]:
    versions = {}
    for tool in (FFMPEG, FFPROBE):
        if shutil.which(tool) is None:
            raise Fatal(f"thiếu {tool} hệ thống (bản imageio-ffmpeg của MPT không có ffprobe)")
        out = subprocess.run([tool, "-version"], capture_output=True, text=True, timeout=10).stdout
        versions[tool] = out.splitlines()[0] if out else "?"
    return versions


def run(args: list[str], timeout: float = 900, cwd: Path | None = None) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if result.returncode:
        raise QualityFail(f"{Path(args[0]).name} lỗi: {result.stderr[-1500:]}")
    return result.stdout


def probe(path: Path) -> dict:
    out = run([FFPROBE, "-v", "error", "-show_entries",
               "format=duration:stream=codec_type,codec_name,width,height,duration",
               "-of", "json", str(path)], timeout=60)
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0)
    return {
        "duration": duration,
        "width": int(video["width"]) if video else None,
        "height": int(video["height"]) if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "has_audio": audio is not None,
        "video_duration": float(video["duration"]) if video and video.get("duration") else None,
    }


def decode_ok(path: Path, timeout: float = 900) -> None:
    """Giải mã toàn bộ file; lỗi giải mã là QualityFail."""
    result = subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode or result.stderr.strip():
        raise QualityFail(f"giải mã lỗi: {result.stderr[-500:]}")


def image_size(path: Path) -> tuple[int, int]:
    info = probe(path)
    if not info["width"]:
        raise QualityFail("không đọc được ảnh")
    return info["width"], info["height"]


def check_keyframe(path: Path, min_short: int = 1080, ratio: float = 9 / 16, tolerance: float = 0.04) -> tuple[int, int]:
    width, height = image_size(path)
    if min(width, height) < min_short:
        raise QualityFail(f"ảnh {width}x{height}: cạnh ngắn < {min_short}px")
    if abs(width / height - ratio) / ratio > tolerance:
        raise QualityFail(f"ảnh {width}x{height}: tỉ lệ lệch 9:16 quá {tolerance:.0%}")
    return width, height


def check_audio(path: Path) -> float:
    info = probe(path)
    if not info["has_audio"] or info["duration"] <= 0:
        raise QualityFail("file giọng không có audio hoặc thời lượng 0")
    return info["duration"]


def check_clip(path: Path, width: int, height: int, min_duration: float) -> dict:
    decode_ok(path)
    info = probe(path)
    if (info["width"], info["height"]) != (width, height):
        raise QualityFail(f"clip {info['width']}x{info['height']}, cần {width}x{height}")
    if info["duration"] + 1e-3 < min_duration:
        raise QualityFail(f"clip {info['duration']:.2f}s ngắn hơn giọng {min_duration:.2f}s")
    return info


def check_render(path: Path, width: int, height: int) -> dict:
    decode_ok(path)
    info = probe(path)
    if (info["width"], info["height"]) != (width, height):
        raise QualityFail(f"render {info['width']}x{info['height']}, cần {width}x{height}")
    if not info["has_audio"]:
        raise QualityFail("render không có audio")
    return info
