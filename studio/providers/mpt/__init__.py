"""Adapter MPT. Chỉ thư mục này được import ``app.*`` (NFR-01).

MPT đọc ``MPT_HOME/config.toml`` một lần lúc import; ``ensure_home`` phải chạy trước import đầu tiên.
"""

from __future__ import annotations

import os
from pathlib import Path


def ensure_home(config: dict) -> Path:
    home = Path(os.environ.get("MPT_HOME") or config.get("mpt_home") or "~/.config/studio/mpt").expanduser()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["MPT_HOME"] = str(home)
    return home
