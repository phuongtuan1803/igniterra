"""Cấu hình hiệu lực: mặc định trong code < studio.toml < tasks.overrides (04 · Cấu hình).

studio.toml không chứa khóa; khóa đọc từ biến môi trường (nạp thêm từ .env nếu có).
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "data_dir": "data",
    "mpt_home": "~/.config/studio/mpt",
    "adapters": {
        "llm": "omniroute",
        "image": "omniroute",
        "tts": "mpt",
        "subtitles": "mpt",
        "video": "none",
        "renderer": "video_project",
    },
    "omniroute": {
        "base_url": "http://localhost:20128/v1",
        "llm_model": "codex/gpt-5.6-luna",
        "image_model": "openrouter/google/gemini-3.1-flash-image-preview",
        "image_max_refs": 1,
        "image_est_usd": None,
        "timeout_s": 120,
    },
    "concurrency": {"image": 4, "tts": 2, "video": 2, "animate": 2},
    "retry": {"max_attempts": 3, "backoff_s": 2.0},
    "gates": {"keyframes": True},
    "render": {"width": 1080, "height": 1920, "fps": 30, "font": "Be Vietnam Pro", "font_size": 66},
    "worker": {"poll_s": 2.0},
    "gpu": {"provider": "none", "image": "", "quote_ttl_s": 300},
}

SECRET_ENV = ("OMNIROUTE_API_KEY", "STUDIO_BASIC_AUTH")
SETTINGS_FILE = "settings.toml"


def deep_merge(base: dict, extra: dict | None) -> dict:
    result = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_dotenv(path: Path) -> None:
    """Nạp .env đơn giản (KEY=VALUE); không ghi đè biến đã có."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load(root: Path | None = None, path: Path | None = None, data_dir: str | None = None) -> dict:
    """mặc định < studio.toml < data/settings.toml (trang Settings ghi) < tasks.overrides (lúc chạy task)."""
    root = Path(root or os.environ.get("STUDIO_ROOT") or Path.cwd())
    load_dotenv(root / ".env")
    path = Path(path or os.environ.get("STUDIO_CONFIG") or root / "studio.toml")
    file_config = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    config = deep_merge(DEFAULTS, file_config)
    folder = Path(data_dir or config["data_dir"]).expanduser()
    folder = folder if folder.is_absolute() else (root / folder).resolve()
    settings = folder / SETTINGS_FILE
    if settings.is_file():
        config = deep_merge(config, tomllib.loads(settings.read_text(encoding="utf-8")))
    config["data_dir"] = str(folder)
    config["root"] = str(root.resolve())
    config["config_path"] = str(path)
    return config


def write_settings(path: Path, overlay: dict) -> None:
    """Trang Settings ghi lớp phủ riêng, không sửa studio.toml đã commit."""
    import tempfile

    import toml   # do gói mpt ghim

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".settings-", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("# Ghi bởi trang Settings; phủ lên studio.toml. Xóa file để về mặc định.\n")
        handle.write(toml.dumps(overlay))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(name, path)


def effective(config: dict, overrides: dict | None) -> dict:
    """Cấu hình của một task. Ghi đè chỉ chạm adapter trực tiếp và tham số lời gọi."""
    return deep_merge(config, overrides)


def redact(text: str) -> str:
    """Bỏ giá trị bí mật khỏi chuỗi trước khi ghi log hoặc lỗi (NFR-07)."""
    for name in SECRET_ENV:
        value = os.environ.get(name)
        if value and len(value) >= 6:
            text = text.replace(value, f"<{name}>")
    return text
