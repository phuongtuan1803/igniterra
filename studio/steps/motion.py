"""Bước motion (theo scene mode: motion, Gate 4): VideoProvider submit → lưu job_id → poll.

Resume luôn poll job cũ (``Item.job_id``), không submit mới. Lỗi thì animate dựng ảnh tĩnh.
"""

from __future__ import annotations

import time

from studio import costs, media
from studio.contracts import CostEvent
from studio.steps.base import Ctx, Item, Result
from studio.taskdir import commit_file

POLL_S = 5.0


def applies(ctx: Ctx, scene: str) -> bool:
    return ctx.plan.scene(scene).mode == "motion" and ctx.config["adapters"].get("video", "none") != "none"


def inputs(ctx: Ctx, scene: str) -> dict:
    image = ctx.td.selected(scene, "image")
    voice = ctx.td.selected(scene, "voice")
    spec = ctx.plan.scene(scene)
    return {
        "image": [image.id, image.sha256] if image else None,
        "seconds": round(voice.duration_s) if voice else None,
        "visual": spec.visual,
        "movement": spec.shot.movement,
        "video": ctx.adapter_id("video"),
    }


def run(ctx: Ctx, item: Item, job_id: str | None = None, poll_s: float = POLL_S) -> Result:
    video = ctx.adapter("video")
    image = ctx.td.selected(item.scene, "image")
    voice = ctx.td.selected(item.scene, "voice")
    spec = ctx.plan.scene(item.scene)
    if job_id is None:
        text = f"{ctx.bible.camera['movement'][spec.shot.movement]}. {spec.visual}"
        # pending ghi trước submit; dòng kết quả chỉ ghi khi poll xong (có thể ở lần chạy sau).
        costs.append(ctx.td.costs_path, CostEvent(
            ts=costs._now(), request_id=item.request_id, status="pending",
            provider=video.name, model=video.model))
        job_id = video.submit(ctx.td.abs(image.path), text, max(1, round(voice.duration_s)))
        ctx.store.set_job(item.run_id, job_id)
    tmp = ctx.td.tmp_path("clips", ".mp4")
    try:
        while (candidate := video.poll(job_id, tmp)) is None:
            time.sleep(poll_s)
        costs.append(ctx.td.costs_path, CostEvent(
            ts=costs._now(), request_id=item.request_id, status="ok",
            units={"seconds": float(round(voice.duration_s))}))
        render = ctx.config["render"]
        info = media.check_clip(tmp, render["width"], render["height"], float(voice.duration_s))
        final = ctx.td.root / "clips" / f"motion_{item.scene}_a{item.attempt}.mp4"
        digest = commit_file(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    candidate = candidate.model_copy(update={
        "id": f"mot_{item.scene}_a{item.attempt}", "kind": "clip", "path": ctx.td.rel(final),
        "plan_rev": ctx.plan_rev, "sha256": digest, "duration_s": round(info["duration"], 3),
        "from_ids": [image.id],
    })
    return Result(output=final, candidate=candidate)
