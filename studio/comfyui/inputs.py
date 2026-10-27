"""Loại workflow và input mapping cho form chạy có kiểm (10 · Loại workflow và input; màn S14, S16, S17).

- **Loại** là nhãn trên revision để lọc thư viện và chọn mẫu, không phải luồng code riêng. Lúc nhập, loại được đoán
  từ ``class_type`` để người dùng xác nhận (S15); ``Giữ nhân vật`` không đoán được nên chỉ do người dùng chọn.
- **Mapping** khai ô nào trên form ứng với ``node · input`` nào trong API JSON. Chỉ input literal (không phải link)
  mới khai được, và kiểu ô phải hợp với giá trị đang có. Mapping là một phần của revision: đổi mapping là revision mới
  và phải duyệt lại.
- **Áp input** chỉ thay đúng các ô đã khai, kiểm phía server theo mapping. Không dò node rồi ghi vào ``inputs`` bất kỳ.
"""

from __future__ import annotations

import copy
import re
import secrets

KINDS = {
    "t2i": "Ảnh từ chữ",
    "i2i": "Sửa ảnh",
    "i2v": "Video từ ảnh",
    "upscale": "Phóng to",
    "tts": "Giọng đọc",
    "fix": "Sửa dị dạng",
    "character": "Giữ nhân vật",
}
FIELD_KINDS = {
    "text": "Chữ",
    "number": "Số",
    "seed": "Seed",
    "image": "Ảnh",
    "character": "Nhân vật",
    "voice": "Giọng đọc",
}
MAX_FIELDS = 20
TEXT_MAX = 100_000
TEXT_DEFAULT_MAX = 2000
SEED_MAX = 2**32 - 1
LABEL = re.compile(r"[^\x00-\x1f<>]{1,60}")


class InputError(ValueError):
    """Mapping hoặc giá trị input không đạt; không đổi gì."""


def guess_kind(class_types: list[str]) -> str:
    """Loại đoán từ ``class_type``; người dùng xác nhận hoặc sửa ở S15."""
    names = [c.lower() for c in class_types]

    def has(*needles: str) -> bool:
        return any(n in name for name in names for n in needles)

    if has("tts", "texttospeech", "vieneu"):
        return "tts"
    if has("imagetovideo", "savevideo", "savewebm", "videocombine", "createvideo"):
        return "i2v"
    if has("detailer"):
        return "fix"
    if has("upscale") and not has("ksampler", "samplercustom"):
        return "upscale"
    if "loadimage" in names:
        return "i2i"
    return "t2i"


def _literal(value) -> bool:
    is_link = isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int)
    return not is_link and not isinstance(value, (list, dict))


def candidates(prompt: dict) -> list[dict]:
    """Input literal khai được, kèm kiểu ô hợp lệ: chọn từ danh sách này (S17), không gõ tay node ID."""
    found = []
    for node_id in sorted(prompt, key=lambda k: (len(k), k)):
        node = prompt[node_id]
        for name, value in sorted((node.get("inputs") or {}).items()):
            if not _literal(value):
                continue
            kinds = allowed_kinds(name, value)
            if kinds:
                shown = value if not isinstance(value, str) else (value[:40] + "…" if len(value) > 40 else value)
                found.append({"node": node_id, "input": name, "class_type": node["class_type"], "value": shown,
                              "kinds": kinds, "suggest": suggest_kind(node["class_type"], name, value)})
    return found


def allowed_kinds(name: str, value) -> list[str]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return ["number", "seed"]
    if isinstance(value, float):
        return ["number"]
    if isinstance(value, str):
        return ["text", "image", "character", "voice"]
    return []


def suggest_kind(class_type: str, name: str, value) -> str:
    if isinstance(value, int) and "seed" in name.lower():
        return "seed"
    if isinstance(value, str) and name == "image":
        return "image"
    if isinstance(value, str) and name.lower() in ("voice", "speaker"):
        return "voice"
    return allowed_kinds(name, value)[0]


def validate_mapping(prompt: dict, fields: list[dict]) -> list[dict]:
    """Mapping đã chuẩn hóa; ``InputError`` liệt kê mọi chỗ sai."""
    problems, clean, seen_targets, seen_labels = [], [], set(), set()
    if len(fields) > MAX_FIELDS:
        raise InputError(f"tối đa {MAX_FIELDS} ô")
    for index, raw in enumerate(fields, start=1):
        label = str(raw.get("label") or "").strip()
        kind, node_id, name = str(raw.get("kind") or ""), str(raw.get("node") or ""), str(raw.get("input") or "")
        where = f"ô {index} ({label or 'chưa đặt tên'})"
        if not LABEL.fullmatch(label):
            problems.append(f"{where}: tên ô 1–60 ký tự")
        if label.lower() in seen_labels:
            problems.append(f"{where}: trùng tên ô")
        seen_labels.add(label.lower())
        node = prompt.get(node_id)
        if node is None or name not in (node.get("inputs") or {}):
            problems.append(f"{where}: node · input không có trong workflow")
            continue
        value = node["inputs"][name]
        if not _literal(value):
            problems.append(f"{where}: {node_id}.{name} là liên kết giữa các node, không khai được")
            continue
        if kind not in allowed_kinds(name, value):
            problems.append(f"{where}: kiểu {FIELD_KINDS.get(kind, kind)!r} không hợp với giá trị {type(value).__name__}")
            continue
        if (node_id, name) in seen_targets:
            problems.append(f"{where}: {node_id}.{name} đã khai ở ô khác")
        seen_targets.add((node_id, name))
        field = {"key": f"in{index}", "label": label, "kind": kind, "node": node_id, "input": name,
                 "class_type": node["class_type"], "required": bool(raw.get("required"))}
        try:
            if kind == "number":
                field["min"] = _number(raw.get("min"), value)
                field["max"] = _number(raw.get("max"), value)
                if field["min"] is not None and field["max"] is not None and field["min"] > field["max"]:
                    problems.append(f"{where}: giới hạn dưới lớn hơn giới hạn trên")
            elif kind == "text":
                limit = int(raw.get("max_length") or TEXT_DEFAULT_MAX)
                if not 1 <= limit <= TEXT_MAX:
                    raise ValueError
                field["max_length"] = limit
            elif kind == "voice":
                options = [o.strip() for o in str(raw.get("options") or "").split(",") if o.strip()]
                if any(len(o) > 100 for o in options) or len(options) > 50:
                    raise ValueError
                field["options"] = options
        except (TypeError, ValueError):
            problems.append(f"{where}: giới hạn không hợp lệ")
        clean.append(field)
    if problems:
        raise InputError("; ".join(problems))
    return clean


def _number(raw, sample):
    if raw in (None, ""):
        return None
    return int(raw) if isinstance(sample, int) else float(raw)


def limits(field: dict) -> str:
    """Mô tả giới hạn cho bảng S17 và form S16."""
    kind = field["kind"]
    if kind == "text":
        return f"1–{field.get('max_length', TEXT_DEFAULT_MAX)} ký tự"
    if kind == "seed":
        return f"0–{SEED_MAX}; để trống là ngẫu nhiên"
    if kind == "number":
        low, high = field.get("min"), field.get("max")
        return f"{'' if low is None else low}–{'' if high is None else high}" if low is not None or high is not None else "—"
    if kind == "image":
        return "PNG, JPEG, WebP; tối đa 64 MiB"
    if kind == "character":
        return "Ảnh tham chiếu đã duyệt của Series"
    if kind == "voice":
        return ", ".join(field.get("options") or []) or "theo giọng có trên máy"
    return "—"


def resolve(fields: list[dict], values: dict, prompt: dict) -> dict:
    """Giá trị form → giá trị đã kiểm theo mapping (ảnh, nhân vật: đường dẫn do lớp service kiểm riêng).

    Ô không bắt buộc để trống thì giữ giá trị trong workflow; seed để trống thì ngẫu nhiên.
    """
    resolved, problems = {}, []
    for field in fields:
        raw = values.get(field["key"])
        current = prompt[field["node"]]["inputs"][field["input"]]
        blank = raw is None or (isinstance(raw, str) and not raw.strip())
        if blank:
            if field["kind"] == "seed":
                resolved[field["key"]] = secrets.randbelow(SEED_MAX + 1)
            elif field["required"]:
                problems.append(f"{field['label']}: bắt buộc")
            continue
        kind = field["kind"]
        try:
            if kind == "text":
                text = str(raw)
                if len(text) > field.get("max_length", TEXT_DEFAULT_MAX):
                    raise InputError(f"{field['label']}: dài quá {field.get('max_length', TEXT_DEFAULT_MAX)} ký tự")
                resolved[field["key"]] = text
            elif kind in ("number", "seed"):
                number = int(raw) if isinstance(current, int) else float(raw)
                low = 0 if kind == "seed" else field.get("min")
                high = SEED_MAX if kind == "seed" else field.get("max")
                if (low is not None and number < low) or (high is not None and number > high):
                    raise InputError(f"{field['label']}: ngoài khoảng {limits(field)}")
                resolved[field["key"]] = number
            elif kind == "voice":
                voice = str(raw).strip()
                if field.get("options") and voice not in field["options"]:
                    raise InputError(f"{field['label']}: giọng không có trong danh sách")
                if len(voice) > 100:
                    raise InputError(f"{field['label']}: tên giọng quá dài")
                resolved[field["key"]] = voice
            else:                                    # image, character: service kiểm đường dẫn và nội dung
                resolved[field["key"]] = raw
        except InputError as exc:
            problems.append(str(exc))
        except (TypeError, ValueError):
            problems.append(f"{field['label']}: phải là số")
    if problems:
        raise InputError("; ".join(problems))
    return resolved


def apply(prompt: dict, fields: list[dict], values: dict) -> dict:
    """Prompt mới với các ô đã khai thay bằng ``values`` (đã resolve; ảnh là tên file trên máy)."""
    result = copy.deepcopy(prompt)
    for field in fields:
        if field["key"] in values:
            result[field["node"]]["inputs"][field["input"]] = values[field["key"]]
    return result


def seed_field(fields: list[dict]) -> dict | None:
    return next((f for f in fields if f["kind"] == "seed"), None)


def limit_text(field: dict) -> str:
    """Giới hạn dạng một ô chữ trên S17 (ngược với ``parse_limit``)."""
    kind = field.get("kind")
    if kind == "text" and field.get("max_length"):
        return str(field["max_length"])
    if kind == "number" and (field.get("min") is not None or field.get("max") is not None):
        low, high = field.get("min"), field.get("max")
        return f"{'' if low is None else low}–{'' if high is None else high}"
    if kind == "voice":
        return ", ".join(field.get("options") or [])
    return ""


def parse_limit(kind: str, text: str) -> dict:
    """Một ô Giới hạn trên S17 → khóa của mapping: ký tự tối đa (chữ), ``từ–tới`` (số), danh sách giọng."""
    text = (text or "").strip()
    if not text:
        return {}
    if kind == "text":
        return {"max_length": text}
    if kind == "number":
        low, sep, high = text.replace("—", "–").replace("–", "-").partition("-") if not text.startswith("-") else ("", "", text)
        if not sep:
            raise InputError(f"giới hạn số phải là từ–tới: {text!r}")
        return {"min": low.strip() or None, "max": high.strip() or None}
    if kind == "voice":
        return {"options": text}
    return {}
