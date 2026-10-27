"""Danh sách bước video và vòng lặp worker (03 · Worker và luồng thực thi).

Không biết provider nào đang chạy. Trạng thái là StepRun trong SQLite và file trong thư mục task;
resume chỉ cần đọc lại hai nơi đó.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from studio import config as studio_config
from studio import costs, gates, media, prompt
from studio.contracts import Brief, SeriesBible
from studio.providers.base import ContentRefused, Fatal, QualityFail, Retryable, Unconfirmed
from studio.steps import animate, keyframes, motion, plan, render, subtitles, voice
from studio.steps.base import Ctx, Item, Result, WaitFor
from studio.store import Store
from studio.taskdir import TaskDir, sha256

log = logging.getLogger("studio.worker")


@dataclass(frozen=True)
class Step:
    name: str
    module: object
    per_scene: bool
    pool: str | None = None        # khóa giới hạn song song trong [concurrency]


@dataclass(frozen=True)
class Gate:
    name: str
    optional: bool = False


VIDEO_STEPS: list[Step | Gate] = [
    Step("plan", plan, per_scene=False),
    Step("keyframes", keyframes, per_scene=True, pool="image"),
    Gate("keyframes", optional=True),
    Step("voice", voice, per_scene=True, pool="tts"),
    Step("subtitles", subtitles, per_scene=False),
    Step("motion", motion, per_scene=True, pool="video"),
    Step("animate", animate, per_scene=True, pool="animate"),
    Step("render", render, per_scene=False),
    Gate("final"),
]


class Stop(Exception):
    """Task phải dừng: chờ người (waiting) hoặc hỏng (failed)."""

    def __init__(self, status: str, reason: str | None, detail: str):
        super().__init__(detail)
        self.status, self.reason, self.detail = status, reason, detail


def tasklog(ctx: Ctx, message: str) -> None:
    """Một dòng vào data/tasks/<id>/worker.log (Task detail hiện 50 dòng cuối); đã lọc khóa."""
    log.info("%s %s", ctx.task_id, message)
    with (ctx.td.root / "worker.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {studio_config.redact(message)}\n")


def input_hash(value: dict) -> str:
    return hashlib.sha256(prompt.canonical(value).encode()).hexdigest()


# ---------------------------------------------------------------- dựng ngữ cảnh


def load_bible(config: dict, series: str | None) -> tuple[SeriesBible, Path | None]:
    if not series:
        return SeriesBible(), None
    from studio.series import SeriesDir

    sd = SeriesDir(Path(config["data_dir"]), series)
    if not sd.exists():
        raise Fatal(f"series {series!r} không có {sd.bible_path}")
    bible = sd.bible()
    for key, character in bible.characters.items():
        for ref in [*character.refs, *([character.sheet] if character.sheet else [])]:
            if not (sd.root / ref).is_file():
                raise Fatal(f"series {series}: thiếu ảnh tham chiếu {ref} của {key}")
    return bible, sd.root


def make_ctx(store: Store, base_config: dict, task_id: str) -> Ctx:
    task = store.task(task_id)
    config = studio_config.effective(base_config, task["overrides"])
    td = TaskDir(Path(config["data_dir"]), task_id)
    bible, series_dir = load_bible(config, task["series"])
    plan_rev = task["plan_rev"]
    return Ctx(task_id=task_id, store=store, td=td, config=config, brief=Brief.model_validate(task["brief"]),
               bible=bible, series=task["series"], series_dir=series_dir,
               plan=td.plan(plan_rev) if plan_rev else None, plan_rev=plan_rev)


# ---------------------------------------------------------------- một mục


def _inputs(ctx: Ctx, step: Step, scene: str) -> dict:
    value = {"step": step.name, **step.module.inputs(ctx, scene)}
    if step.name == "render":
        value["subtitles"] = _subtitles_output(ctx)[1]
    rerun = gates.rerun_request(ctx, step.name, scene)
    if rerun:
        value["rerun"] = rerun["id"]
    return value


def _subtitles_output(ctx: Ctx) -> tuple[Path, str]:
    run = ctx.store.done_run(ctx.task_id, "subtitles", "_", input_hash(_inputs(ctx, STEPS["subtitles"], "_")))
    if run is None:
        raise Fatal("render cần subtitles đã xong cho giọng hiện tại")
    return ctx.td.abs(run["output_path"]), run["output_sha256"]


def current_render(ctx: Ctx) -> str | None:
    """Render revision ứng với selected hiện tại (không phải thư mục mới nhất): bản mà gate final duyệt."""
    try:
        digest = input_hash(_inputs(ctx, STEPS["render"], "_"))
    except Fatal:
        return None
    run = ctx.store.done_run(ctx.task_id, "render", "_", digest)
    if not run or not _valid(ctx, run):
        return None
    return Path(run["output_path"]).parent.name


def _valid(ctx: Ctx, run: dict) -> bool:
    if not run["output_path"]:
        return False
    path = ctx.td.root / run["output_path"]
    return path.is_file() and sha256(path) == run["output_sha256"]


def _invoke(ctx: Ctx, step: Step, item: Item, job_id: str | None = None) -> Result:
    if step.name == "render":
        return render.run(ctx, item, _subtitles_output(ctx)[0])
    if step.name == "motion":
        return motion.run(ctx, item, job_id=job_id)
    return step.module.run(ctx, item)


def _commit(ctx: Ctx, step: Step, item: Item, result: Result) -> None:
    """Luồng chính: ghi revision / ứng viên rồi StepRun done (03 · commit artifact)."""
    output = result.output
    if result.plan is not None:
        revision = ctx.td.write_plan(result.plan)
        ctx.store.update_task(ctx.task_id, plan_rev=revision)
        ctx.plan, ctx.plan_rev = ctx.td.plan(revision), revision
        output = ctx.td.plan_path(revision)
    if result.candidate is not None:
        ctx.td.add_candidate(item.scene, result.candidate, select=True)
    ctx.store.finish_run(item.run_id, "done", output_path=ctx.td.rel(output), output_sha256=sha256(output))


def _prepare(ctx: Ctx, step: Step, scene: str) -> tuple[str, dict | None, str | None] | str:
    """Kiểm trước khi chạy một mục. Trả 'skip'/'unknown' hoặc (hash, run_to_resume, note)."""
    value = _inputs(ctx, step, scene)
    digest = input_hash(value)
    done = ctx.store.done_run(ctx.task_id, step.name, scene, digest)
    if done and _valid(ctx, done):
        return "skip"
    latest = ctx.store.latest_run(ctx.task_id, step.name, scene)
    if latest and latest["status"] in ("unknown", "running") and latest["job_id"] and latest["input_hash"] == digest:
        return digest, latest, None                      # job bất đồng bộ: poll job cũ
    if latest and latest["status"] == "unknown":
        request_id = f"{ctx.task_id}:{scene}:{step.name}:{latest['attempt']}"
        decision = gates.unknown_decision(ctx, request_id)
        if decision is None:
            return "unknown"
        if decision == "skip":
            kind = {"keyframes": "image", "voice": "voice", "animate": "clip", "motion": "clip"}.get(step.name)
            if kind and ctx.td.selected(scene, kind):
                return "skip"
            raise Stop("waiting", "unknown_call", f"{request_id}: bỏ qua nhưng {scene} chưa có ứng viên {kind}; chọn retry")
    rerun = gates.rerun_request(ctx, step.name, scene)
    return digest, None, rerun["note"] if rerun else None


def run_item(ctx: Ctx, step: Step, scene: str, sleep: Callable[[float], None] = time.sleep) -> tuple[str, Item | None, Result | None]:
    """Chạy một mục (trong thread nếu per_scene). Không ghi assets.json: trả Result cho luồng chính."""
    prepared = _prepare(ctx, step, scene)
    if isinstance(prepared, str):
        return prepared, None, None
    digest, resume, note = prepared
    retry = ctx.config["retry"]
    feedback: list[str] = []
    for attempt_index in range(int(retry["max_attempts"])):
        if resume is not None:
            item = Item(ctx.task_id, step.name, scene, resume["attempt"], resume["id"], note)
            job_id = resume["job_id"]
            resume = None
        else:
            started = ctx.store.start_run(ctx.task_id, step.name, scene, digest, ctx.plan_rev, note)
            item = Item(ctx.task_id, step.name, scene, started["attempt"], started["id"], note, list(feedback))
            job_id = None
        try:
            result = _invoke(ctx, step, item, job_id)
            tasklog(ctx, f"{step.name} {scene} lượt {item.attempt}: xong")
            return "done", item, result
        except Retryable as exc:
            ctx.store.finish_run(item.run_id, "failed", error=f"retryable: {studio_config.redact(str(exc))}"[:2000])
            tasklog(ctx, f"{step.name} {scene} lượt {item.attempt}: Retryable {exc}")
            sleep(float(retry["backoff_s"]) * (2 ** attempt_index))
        except QualityFail as exc:
            ctx.store.finish_run(item.run_id, "failed", error=f"quality: {studio_config.redact(exc.reason)}"[:2000])
            tasklog(ctx, f"{step.name} {scene} lượt {item.attempt}: QualityFail {exc.reason}")
            feedback.append(("refused: " if isinstance(exc, ContentRefused) else "") + exc.reason[:600])
        except Unconfirmed as exc:
            ctx.store.finish_run(item.run_id, "unknown", error=f"unconfirmed: {studio_config.redact(str(exc))}"[:2000])
            tasklog(ctx, f"{step.name} {scene} lượt {item.attempt}: Unconfirmed {exc}")
            return "unknown", item, None
        except WaitFor as exc:
            ctx.store.finish_run(item.run_id, "failed", error=f"wait: {exc.reason}: {exc.detail}")
            raise Stop("waiting", exc.reason, exc.detail) from None
        except Fatal as exc:
            ctx.store.finish_run(item.run_id, "failed", error=f"fatal: {studio_config.redact(str(exc))}"[:2000])
            raise Stop("failed", None, f"{step.name} {scene}: {studio_config.redact(str(exc))}") from None
        except Exception as exc:  # lỗi lập trình: task failed, giữ traceback trong StepRun
            ctx.store.finish_run(item.run_id, "failed", error=studio_config.redact(traceback.format_exc())[-4000:])
            raise Stop("failed", None, f"{step.name} {scene}: {type(exc).__name__}: {studio_config.redact(str(exc))}") from None
    return "exhausted", None, None


STEPS = {s.name: s for s in VIDEO_STEPS if isinstance(s, Step)}


def _reselect_cached_clip(ctx: Ctx, scene: str) -> None:
    """Clip là đầu ra dẫn xuất của ảnh + giọng selected: dùng lại clip đã dựng thì cũng chọn lại nó."""
    run = ctx.store.done_run(ctx.task_id, "animate", scene, input_hash(_inputs(ctx, STEPS["animate"], scene)))
    if not run:
        return
    wanted = ctx.td.candidate_id("clip", scene, run["attempt"])
    selected = ctx.td.selected(scene, "clip")
    if selected and selected.id != wanted and selected.provider == animate.PROVIDER:
        ctx.td.select(scene, wanted)


def scenes_for(ctx: Ctx, step: Step) -> list[str]:
    if not step.per_scene:
        return ["_"] if step.module.applies(ctx, "_") else []
    return [scene for scene in ctx.scene_ids() if step.module.applies(ctx, scene)]


def run_step(ctx: Ctx, step: Step, sleep: Callable[[float], None] = time.sleep) -> dict[str, list[str]]:
    """Một bước: các scene chạy trong thread pool giới hạn theo adapter; luồng chính commit."""
    outcome: dict[str, list[str]] = {"skip": [], "done": [], "unknown": [], "exhausted": []}
    items = scenes_for(ctx, step)
    if not items:
        return outcome
    workers = int(ctx.config["concurrency"].get(step.pool, 1)) if step.pool else 1
    stop: Stop | None = None
    paused = False
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix=f"{step.name}") as pool:
        pending: dict[Future, str] = {}
        queue = list(items)
        while queue or pending:
            while queue and len(pending) < max(1, workers) and stop is None and not paused:
                if ctx.store.pause_requested(ctx.task_id):
                    paused = True
                    break
                scene = queue.pop(0)
                pending[pool.submit(run_item, ctx, step, scene, sleep)] = scene
            if not pending:
                break
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                scene = pending.pop(future)
                try:
                    status, item, result = future.result()
                except Stop as exc:
                    stop = stop or exc
                    continue
                if status == "done":
                    _commit(ctx, step, item, result)
                elif status == "skip" and step.name == "animate":
                    _reselect_cached_clip(ctx, scene)
                outcome[status].append(scene)
    if stop:
        raise stop
    if paused:
        raise Stop("waiting", "paused", f"tạm dừng trước {step.name}")
    return outcome


# ---------------------------------------------------------------- task


def _recover(ctx: Ctx) -> None:
    """Sau khi worker chết: StepRun 'running' → unknown (trả phí, còn pending) hoặc failed."""
    orphan_ids = {event.request_id for event in costs.orphans(ctx.td.costs_path)}
    for run in ctx.store.runs(ctx.task_id):
        if run["status"] != "running":
            continue
        if run["job_id"]:
            continue                                     # motion: poll job cũ ở lượt chạy này
        request_id = f"{ctx.task_id}:{run['scene']}:{run['step']}:{run['attempt']}"
        if request_id in orphan_ids:
            costs.mark_unknown(ctx.td.costs_path, request_id, "worker dừng sau khi gửi lượt gọi trả phí")
            ctx.store.set_run_status(run["id"], "unknown", "worker dừng sau pending")
        else:
            ctx.store.set_run_status(run["id"], "failed", "worker dừng giữa chừng")
    for event in costs.orphans(ctx.td.costs_path):
        _, scene, step, attempt = event.request_id.rsplit(":", 3)
        run = ctx.store.run_by_request(ctx.task_id, step, scene, int(attempt))
        if run and run["job_id"]:
            continue
        costs.mark_unknown(ctx.td.costs_path, event.request_id, "pending mồ côi")
        if run and run["status"] != "unknown":
            ctx.store.set_run_status(run["id"], "unknown", "pending mồ côi")


def _total_voice(ctx: Ctx) -> float:
    return sum(float(ctx.td.selected(scene, "voice").duration_s or 0) for scene in ctx.scene_ids())


def run_task(store: Store, base_config: dict, task_id: str, sleep: Callable[[float], None] = time.sleep) -> str:
    """Chạy một task đã ở 'running' cho tới gate, chờ người, xong hoặc hỏng. Trả trạng thái mới."""
    try:
        ctx = make_ctx(store, base_config, task_id)
        _recover(ctx)
        for entry in VIDEO_STEPS:
            if isinstance(entry, Gate):
                if entry.name == "keyframes" and not ctx.config["gates"].get("keyframes", True):
                    continue
                if not gates.approved(ctx, entry.name):
                    raise Stop("waiting", f"gate:{entry.name}", gates.describe(ctx, entry.name))
                if entry.name == "final":
                    store.set_status(task_id, "completed")
                    return "completed"
                continue
            store.update_task(task_id, current_step=entry.name)
            outcome = run_step(ctx, entry, sleep)
            if outcome["unknown"]:
                raise Stop("waiting", "unknown_call", f"{entry.name}: lượt gọi không rõ kết quả ở {', '.join(outcome['unknown'])}")
            if outcome["exhausted"]:
                raise Stop("waiting", "retries_exhausted", f"{entry.name}: hết lượt thử ở {', '.join(outcome['exhausted'])}")
            if entry.name == "voice":
                total = _total_voice(ctx)
                if total > ctx.brief.max_s:
                    raise Stop("waiting", "duration_exceeded", f"tổng giọng {total:.2f}s > max_s {ctx.brief.max_s:g}s")
        store.set_status(task_id, "completed")
        return "completed"
    except Stop as stop:
        store.set_status(task_id, stop.status, stop.reason, stop.detail)
        tasklog(ctx, f"→ {stop.status} {stop.reason or ''}: {stop.detail}")
        return stop.status
    except Fatal as exc:
        store.set_status(task_id, "failed", None, studio_config.redact(str(exc)))
        return "failed"


def preflight(config: dict) -> list[str]:
    """Kiểm cấu hình trước lượt gọi trả phí đầu tiên (03 · Kiểm tra trước khi chạy)."""
    from studio import providers

    problems = []
    try:
        media.check_tools()
    except Fatal as exc:
        problems.append(str(exc))
    for capability in ("llm", "image", "tts", "subtitles", "renderer"):
        try:
            providers.get(capability, config)
        except Exception as exc:  # noqa: BLE001 — báo mọi lỗi khởi tạo
            problems.append(f"{capability}: {exc}")
    return problems


def worker(store: Store, config: dict, once: bool = False, task_id: str | None = None) -> None:
    from studio import series

    for requeued in store.requeue_running():
        log.info("%s: running → pending (worker khởi động lại)", requeued)
    series.recover_series_jobs(store, config)
    from studio.comfyui import service as comfy_service
    from studio.gpu import service as gpu_service

    guard_stop = threading.Event()
    guard = gpu_service.start_guard(store, config, stop=guard_stop) if not once and not task_id else None
    watch = (comfy_service.start_watch(store, config, gpu_service.from_config(store, config), stop=guard_stop)
             if not once and not task_id else None)
    try:
        while True:
            job = None if task_id else store.claim_series_job()
            if job:
                log.info("series %s: ảnh tham chiếu %s/%s → %s", job["series"], job["character"], job["view"],
                         series.run_series_job(store, config, job))
                continue
            claimed = (task_id if store.claim_one(task_id) else None) if task_id else store.claim()
            if claimed:
                status = run_task(store, config, claimed)
                log.info("%s: %s", claimed, status)
                continue
            if once:
                return
            time.sleep(float(config["worker"]["poll_s"]))
    finally:
        guard_stop.set()
        for thread in (guard, watch):
            if thread is not None:
                thread.join(timeout=1)
