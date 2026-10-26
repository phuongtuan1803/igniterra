"""Kiểm output của một run trước khi coi là ``succeeded`` (09 · s6 "Output", G11).

- Đường dẫn đích: ``data/comfyui/runs/<run_id>/<subfolder>/<filename>``, kiểm bằng ``studio.paths.contained``
  (realpath + commonpath, cùng hàm ``safe_path`` của web dùng), không so tiền tố chuỗi.
- Loại media: đọc magic bytes của file đã tải, phải khớp phần mở rộng; ``Content-Type`` server báo (nếu cụ thể)
  phải cùng nhóm (image/video/audio).
- SHA-256: tính khi stream, rồi đọc lại file đã ghi trên đĩa và so; lệch là hỏng.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from studio.comfyui.runtime import ComfyUIRejected, check_output_ref
from studio.paths import contained

RUN_ID = re.compile(r"cr_[0-9a-f]{12}")
# Phần mở rộng → (loại magic chấp nhận, media type ghi lại).
MEDIA = {
    ".png": (("png",), "image/png"),
    ".jpg": (("jpeg",), "image/jpeg"),
    ".jpeg": (("jpeg",), "image/jpeg"),
    ".webp": (("webp",), "image/webp"),
    ".gif": (("gif",), "image/gif"),
    ".mp4": (("isobmff",), "video/mp4"),
    ".mov": (("isobmff",), "video/quicktime"),
    ".webm": (("ebml",), "video/webm"),
    ".mkv": (("ebml",), "video/x-matroska"),
    ".wav": (("wav",), "audio/wav"),
    ".flac": (("flac",), "audio/flac"),
    ".mp3": (("mp3",), "audio/mpeg"),
    ".ogg": (("ogg",), "audio/ogg"),
}


class OutputInvalid(Exception):
    """Output tải về không đạt kiểm (loại media, SHA-256, đường dẫn): không tự tải lại."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def sniff(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[4:8] == b"ftyp":
        return "isobmff"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "ebml"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"ID3") or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "mp3"
    return None


def media_type(filename: str, head: bytes, content_type: str = "") -> str:
    """Media type đã kiểm của output; ``OutputInvalid`` khi phần mở rộng, magic bytes và header không khớp."""
    suffix = Path(filename).suffix.lower()
    if suffix not in MEDIA:
        raise OutputInvalid("media_unsupported", f"{filename}: phần mở rộng {suffix or '(không có)'} không nhận")
    kinds, expected = MEDIA[suffix]
    found = sniff(head)
    if found not in kinds:
        raise OutputInvalid("media_mismatch", f"{filename}: nội dung là {found or 'không rõ'}, không phải {expected}")
    declared = content_type.split(";")[0].strip().lower()
    if declared and declared != "application/octet-stream" and declared.split("/")[0] != expected.split("/")[0]:
        raise OutputInvalid("media_mismatch", f"{filename}: server báo {declared}, nội dung là {expected}")
    return expected


def output_target(run_dir: Path, subfolder: str, filename: str, folder_type: str) -> Path:
    """Đường dẫn ghi output, nằm trong ``run_dir`` sau realpath; tạo thư mục cha."""
    try:
        check_output_ref(filename, subfolder, folder_type)
    except ComfyUIRejected as exc:
        raise OutputInvalid("unsafe_path", str(exc)) from None
    run_dir.mkdir(parents=True, exist_ok=True)
    relative = f"{subfolder}/{filename}" if subfolder else filename
    target = contained(run_dir, relative)
    if target is None or target == Path(run_dir).resolve() or target.is_dir():
        raise OutputInvalid("unsafe_path", f"{relative!r} ra ngoài thư mục run")
    target.parent.mkdir(parents=True, exist_ok=True)
    if contained(run_dir, relative) != target:          # thư mục cha vừa tạo không được là symlink ra ngoài
        raise OutputInvalid("unsafe_path", f"{relative!r} ra ngoài thư mục run")
    return target


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
