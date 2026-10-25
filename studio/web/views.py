"""Dữ liệu chỉ đọc cho các trang review. Không ghi gì, không gọi provider."""

from __future__ import annotations

import json
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


def task_summary(store: Store, data_dir: Path, task: dict) -> dict:
    td = TaskDir(data_dir, task["id"])
    usd, missing = costs.total(td.costs_path)
    brief = store.task(task["id"])["brief"]
    return {**task, "title": brief.get("title"), "brief_id": brief.get("id"), "usd": usd, "unpriced": missing,
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
