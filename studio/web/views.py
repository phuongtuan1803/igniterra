"""Dữ liệu chỉ đọc cho các trang review. Không ghi gì, không gọi provider."""

from __future__ import annotations

import json
import re
from pathlib import Path

from studio import costs, gates
from studio.contracts import CHECKLIST
from studio.store import Store
from studio.taskdir import TaskDir

STEP_ORDER = ["plan", "keyframes", "gate:keyframes", "voice", "subtitles", "motion", "animate", "render", "gate:final"]
CHECKLIST_TEXT = {
    "C01": "MP4 phát được, dọc 9:16, 1080×1920",
    "C02": "Tổng thời lượng không vượt 60 giây",
    "C03": "Hook trong 3 giây đầu, đủ diễn biến, đúng twist",
    "C04": "Giọng Việt rõ, không đọc sai đổi nghĩa",
    "C05": "Phụ đề có dấu, không mất chữ, không lệch nguyên câu",
    "C06": "Đủ hình bắt buộc, không mâu thuẫn lời kể, không lộ twist sớm",
    "C07": "Chủ thể chính không đổi loại/ngoại hình khi xuất hiện lại",
    "C08": "Không khung đen, lỗi hình nghiêm trọng, âm thanh vỡ",
    "C09": "Lưu nguồn/quyền tài sản; mô tả ghi truyện hư cấu, hình/giọng AI",
}


def comfy_origin(note: str | None) -> dict | None:
    match = re.match(r"image_request:(t_[0-9]+):(s[0-9]{2}):keyframes:([0-9]+)(?:\n|$)", note or "")
    return {"task_id": match[1], "scene": match[2], "attempt": int(match[3])} if match else None


def task_summary(store: Store, data_dir: Path, task: dict) -> dict:
    td = TaskDir(data_dir, task["id"])
    usd, missing = costs.total(td.costs_path)
    details = store.task(task["id"])
    brief = details["brief"]
    return {**task, "series": details.get("series"), "title": brief.get("title"), "brief_id": brief.get("id"), "usd": usd, "unpriced": missing,
            "renders": td.render_revisions()}


def storyboard(store: Store, config: dict, task_id: str) -> dict:
    task = store.task(task_id)
    td = TaskDir(Path(config["data_dir"]), task_id)
    plan = td.plan(task["plan_rev"]) if task["plan_rev"] else None
    assets = td.assets()
    events = costs.read(td.costs_path)
    scenes = []
    for scene in (plan.scenes if plan else []):
        slots = assets.get(scene.id, {})
        kinds = {}
        for kind in ("image", "voice", "clip"):
            slot = slots.get(kind) or {"selected": None, "candidates": []}
            kinds[kind] = {"selected": slot["selected"], "candidates": slot["candidates"]}
        image = next((c for c in kinds["image"]["candidates"] if c["id"] == kinds["image"]["selected"]), None)
        stale = bool(image and image.get("plan_rev") and image["plan_rev"] < _scene_rev(td, task["plan_rev"], scene.id, "visual"))
        scene_usd = sum(e.est_usd or 0 for e in events if e.status == "ok" and f":{scene.id}:" in e.request_id)
        scenes.append({"scene": scene, "kinds": kinds, "image": image, "stale": stale, "usd": scene_usd,
                       "unknown": [r for r in store.runs(task_id, scene=scene.id) if r["status"] == "unknown"]})
    from studio.pipeline import load_bible

    try:
        bible, _ = load_bible(config, task["series"])
        camera = bible.camera
    except Exception:  # noqa: BLE001 — trang vẫn hiện khi series lỗi
        camera = {}
    latest = td.render_revisions()
    current_render = None
    if plan:
        try:
            from studio.pipeline import make_ctx

            current_render = gates.subject(make_ctx(store, config, task_id), "final")
        except Exception:  # noqa: BLE001
            current_render = None
    manifest = None
    if current_render:
        manifest = json.loads((td.root / "renders" / current_render / "manifest.json").read_text(encoding="utf-8"))
    return {"task": task, "plan": plan, "scenes": scenes, "camera": camera, "renders": latest,
            "current_render": current_render, "manifest": manifest, "reviews": store.reviews(task_id), "checklist": CHECKLIST,
            "checklist_text": CHECKLIST_TEXT}


def _scene_rev(td: TaskDir, current: int, scene_id: str, field: str) -> int:
    """Revision gần nhất mà trường của scene thay đổi (để đánh dấu ứng viên lệch revision)."""
    revision = current
    value = _field(td, current, scene_id, field)
    while revision > 1 and _field(td, revision - 1, scene_id, field) == value:
        revision -= 1
    return revision


def _field(td: TaskDir, revision: int, scene_id: str, field: str):
    try:
        scene = td.plan(revision).scene(scene_id)
    except (FileNotFoundError, KeyError):
        return None
    return json.dumps([getattr(scene, field), scene.shot.model_dump(), scene.characters], ensure_ascii=False)


def detail(store: Store, config: dict, task_id: str) -> dict:
    task = store.task(task_id)
    td = TaskDir(Path(config["data_dir"]), task_id)
    runs = store.runs(task_id)
    steps = []
    for name in STEP_ORDER:
        if name.startswith("gate:"):
            gate = name.split(":", 1)[1]
            decisions = store.decisions(task_id, gate)
            steps.append({"name": name, "gate": True, "decisions": decisions})
            continue
        mine = [r for r in runs if r["step"] == name]
        if mine:
            steps.append({"name": name, "gate": False, "runs": mine,
                          "done": sum(r["status"] == "done" for r in mine), "attempts": len(mine)})
    events = costs.read(td.costs_path)
    orphans = {e.request_id for e in costs.orphans(td.costs_path)}
    log_path = td.root / "worker.log"
    log_tail = log_path.read_text(encoding="utf-8").splitlines()[-50:] if log_path.is_file() else []
    unknown = []
    for run in runs:
        if run["status"] == "unknown":
            request_id = f"{task_id}:{run['scene']}:{run['step']}:{run['attempt']}"
            pending = next((e for e in events if e.request_id == request_id and e.status == "pending"), None)
            unknown.append({"run": run, "request_id": request_id, "pending": pending,
                            "decision": gates.unknown_decision_raw(store, task_id, request_id)})
    usd, missing = costs.total(td.costs_path)
    return {"task": task, "steps": steps, "events": events, "orphans": orphans, "log": log_tail,
            "unknown": unknown, "total_usd": usd, "unpriced": missing, "renders": td.render_revisions(),
            "plans": td.plan_revisions()}


RUN_LABELS = {"checking": "đang kiểm", "submitting": "đang gửi", "submit_unknown": "chưa rõ đã gửi", "queued": "đang chờ",
              "running": "đang chạy", "downloading": "đang tải output", "output_failed": "tải output lỗi",
              "succeeded": "đã kiểm", "failed": "lỗi", "blocked": "bị chặn", "abandoned": "đã bỏ"}
GROUP_LABELS = {"running": "Đang chạy", "awaiting_review": "Chờ bạn duyệt", "passed": "Đạt", "exhausted": "Hết lượt",
                "capped": "Chạm trần chi phí", "stopped": "Đã dừng"}


def usd(value: float | None) -> str:
    """Chi phí ước tính: số nhỏ hơn 0,001 USD hiện là "dưới $0.001", không phải "$0.000"."""
    if value is None:
        return "—"
    if value < 0.001:
        return "dưới $0.001"
    return f"${value:.3f}"


def machine_label(rental) -> str:
    """Tên máy cho người mới: tên tự đặt (máy cố định) hoặc GPU, loại máy và giá."""
    if rental.provider_key == "fixed":
        price = f" · ${rental.offer.dph_total:.2f}/giờ" if rental.offer.dph_total else ""
        return f"{rental.offer.offer_id} · Cố định{price}"
    return f"{rental.offer.num_gpus}× {rental.offer.gpu_name} · Thuê · ${rental.offer.dph_total:.3f}/giờ"


def workflow_gates(record, machines: list, runs: list, checked: str | None = None) -> list[dict]:
    """Bốn cổng trước khi chạy (S16); cổng chưa đạt có một nút tới chỗ sửa. ``checked``: máy vừa qua nút Kiểm máy."""
    base = f"/comfyui/workflows/{record.workflow_id}?revision={record.revision}"
    deps = len(record.draft.get("models") or []), len(record.draft.get("custom_nodes") or [])
    if record.status == "approved":
        dep = {"ok": True, "title": "Dependency đã duyệt",
               "detail": f"{deps[0]} model, {deps[1]} custom node" if any(deps) else "Chỉ dùng node lõi ComfyUI"}
    elif record.status == "pending_review":
        what = "input mapping" if record.inputs_pending and record.manifest_status == "approved" else "dependency"
        dep = {"ok": False, "title": "Chờ duyệt", "detail": f"Đọc {what} rồi bấm Duyệt", "fix": base + "#deps",
               "fix_label": "Xem và duyệt"}
    else:
        dep = {"ok": False, "title": "Chưa khai đủ dependency", "detail": "Igniterra không đoán dependency; cần manifest",
               "fix": base + "#deps", "fix_label": "Khai manifest"}
    blocked = next((r for r in runs if r.revision == record.revision and r.error_code in ("missing_class_type", "invalid_input")), None)
    latest_run = next((r for r in runs if r.revision == record.revision), None)
    machine_ok = bool(checked) or blocked is None or (latest_run is not None and latest_run.run_id != blocked.run_id
                                                      and latest_run.status == "succeeded")
    return [
        {"ok": record.runnable, "title": "Có bản chạy được" if record.runnable else "Chưa có bản chạy được",
         "detail": f"API JSON trong revision {record.revision}" if record.runnable else "Mới có bản Save (GUI)",
         "fix": base + "#new-revision", "fix_label": "Nhập bản Export (API)"},
        dep,
        {"ok": bool(machines), "title": "Có máy sẵn sàng" if machines else "Chưa có máy sẵn sàng",
         "detail": f"{len(machines)} máy ready" if machines else "Thuê máy hoặc thêm máy cố định",
         "fix": "/gpu", "fix_label": "Mở máy GPU"},
        {"ok": machine_ok, "title": "Máy có đủ node và model" if machine_ok else "Lần trước máy thiếu node hoặc model",
         "detail": (f"Đã kiểm {checked}: đủ" if checked else "Kiểm /object_info lúc bấm Chạy; thiếu thì chặn trước khi gửi")
         if machine_ok else (blocked.detail or "")[:160],
         "fix": f"/comfyui/runs/{blocked.run_id}" if blocked else None, "fix_label": "Xem chỗ thiếu", "check": True},
    ]


def audio_info(path: Path, media_type: str) -> dict:
    """Thời lượng và định dạng của output giọng đọc (S19). WAV đọc từ header; định dạng khác chỉ ghi loại."""
    import wave

    fmt = {"audio/wav": "WAV", "audio/mpeg": "MP3", "audio/flac": "FLAC", "audio/ogg": "OGG"}.get(media_type, media_type)
    if media_type != "audio/wav":
        return {"format": fmt, "seconds": None, "detail": ""}
    try:
        with wave.open(str(path), "rb") as handle:
            rate, frames, channels = handle.getframerate(), handle.getnframes(), handle.getnchannels()
    except (wave.Error, OSError, EOFError):
        return {"format": fmt, "seconds": None, "detail": ""}
    layout = {1: "mono", 2: "stereo"}.get(channels, f"{channels} kênh")
    return {"format": fmt, "seconds": frames / rate if rate else None,
            "detail": f"{rate / 1000:g} kHz {layout}".replace(".", ",")}


def shown_inputs(record, run) -> list[dict]:
    """Giá trị run đã gửi theo mapping của revision, dạng đọc được (ảnh: tên file, nhân vật: tên)."""
    values = run.inputs
    shown = []
    for field in record.inputs:
        if field["key"] not in values:
            continue
        value = values[field["key"]]
        if isinstance(value, dict):
            value = value.get("ref") or Path(value.get("path", "")).name
        shown.append({"label": field["label"], "kind": field["kind"], "value": value})
    return shown


def scene_targets(store: Store, data_dir: Path) -> list[dict]:
    """Scene của các task đã có storyboard, cho nút Dùng cho scene (S19)."""
    targets = []
    for task in store.tasks():
        if not task.get("plan_rev"):
            continue
        try:
            plan = TaskDir(data_dir, task["id"]).plan(task["plan_rev"])
        except (OSError, ValueError):
            continue
        for scene in plan.scenes:
            targets.append({"value": f"{task['id']}/{scene.id}",
                            "label": f"{task['id']} · {scene.id} · {(scene.narration or '')[:50]}"})
    return targets
