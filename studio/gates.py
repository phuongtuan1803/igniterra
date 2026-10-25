"""Gate và lớp hành động của người (03 · Gate, sửa và chạy lại; 06 · Can thiệp).

CLI và web gọi cùng các hàm này. Mọi hành động bị từ chối khi task đang ``running``;
sau hành động task về ``pending`` để worker chạy lại đúng các mục có input_hash đổi.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from studio import media
from studio.contracts import AssetCandidate, Brief, Review, ScenePlan, Shot
from studio.store import Store
from studio.taskdir import TaskDir, commit_file


class ActionError(Exception):
    pass


# ---------------------------------------------------------------- worker hỏi


def subject(ctx, gate: str) -> str | None:
    if gate == "keyframes":
        selected = {scene: (ctx.td.selected(scene, "image").id if ctx.td.selected(scene, "image") else None)
                    for scene in ctx.scene_ids()}
        return "img:" + hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()[:16]
    if gate == "final":
        from studio.pipeline import current_render   # tránh vòng import

        return current_render(ctx)
    raise ValueError(gate)


def approved(ctx, gate: str) -> bool:
    current = subject(ctx, gate)
    decisions = [d for d in ctx.store.decisions(ctx.task_id, gate) if d["subject"] == current]
    return bool(decisions) and decisions[-1]["decision"] == "approve"


def describe(ctx, gate: str) -> str:
    if gate == "keyframes":
        return "duyệt ảnh keyframe: " + ", ".join(
            f"{s}={ctx.td.selected(s, 'image').path}" for s in ctx.scene_ids() if ctx.td.selected(s, "image"))
    return f"duyệt bản render {subject(ctx, 'final')}: renders/{subject(ctx, 'final')}/final_9x16.mp4"


def rerun_request(ctx, step: str, scene: str) -> dict | None:
    decisions = ctx.store.decisions(ctx.task_id, "rerun", f"{step}:{scene}")
    return decisions[-1] if decisions else None


def unknown_decision(ctx, request_id: str) -> str | None:
    return unknown_decision_raw(ctx.store, ctx.task_id, request_id)


def unknown_decision_raw(store: Store, task_id: str, request_id: str) -> str | None:
    decisions = store.decisions(task_id, "unknown_call", request_id)
    return decisions[-1]["decision"] if decisions else None


# ---------------------------------------------------------------- hành động


class Actions:
    def __init__(self, store: Store, config: dict):
        self.store = store
        self.config = config

    def _td(self, task_id: str) -> TaskDir:
        return TaskDir(Path(self.config["data_dir"]), task_id)

    def _task(self, task_id: str, allow: tuple[str, ...] = ("pending", "waiting", "completed")) -> dict:
        task = self.store.task(task_id)
        if task["status"] == "running":
            raise ActionError(f"{task_id} đang chạy bước {task['current_step']}; dùng `task pause {task_id}` rồi thử lại")
        if task["status"] not in allow:
            raise ActionError(f"{task_id} đang {task['status']}; hành động này cần {', '.join(allow)}")
        return task

    def _requeue(self, task_id: str) -> None:
        self.store.set_status(task_id, "pending", expect=("pending", "waiting", "completed"))

    # -------------------------------------------------------- task

    def create_task(self, brief: Brief, series: str | None = None, overrides: dict | None = None) -> str:
        task_id = self.store.next_task_id()
        td = self._td(task_id)
        td.write_brief(brief)
        self.store.create_task(task_id, brief.model_dump(mode="json"), series, overrides or {})
        return task_id

    def pause(self, task_id: str) -> str:
        task = self.store.task(task_id)
        if task["status"] == "running":
            self.store.request_pause(task_id)
            return "đã yêu cầu dừng ở ranh giới mục kế tiếp"
        if task["status"] == "pending":
            self.store.set_status(task_id, "waiting", "paused", "tạm dừng trước khi chạy", expect=("pending",))
            return "task đã waiting (paused)"
        raise ActionError(f"{task_id} đang {task['status']}, không cần dừng")

    def resume(self, task_id: str) -> None:
        task = self._task(task_id, ("waiting",))
        if task["wait_reason"] not in ("paused", "retries_exhausted", "duration_exceeded"):
            raise ActionError(f"{task_id} chờ {task['wait_reason']}; dùng lệnh gate/unknown tương ứng")
        self._requeue(task_id)

    def cancel(self, task_id: str, note: str = "") -> None:
        self._task(task_id, ("pending", "waiting"))
        self.store.set_status(task_id, "failed", None, f"người hủy: {note}")

    # -------------------------------------------------------- gate

    def decide(self, task_id: str, decision: str, note: str | None = None) -> str:
        task = self._task(task_id, ("waiting",))
        reason = task["wait_reason"] or ""
        if not reason.startswith("gate:"):
            raise ActionError(f"{task_id} không chờ gate (đang chờ {reason})")
        gate = reason.split(":", 1)[1]
        from studio.pipeline import make_ctx   # tránh vòng import

        ctx = make_ctx(self.store, self.config, task_id)
        current = subject(ctx, gate)
        if decision not in ("approve", "reject"):
            raise ActionError("decision phải là approve hoặc reject")
        if gate == "final" and decision == "approve":
            reviews = self.store.reviews(task_id, current)
            if not reviews:
                raise ActionError(f"chưa có phiếu duyệt cho {current}: chạy `review {task_id} --checklist ...` trước")
        self.store.decide(task_id, gate, task["plan_rev"], decision, subject=current,
                          render_rev=current if gate == "final" else None, note=note)
        if decision == "reject":
            return f"đã ghi từ chối gate {gate}; sửa scene, chọn ứng viên hoặc rerun rồi task chạy lại"
        if gate == "final":
            self.store.set_status(task_id, "completed", expect=("waiting",))
            return f"đã duyệt {current}; task completed"
        self._requeue(task_id)
        return f"đã duyệt gate {gate}; task pending"

    def review(self, task_id: str, review: Review) -> tuple[str, bool]:
        task = self._task(task_id, ("waiting", "completed"))
        from studio.pipeline import make_ctx

        render_rev = subject(make_ctx(self.store, self.config, task_id), "final")
        if not render_rev:
            raise ActionError(f"{task_id} chưa có bản render cho lựa chọn hiện tại")
        if task["status"] == "waiting" and task["wait_reason"] != "gate:final":
            raise ActionError(f"{task_id} chưa tới gate final")
        self.store.add_review(task_id, render_rev, {k: v.model_dump() for k, v in review.checklist.items()},
                              review.accepted, review.active_minutes, review.reviewer, review.note)
        return render_rev, review.accepted

    def unknown(self, task_id: str, scene: str, decision: str, note: str | None = None) -> str:
        task = self._task(task_id, ("waiting",))
        if decision not in ("retry", "skip"):
            raise ActionError("decision phải là retry hoặc skip")
        runs = [r for r in self.store.runs(task_id, scene=scene) if r["status"] == "unknown"]
        if not runs:
            raise ActionError(f"{scene} không có lượt gọi unknown")
        run = runs[-1]
        request_id = f"{task_id}:{scene}:{run['step']}:{run['attempt']}"
        self.store.decide(task_id, "unknown_call", task["plan_rev"], decision, subject=request_id, note=note)
        self._requeue(task_id)
        return request_id

    # -------------------------------------------------------- sửa

    def select(self, task_id: str, scene: str, candidate_id: str) -> str:
        self._task(task_id)
        kind = self._td(task_id).select(scene, candidate_id)
        self._requeue(task_id)
        return kind

    def edit_scene(self, task_id: str, scene_id: str, note: str = "", **fields) -> int:
        task = self._task(task_id)
        td = self._td(task_id)
        current = td.plan(task["plan_rev"])
        data = current.model_dump(mode="json")
        scene = next((s for s in data["scenes"] if s["id"] == scene_id), None)
        if scene is None:
            raise ActionError(f"không có scene {scene_id}")
        for key, value in fields.items():
            if value is None:
                continue
            if key in ("size", "angle", "movement"):
                scene["shot"][key] = value
            elif key in ("narration", "visual", "mode"):
                scene[key] = value
            else:
                raise ActionError(f"không sửa được trường {key}")
        from studio.pipeline import load_bible

        bible, _ = load_bible(self.config, task["series"])
        Shot.model_validate(scene["shot"])
        for key in ("size", "angle", "movement"):
            if scene["shot"][key] not in bible.camera[key]:
                raise ActionError(f"{key}={scene['shot'][key]!r} không có trong từ vựng camera")
        data.update(parent=current.revision, note=note or f"sửa {scene_id}: {', '.join(k for k, v in fields.items() if v is not None)}")
        revision = td.write_plan(ScenePlan.model_validate(data))
        self.store.update_task(task_id, plan_rev=revision)
        self._requeue(task_id)
        return revision

    def rerun(self, task_id: str, scene: str, step: str, note: str) -> None:
        task = self._task(task_id)
        self.store.decide(task_id, "rerun", task["plan_rev"], "retry", subject=f"{step}:{scene}", note=note)
        self._requeue(task_id)

    def upload(self, task_id: str, scene: str, kind: str, file: Path, source: str, rights: str,
               ai_generated: bool) -> str:
        task = self._task(task_id)
        td = self._td(task_id)
        file = Path(file)
        existing = [c for c in td.candidates(scene, kind) if c.provider == "user"]
        candidate_id = f"up_{scene}_{len(existing) + 1}"
        tmp = td.tmp_path("uploads", file.suffix)
        shutil.copyfile(file, tmp)
        final = td.root / "uploads" / f"{candidate_id}{file.suffix.lower()}"
        digest = commit_file(tmp, final)
        info = media.probe(final)
        td.add_candidate(scene, AssetCandidate(
            id=candidate_id, kind=kind, path=td.rel(final), provider="user", plan_rev=task["plan_rev"],
            ai_generated=ai_generated, source=source, rights=rights, sha256=digest,
            duration_s=round(info["duration"], 3) if kind in ("voice", "clip", "music") else None,
            width=info["width"], height=info["height"]))
        self._requeue(task_id)
        return candidate_id
