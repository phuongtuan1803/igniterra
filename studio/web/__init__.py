"""Trang review (Gate 3): FastAPI + Jinja2 + HTMX, server render, không bước build frontend.

Web không gọi provider và không chạy bước: mọi hành động đi qua ``gates.Actions`` / ``series.SeriesActions``
(cùng lớp CLI dùng), và bị từ chối khi task đang ``running``. Trang GPU chỉ gọi ``studio.gpu.service``,
không import adapter GPU; POST của trang GPU có CSRF token và rental_id sinh trước làm nonce.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import tempfile
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from studio import config as studio_config
from studio import costs, providers
from studio.contracts import Brief, CheckItem, Review
from studio.gates import ActionError, Actions
from studio.gpu import service as gpu_service
from studio.gpu.contracts import ATTENTION, BILLABLE, OfferFilters, Quote
from studio.series import VIEWS, SeriesActions, SeriesDir, SeriesError, list_series
from studio.store import Store
from studio.web import views

HERE = Path(__file__).resolve().parent
CAPABILITIES = ("llm", "image", "tts", "subtitles", "video")


def safe_path(root: Path, relative: str) -> Path | None:
    """realpath + commonpath trong root; không so tiền tố chuỗi (07 · Đường dẫn file)."""
    base = os.path.realpath(root)
    target = os.path.realpath(os.path.join(base, relative))
    if os.path.commonpath([base, target]) != base or not os.path.isfile(target):
        return None
    return Path(target)


def create_app(config: dict | None = None) -> FastAPI:
    config = config or studio_config.load()
    data_dir = Path(config["data_dir"])
    store = Store(data_dir / "studio.db")
    app = FastAPI(title="Igniterra studio", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, ensure_ascii=False, indent=2)
    auth = os.environ.get("STUDIO_BASIC_AUTH", "")

    def actions() -> Actions:
        return Actions(store, current_config())

    def current_config() -> dict:
        return studio_config.load(root=Path(config["root"]), path=Path(config["config_path"]), data_dir=config["data_dir"])

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if auth:
            header = request.headers.get("authorization", "")
            ok = False
            if header.startswith("Basic "):
                try:
                    ok = secrets.compare_digest(base64.b64decode(header[6:]).decode(), auth)
                except (binascii.Error, UnicodeDecodeError):
                    ok = False
            if not ok:
                return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="studio"'})
        return await call_next(request)

    def page(request: Request, template: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, template, {"flash": request.query_params.get("msg"),
                                                          "error": request.query_params.get("err"), **context})

    def back(url: str, msg: str | None = None, err: str | None = None, request: Request | None = None) -> Response:
        from urllib.parse import urlencode

        query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
        target = url + (("?" + query) if query else "")
        if request is not None and request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    def act(url: str, request: Request, fn, *args, ok: str = "đã lưu", **kwargs) -> Response:
        try:
            result = fn(*args, **kwargs)
        except (ActionError, SeriesError, ValueError, KeyError) as exc:
            return back(url, err=str(exc), request=request)
        return back(url, msg=result if isinstance(result, str) and result else ok, request=request)

    # ------------------------------------------------------------ Tasks

    @app.get("/", response_class=HTMLResponse)
    def tasks_page(request: Request):
        tasks = [views.task_summary(store, data_dir, t) for t in store.tasks()]
        briefs = sorted((Path(config["root"]) / "briefs").glob("*.json"))
        return page(request, "tasks.html", tasks=tasks, briefs=[b.name for b in briefs], series=list_series(data_dir))

    @app.post("/tasks")
    def create_task(request: Request, brief_file: str = Form(""), brief_json: str = Form(""), series: str = Form(""),
                    overrides: str = Form("")):
        try:
            if brief_json.strip():
                brief = Brief.model_validate_json(brief_json)
            else:
                path = safe_path(Path(config["root"]) / "briefs", brief_file)
                if path is None:
                    raise ValueError("chọn brief hoặc dán JSON")
                brief = Brief.model_validate_json(path.read_text(encoding="utf-8"))
            task_id = actions().create_task(brief, series or None, json.loads(overrides) if overrides.strip() else {})
        except (ValueError, ActionError) as exc:
            return back("/", err=str(exc)[:500])
        return back(f"/tasks/{task_id}", msg=f"đã tạo {task_id}")

    # ------------------------------------------------------------ Storyboard

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def storyboard_page(request: Request, task_id: str, scene: str | None = None):
        try:
            data = views.storyboard(store, current_config(), task_id)
        except KeyError:
            return Response("không có task", status_code=404)
        selected = scene or (data["scenes"][0]["scene"].id if data["scenes"] else None)
        return page(request, "storyboard.html", **data, active=selected, views=VIEWS)

    @app.get("/tasks/{task_id}/status", response_class=HTMLResponse)
    def status_partial(request: Request, task_id: str):
        return page(request, "_status.html", task=store.task(task_id))

    @app.post("/tasks/{task_id}/scenes/{scene}/edit")
    def edit_scene(request: Request, task_id: str, scene: str, narration: str = Form(None), visual: str = Form(None),
                   size: str = Form(None), angle: str = Form(None), movement: str = Form(None), mode: str = Form(None),
                   note: str = Form("")):
        current = views.storyboard(store, current_config(), task_id)["plan"].scene(scene)
        changes = {k: v for k, v in {"narration": narration, "visual": visual, "size": size, "angle": angle,
                                       "movement": movement, "mode": mode}.items()
                   if v is not None and v != (getattr(current.shot, k) if k in ("size", "angle", "movement") else getattr(current, k))}
        url = f"/tasks/{task_id}?scene={scene}"
        if not changes:
            return back(url, msg="không có thay đổi", request=request)
        return act(url, request, lambda: f"ScenePlan rev {actions().edit_scene(task_id, scene, note=note, **changes)}")

    @app.post("/tasks/{task_id}/scenes/{scene}/select")
    def select(request: Request, task_id: str, scene: str, candidate: str = Form(...)):
        return act(f"/tasks/{task_id}?scene={scene}", request,
                   lambda: f"đã chọn {candidate} ({actions().select(task_id, scene, candidate)})")

    @app.post("/tasks/{task_id}/scenes/{scene}/rerun")
    def rerun(request: Request, task_id: str, scene: str, note: str = Form(...), step: str = Form("keyframes")):
        return act(f"/tasks/{task_id}?scene={scene}", request, actions().rerun, task_id, scene, step, note,
                   ok=f"đã yêu cầu chạy lại {step} {scene}")

    @app.post("/tasks/{task_id}/scenes/{scene}/upload")
    async def upload(request: Request, task_id: str, scene: str, file: UploadFile, kind: str = Form("image"),
                     source: str = Form(...), rights: str = Form(...), ai_generated: str = Form("")):
        suffix = Path(file.filename or "upload").suffix.lower()
        if kind not in ("image", "clip", "voice") or suffix not in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".mp3", ".wav", ".m4a"}:
            return back(f"/tasks/{task_id}?scene={scene}", err="loại file không hỗ trợ", request=request)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / f"upload{suffix}"
            path.write_bytes(await file.read())
            return act(f"/tasks/{task_id}?scene={scene}", request,
                       lambda: f"đã thêm {actions().upload(task_id, scene, kind, path, source, rights, bool(ai_generated))}")

    @app.post("/tasks/{task_id}/gate")
    def gate(request: Request, task_id: str, decision: str = Form(...), note: str = Form("")):
        return act(f"/tasks/{task_id}", request, actions().decide, task_id, decision, note or None)

    @app.post("/tasks/{task_id}/review")
    async def review(request: Request, task_id: str):
        form = await request.form()
        try:
            checklist = {c: CheckItem(result=form.get(f"{c}_result", "unknown"), timestamp=form.get(f"{c}_ts") or None,
                                      note=form.get(f"{c}_note", "")) for c in views.CHECKLIST_TEXT}
            minutes = form.get("active_minutes")
            item = Review(checklist=checklist, active_minutes=float(minutes) if minutes else None,
                          reviewer=form.get("reviewer") or "Tuan", note=form.get("note", ""))
        except ValueError as exc:
            return back(f"/tasks/{task_id}", err=str(exc)[:300], request=request)
        return act(f"/tasks/{task_id}", request,
                   lambda: "đã ghi phiếu {} (accepted={})".format(*actions().review(task_id, item)))

    @app.post("/tasks/{task_id}/{action}")
    def task_action(request: Request, task_id: str, action: str, note: str = Form("")):
        handlers = {"pause": lambda: actions().pause(task_id), "resume": lambda: actions().resume(task_id),
                    "cancel": lambda: actions().cancel(task_id, note)}
        if action not in handlers:
            return Response(status_code=404)
        return act(f"/tasks/{task_id}", request, handlers[action], ok=action)

    # ------------------------------------------------------------ Task detail

    @app.get("/tasks/{task_id}/detail", response_class=HTMLResponse)
    def detail_page(request: Request, task_id: str):
        return page(request, "detail.html", **views.detail(store, current_config(), task_id))

    @app.post("/tasks/{task_id}/unknown/{scene}")
    def unknown(request: Request, task_id: str, scene: str, decision: str = Form(...), note: str = Form("")):
        return act(f"/tasks/{task_id}/detail", request, lambda: f"{actions().unknown(task_id, scene, decision, note or None)}: {decision}")

    # ------------------------------------------------------------ Series

    def series_actions() -> SeriesActions:
        return SeriesActions(store, current_config())

    @app.get("/series", response_class=HTMLResponse)
    def series_list(request: Request):
        return page(request, "series_list.html", series=list_series(data_dir),
                    tasks=[t for t in store.tasks() if store.task(t["id"])["plan_rev"]])

    @app.post("/series")
    def series_create(request: Request, name: str = Form(...), from_task: str = Form("")):
        sa = series_actions()
        return act(f"/series/{name}", request,
                   (lambda: sa.create_from_task(name, from_task)) if from_task else (lambda: sa.create(name)),
                   ok=f"đã tạo series {name}")

    @app.get("/series/{name}", response_class=HTMLResponse)
    def series_page(request: Request, name: str):
        try:
            sd = SeriesDir(data_dir, name)
            bible = sd.bible()
        except (SeriesError, FileNotFoundError):
            return Response("không có series", status_code=404)
        candidates = {key: [sd.rel(p) for p in sd.candidates(key)] for key in bible.characters}
        jobs = store.series_jobs(name)
        return page(request, "series.html", name=name, bible=bible, candidates=candidates, jobs=jobs, views=VIEWS,
                    costs=costs.read(sd.costs_path))

    @app.post("/series/{name}/update")
    def series_update(request: Request, name: str, style: str = Form(None), negative: str = Form(None),
                      lexicon: str = Form(None), voice_name: str = Form(None), voice_rate: float = Form(None),
                      voice_wps: float = Form(None)):
        def run():
            lex = None
            if lexicon is not None:
                lex = {}
                for line in lexicon.splitlines():
                    if "=" in line:
                        word, spoken = line.split("=", 1)
                        lex[word.strip()] = spoken.strip()
            series_actions().update(name, style=style, negative=negative, lexicon=lex, voice_name=voice_name,
                                    voice_rate=voice_rate, voice_wps=voice_wps)
        return act(f"/series/{name}", request, run)

    @app.post("/series/{name}/characters")
    def series_character(request: Request, name: str, key: str = Form(...), description: str = Form(...)):
        return act(f"/series/{name}", request, series_actions().set_character, name, key, description)

    @app.post("/series/{name}/characters/{key}/ref")
    def series_ref(request: Request, name: str, key: str, view: str = Form(...), note: str = Form("")):
        return act(f"/series/{name}", request, lambda: f"job #{series_actions().request_ref(name, key, view, note or None)}"
                   " đã vào hàng đợi; worker sẽ sinh ảnh")

    @app.post("/series/{name}/characters/{key}/upload")
    async def series_upload(request: Request, name: str, key: str, file: UploadFile, source: str = Form(...),
                            rights: str = Form(...)):
        suffix = Path(file.filename or "ref").suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            return back(f"/series/{name}", err="chỉ nhận ảnh", request=request)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / f"ref{suffix}"
            path.write_bytes(await file.read())
            return act(f"/series/{name}", request, series_actions().upload_ref, name, key, path, source, rights)

    @app.post("/series/{name}/characters/{key}/approve")
    async def series_approve(request: Request, name: str, key: str):
        chosen = [str(v) for v in (await request.form()).getlist("candidate")]
        return act(f"/series/{name}", request, lambda: "đã duyệt {} ảnh; sheet {}".format(
            len(chosen), series_actions().approve_refs(name, key, chosen).sheet))

    # ------------------------------------------------------------ Settings

    settings_path = data_dir / "settings.toml"

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        cfg = current_config()
        return page(request, "settings.html", adapters=cfg["adapters"], concurrency=cfg["concurrency"],
                    registry={c: sorted(providers.REGISTRY.get(c, {})) + ["none"] for c in CAPABILITIES},
                    omniroute=cfg["omniroute"], gates=cfg["gates"], path=settings_path)

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        overlay = {"adapters": {c: form.get(f"adapter_{c}") for c in CAPABILITIES if form.get(f"adapter_{c}")},
                   "concurrency": {k: int(form.get(f"conc_{k}")) for k in ("image", "tts", "video", "animate") if form.get(f"conc_{k}")},
                   "omniroute": {k: form.get(k) for k in ("llm_model", "image_model") if form.get(k)},
                   "gates": {"keyframes": form.get("gate_keyframes") == "on"}}
        candidate = studio_config.deep_merge(current_config(), overlay)
        problems = []
        for capability in CAPABILITIES:
            try:
                providers.get(capability, candidate)       # khởi tạo được; không gọi API
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{capability}: {exc}")
        if problems:
            return back("/settings", err="; ".join(problems)[:500], request=request)
        studio_config.write_settings(settings_path, overlay)
        return back("/settings", msg="đã lưu; task mới và worker khởi động lại dùng cấu hình này", request=request)

    # ------------------------------------------------------------ GPU Rentals (Gate 4, lát A)

    gpu = gpu_service.from_config(store, config)       # một service mỗi app; None khi [gpu] provider = "none"
    csrf = secrets.token_urlsafe(32)                    # theo process: giả định một uvicorn worker

    def gpu_filters(params) -> OfferFilters:
        """Bộ lọc từ query hoặc form; ô trống dùng mặc định của OfferFilters."""
        fields = {key: float(params[key]) for key in ("min_gpu_ram_gb", "disk_gb", "max_dph") if params.get(key)}
        return OfferFilters(**fields)

    def gpu_offers(params) -> tuple[OfferFilters, list | None, str | None]:
        try:
            filters = gpu_filters(params)
        except ValueError as exc:
            return OfferFilters(), None, f"bộ lọc không hợp lệ: {str(exc)[:200]}"
        if gpu is None or "search" not in params:
            return filters, None, None
        try:
            return filters, gpu.search_offers(filters), None
        except Exception as exc:  # noqa: BLE001 — lỗi provider hiện trên trang, không làm hỏng trang
            return filters, None, studio_config.redact(str(exc))[:300]

    def gpu_csrf_ok(form) -> bool:
        return secrets.compare_digest(str(form.get("csrf", "")), csrf)

    @app.get("/gpu", response_class=HTMLResponse)
    def gpu_page(request: Request):
        filters, offers, offer_error = gpu_offers(request.query_params)
        rentals = gpu.rentals() if gpu else []
        return page(request, "gpu.html", gpu=gpu, rentals=rentals, filters=filters, offers=offers,
                    offer_error=offer_error, csrf=csrf, attention=ATTENTION, billable=BILLABLE,
                    new_rental_id=gpu.new_rental_id if gpu else None)

    @app.get("/gpu/offers", response_class=HTMLResponse)
    def gpu_offers_partial(request: Request):
        filters, offers, offer_error = gpu_offers({**request.query_params, "search": "1"})
        return page(request, "_gpu_offers.html", gpu=gpu, filters=filters, offers=offers, offer_error=offer_error,
                    csrf=csrf, new_rental_id=gpu.new_rental_id if gpu else None)

    @app.post("/gpu/instances")
    async def gpu_rent(request: Request):
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        if gpu is None:
            return back("/gpu", err="GPU provider chưa bật", request=request)
        rental_id = str(form.get("rental_id", ""))
        try:
            quote = Quote(offer_id=str(form.get("offer_id", "")), dph_total=float(form.get("dph_total", "")),
                          quoted_at=str(form.get("quoted_at", "")), confirmed=form.get("confirm") == "on")
            rental = gpu.rent(rental_id, quote, gpu_filters(form))
        except (gpu_service.GPUServiceError, ValueError) as exc:
            return back("/gpu", err=str(exc)[:500], request=request)
        return back(f"/gpu/instances/{rental.rental_id}", msg=f"{rental.rental_id}: {rental.status}", request=request)

    @app.get("/gpu/instances/{rental_id}", response_class=HTMLResponse)
    def gpu_rental_page(request: Request, rental_id: str):
        if gpu is None:
            return Response("GPU provider chưa bật", status_code=404)
        try:
            data = gpu.timeline(rental_id)
        except KeyError:
            return Response("không có rental", status_code=404)
        return page(request, "gpu_rental.html", **data, csrf=csrf, attention=ATTENTION, billable=BILLABLE)

    @app.post("/gpu/instances/{rental_id}/reconcile")
    async def gpu_reconcile(request: Request, rental_id: str):
        if not gpu_csrf_ok(await request.form()):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        if gpu is None:
            return Response(status_code=404)
        url = f"/gpu/instances/{rental_id}"
        try:
            return back(url, msg=f"{rental_id}: {gpu.reconcile(rental_id).status}", request=request)
        except (gpu_service.GPUServiceError, KeyError) as exc:
            return back(url, err=str(exc)[:500], request=request)

    # ------------------------------------------------------------ files

    @app.get("/files/{relative:path}")
    def files(relative: str):
        path = safe_path(data_dir, relative)
        if path is None:
            return Response(status_code=404)
        return FileResponse(path)

    app.state.store = store
    app.state.gpu = gpu
    return app

