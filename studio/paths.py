"""Kiểm đường dẫn nằm trong một thư mục gốc (07 · Đường dẫn file): realpath + commonpath, không so tiền tố chuỗi.

Dùng chung cho ``studio.web.safe_path`` (đọc file phục vụ qua ``/files``) và output ComfyUI ghi vào
``data/comfyui/runs/<run_id>/`` (09 · s6).
"""

from __future__ import annotations

import os
from pathlib import Path


def contained(root: Path | str, relative: str) -> Path | None:
    """``root/relative`` sau realpath (theo cả symlink) nếu vẫn nằm trong realpath của ``root``; ngoài ra None."""
    base = os.path.realpath(root)
    target = os.path.realpath(os.path.join(base, relative))
    if os.path.commonpath([base, target]) != base:
        return None
    return Path(target)
