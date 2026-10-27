"""Trang review (Gate 3): FastAPI + Jinja2 + HTMX, server render, không bước build frontend.

Web không gọi provider và không chạy bước: mọi hành động đi qua ``gates.Actions`` / ``series.SeriesActions``
(cùng lớp CLI dùng), và bị từ chối khi task đang ``running``. Trang GPU chỉ gọi ``studio.gpu.service``,
không import adapter GPU; POST của trang GPU có CSRF token và rental_id sinh trước làm nonce. Trang ComfyUI chỉ
gọi ``studio.comfyui.service`` (không cầm runtime ComfyUI); POST dùng cùng CSRF token, chạy workflow có ô xác nhận và
run_id sinh trước làm nonce.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
import os
import secrets
import tempfile
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from studio import config as studio_config
from studio import costs, providers
from studio.comfyui import service as comfy_service
from studio.contracts import Brief, CheckItem, Review
from studio.gates import ActionError, Actions
from studio.gpu import service as gpu_service
from studio.gpu.contracts import ATTENTION, BILLABLE, TRANSITIONS, OfferFilters, Quote
from studio.paths import contained
from studio.series import VIEWS, SeriesActions, SeriesDir, SeriesError, list_series
from studio.store import Store
from studio.web import views

HERE = Path(__file__).resolve().parent
CAPABILITIES = ("llm", "image", "tts", "subtitles", "video")


def safe_path(root: Path, relative: str) -> Path | None:
    """realpath + commonpath trong root (``studio.paths.contained``), và phải là file (07 · Đường dẫn file)."""
    target = contained(root, relative)
    return target if target is not None and target.is_file() else None


def create_app(config: dict | None = None) -> FastAPI:
    config = config or studio_config.load()
    data_dir = Path(config["data_dir"])
    store = Store(data_dir / "studio.db")
    app = FastAPI(title="Igniterra studio", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["tojson_pretty"] = lambda v: json.dumps(v, ensure_ascii=False, indent=2)
    templates.env.filters["utc_time"] = lambda epoch: datetime.fromtimestamp(float(epoch), timezone.utc).strftime(
        "%Y-%m-%d %H:%M")
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
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(url)
        query = urlencode([*parse_qsl(parts.query, keep_blank_values=True),
                           *((k, v) for k, v in (("msg", msg), ("err", err)) if v)])
        target = urlunsplit(parts._replace(query=query))
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
    def tasks_page(request: Request, q: str = "", status: str = "all"):
        tasks = [views.task_summary(store, data_dir, t) for t in store.tasks()]
        briefs = sorted((Path(config["root"]) / "briefs").glob("*.json"))
        groups = {"all": None, "review": {"waiting"}, "running": {"running", "pending"},
                  "done": {"completed"}, "failed": {"failed"}}
        status = status if status in groups else "all"
        visible = [t for t in tasks if (groups[status] is None or t["status"] in groups[status])
                   and q.casefold() in f"{t.get('title') or ''} {t['id']} {t.get('series') or ''}".casefold()]
        return page(request, "tasks.html", tasks=visible, all_tasks=tasks, q=q, status=status,
                    attention=[t for t in tasks if t["status"] in ("waiting", "failed")],
                    briefs=[b.name for b in briefs], series=list_series(data_dir))

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
        return page(request, "storyboard.html", **data, active=selected, views=VIEWS,
                    summary=views.task_summary(store, data_dir, data["task"]))

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
                    omniroute=cfg["omniroute"], gates=cfg["gates"], path=settings_path,
                    comfy_image=cfg.get("comfyui", {}).get("image", {}))

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        overlay = {"adapters": {c: form.get(f"adapter_{c}") for c in CAPABILITIES if form.get(f"adapter_{c}")},
                   "concurrency": {k: int(form.get(f"conc_{k}")) for k in ("image", "tts", "video", "animate") if form.get(f"conc_{k}")},
                   "omniroute": {k: form.get(k) for k in ("llm_model", "image_model") if form.get(k)},
                   "gates": {"keyframes": form.get("gate_keyframes") == "on"}}
        candidate = studio_config.deep_merge(current_config(), overlay)
        image_fields = {k: str(form[k]).strip() for k in ("rental_id", "workflow_id", "reference_workflow_id") if k in form}
        if image_fields:
            overlay["comfyui"] = {"image": image_fields}
            candidate = studio_config.deep_merge(candidate, overlay)
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

    # ------------------------------------------------------------ GPU Rentals (Gate 4, lát A–B)

    gpu = gpu_service.from_config(store, config)       # một service mỗi app; None khi [gpu] provider = "none"
    csrf = secrets.token_urlsafe(32)                    # theo process: giả định một uvicorn worker
    if gpu is not None:
        gpu.recover()           # G10: đối soát rental chưa rõ, báo orphan theo label; không tự xóa gì

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
        fixed = gpu.fixed_hosts() if gpu else []
        manifest = gpu.approved_manifest() if gpu else None
        return page(request, "gpu.html", gpu=gpu, rentals=rentals, fixed=fixed, machines=rentals + fixed,
                    provider_name=gpu.provider.capabilities().display_name if gpu else "",
                    filters=filters, offers=offers,
                    offer_error=offer_error, csrf=csrf, attention=ATTENTION, billable=BILLABLE, manifest=manifest,
                    manifest_view=gpu.manifest_view(manifest) if manifest else None,
                    new_rental_id=gpu.new_rental_id if gpu else None, orphans=gpu.orphans if gpu else [],
                    limits=gpu.limits if gpu else None, over_limits=gpu.over_limits() if gpu else [],
                    rental_costs={r.rental_id: gpu.cost(r.rental_id) for r in rentals} if gpu else {})

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

    def gpu_rental_view(request: Request, rental_id: str, **extra) -> Response:
        if gpu is None:
            return Response("GPU provider chưa bật", status_code=404)
        try:
            data = gpu.timeline(rental_id)
        except KeyError:
            return Response("không có rental", status_code=404)
        operations = data["operations"]
        return page(request, "gpu_rental.html", **data, csrf=csrf, attention=ATTENTION, billable=BILLABLE,
                    transitions=TRANSITIONS, supports_stop=gpu.supports_stop,
                    last_destroy=next((op for op in reversed(operations) if op["action"] == "destroy"), None),
                    stop_unknown=any(op["action"] == "stop" and op["status"] == "unknown" for op in operations),
                    **extra)

    @app.get("/gpu/instances/{rental_id}", response_class=HTMLResponse)
    def gpu_rental_page(request: Request, rental_id: str):
        return gpu_rental_view(request, rental_id)

    @app.get("/gpu/instances/{rental_id}/comfyui")
    def gpu_open_comfyui(request: Request, rental_id: str):
        """Nút "Mở ComfyUI": chuyển thẳng tới ComfyUI gốc trên máy (link trực tiếp, không proxy, không token)."""
        if gpu is None:
            return Response(status_code=404)
        try:
            target = gpu.comfy_url(rental_id)
        except (gpu_service.GPUServiceError, KeyError) as exc:
            return back(f"/gpu/instances/{rental_id}", err=str(exc)[:500], request=request)
        return RedirectResponse(target + "/", status_code=303)

    @app.get("/gpu/hosts/new", response_class=HTMLResponse)
    def gpu_host_form(request: Request):
        if gpu is None:
            return Response("GPU provider chưa bật", status_code=404)
        return page(request, "gpu_host.html", csrf=csrf, new_rental_id=gpu.new_rental_id(), form={}, check=None)

    @app.post("/gpu/hosts", response_class=HTMLResponse)
    async def gpu_host_add(request: Request):
        """Thêm máy cố định. ``intent=check`` chỉ kiểm kết nối (HTMX trả khối kết quả); lưu luôn kiểm lại trước."""
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        if gpu is None:
            return Response(status_code=404)
        fields = {key: str(form.get(key, "")).strip() for key in ("rental_id", "name", "url", "hourly")}
        token = str(form.get("token", ""))
        check, error = None, None
        try:
            hourly = float(fields["hourly"]) if fields["hourly"] else None
            if form.get("intent") == "check":
                check = gpu.check_fixed_host(fields["url"], token)
            else:
                rental = gpu.add_fixed_host(fields["rental_id"], fields["name"], fields["url"], token, hourly)
                return back(f"/gpu/instances/{rental.rental_id}", msg=f"Đã thêm {fields['name']}", request=request)
        except (gpu_service.GPUServiceError, ValueError) as exc:
            error = gpu.redact(str(exc).replace(token, "<token>") if token.strip() else str(exc))[:500]
        if request.headers.get("hx-request"):
            return page(request, "_gpu_host_check.html", check=check, check_error=error)
        return page(request, "gpu_host.html", csrf=csrf, new_rental_id=fields["rental_id"] or gpu.new_rental_id(),
                    form=fields, check=check, check_error=error)

    @app.post("/gpu/instances/{rental_id}/{action}")
    async def gpu_rental_action(request: Request, rental_id: str, action: str):
        """reconcile | stop | destroy | remove | login. Stop, destroy, remove cần ô xác nhận; tất cả cần CSRF token.

        ``remove`` gỡ máy cố định khỏi danh sách. ``login`` hiện user/mật khẩu ComfyUI của máy thuê ngay trong
        response (không redirect, không cache), để mật khẩu không nằm trong URL hay lịch sử trình duyệt.
        """
        if action not in ("reconcile", "stop", "destroy", "remove", "login"):
            return Response(status_code=404)
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        if gpu is None:
            return Response(status_code=404)
        url = f"/gpu/instances/{rental_id}"
        if action == "login":
            try:
                login = gpu.web_login(rental_id)
            except (gpu_service.GPUServiceError, KeyError) as exc:
                return back(url, err=str(exc)[:500], request=request)
            response = gpu_rental_view(request, rental_id, web_login=login)
            response.headers["Cache-Control"] = "no-store"
            return response
        if action != "reconcile" and form.get("confirm") != "on":
            return back(url, err=f"{action} cần xác nhận", request=request)
        try:
            if action in ("destroy", "remove"):
                # G11: còn run có output chưa tải/kiểm thì service từ chối, trừ khi đã tick ô bỏ output riêng.
                fn = gpu.destroy if action == "destroy" else gpu.remove_fixed_host
                rental = fn(rental_id, accept_unverified_output=form.get("accept_unverified") == "on")
            else:
                rental = getattr(gpu, action)(rental_id)
        except (gpu_service.GPUServiceError, KeyError) as exc:
            return back(url, err=str(exc)[:500], request=request)
        if action == "remove":
            return back("/gpu", msg=f"Đã gỡ {rental.offer.offer_id} khỏi danh sách", request=request)
        return back(url, msg=f"{rental_id}: {rental.status}", request=request)

    # ------------------------------------------------------------ ComfyUI Workflows (Gate 4, lát D)

    comfy = comfy_service.from_config(store, config, gpu)  # thư viện luôn bật; chạy cần GPU provider

    @app.get("/monitor", response_class=HTMLResponse)
    def monitor(request: Request, task: str | None = None):
        items = []
        for run in comfy.runs()[:100]:
            revision = comfy.revision(run.workflow_id, run.revision)
            origin = views.comfy_origin(revision.note)
            if task and (not origin or origin["task_id"] != task):
                continue
            items.append({"run": run, "name": revision.name, "origin": origin})
        rentals = [{"rental": r, "cost": gpu.cost(r.rental_id), "breaches": gpu.breaches(r),
                    "unverified": gpu.unverified_runs(r.rental_id)} for r in gpu.rentals()] if gpu else []
        tasks = [views.task_summary(store, data_dir, t) for t in store.tasks() if not task or t["id"] == task]
        return page(request, "monitor.html", items=items, rentals=rentals, tasks=tasks,
                    attention_tasks=[t for t in tasks if t["status"] in ("waiting", "failed")],
                    auto_refresh=None if request.query_params.get("refresh") == "off" else 5, task_filter=task)

    @app.post("/comfyui/image-profile")
    async def comfy_image_profile(request: Request):
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        if form.get("confirm") != "on":
            return back("/comfyui/workflows", err="xác nhận nhập profile trước", request=request)
        try:
            result = comfy.import_image_profile(HERE.parent.parent / "workflows" / "flux2-klein-4b")
        except (comfy_service.ComfyServiceError, ValueError, OSError) as exc:
            return back("/comfyui/workflows", err=str(exc)[:500], request=request)
        if all(result[k]["reused"] for k in ("text2image", "reference")):
            return back("/comfyui/workflows", msg="Mẫu đã có trong thư viện, không nhập thêm.", request=request)
        return back("/comfyui/workflows", msg=f"Đã nhập profile; manifest {result['manifest_sha256']} chờ duyệt. "
                    "Nhập ID hai workflow và rental ready vào Settings để dùng ComfyImage.", request=request)

    @app.get("/comfyui")
    def comfy_home():
        return RedirectResponse("/comfyui/workflows", status_code=303)

    templates.env.globals.update(kinds=comfy_service.KINDS, field_kinds=comfy_service.FIELD_KINDS,
                                 run_labels=views.RUN_LABELS, group_labels=views.GROUP_LABELS,
                                 machine_label=views.machine_label, usd=views.usd, limits=comfy_service.field_limits,
                                 limit_text=comfy_service.limit_text)

    @app.get("/comfyui/workflows", response_class=HTMLResponse)
    def comfy_library(request: Request, kind: str | None = None):
        """S14: một thư viện, lọc theo loại."""
        kind = kind if kind in comfy_service.KINDS else None
        names = {item["latest"].workflow_id: item["latest"].name for item in comfy.library()}
        return page(request, "comfyui.html", library=comfy.library(kind), kind=kind, csrf=csrf, names=names,
                    runs=comfy.runs()[:20], gpu_enabled=gpu is not None)

    @app.post("/comfyui/workflows")
    async def comfy_import(request: Request):
        """Bước 1 của nhập (GUI hoặc API JSON, kèm bản API tùy chọn): kiểm file, giữ tạm, sang S15; cần CSRF token."""
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        workflow_id = str(form.get("workflow_id", "")) or None
        url = f"/comfyui/workflows/{workflow_id}" if workflow_id else "/comfyui/workflows"
        source, api = form.get("file"), form.get("api_file")
        if source is None or not hasattr(source, "read"):
            return back(url, err="chọn file workflow JSON", request=request)
        raw = await source.read()
        api_raw = await api.read() if api is not None and hasattr(api, "read") and api.filename else None
        name = str(form.get("name", "")) or (source.filename or "").removesuffix(".json")
        try:
            stage_id = comfy.stage_import(name, raw, api_raw=api_raw, workflow_id=workflow_id)
        except comfy_service.ComfyServiceError as exc:
            return back(url, err=str(exc)[:500], request=request)
        return back(f"/comfyui/import/{stage_id}", request=request)

    @app.get("/comfyui/import/{stage_id}", response_class=HTMLResponse)
    def comfy_import_check(request: Request, stage_id: str):
        """S15: kết quả kiểm, loại đoán được, cảnh báo trùng trong cả thư viện."""
        try:
            view = comfy.staged(stage_id)
        except comfy_service.ComfyServiceError as exc:
            return back("/comfyui/workflows", err=str(exc)[:500], request=request)
        origin = view.get("origin") or ""
        back_url = (f"/gpu/instances/{origin.split()[3].rstrip(':')}/workflows" if origin.startswith("lưu từ máy ")
                    else f"/comfyui/workflows/{view['workflow_id']}" if view.get("workflow_id") else "/comfyui/workflows")
        return page(request, "comfyui_import.html", view=view, csrf=csrf, back_url=back_url)

    @app.post("/comfyui/import/{stage_id}")
    async def comfy_import_save(request: Request, stage_id: str):
        """Bước 2: lưu thành revision bất biến (save), lưu dù trùng (keep) hoặc hủy (cancel); cần CSRF token."""
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        action = str(form.get("action", "save"))
        if action == "cancel":
            comfy.discard_stage(stage_id)
            return back("/comfyui/workflows", msg="Đã hủy, không lưu gì", request=request)
        try:
            record = comfy.commit_stage(stage_id, name=str(form.get("name", "")) or None,
                                        kind=str(form.get("kind", "")) or None, note=str(form.get("note", "")) or None,
                                        keep_duplicate=action == "keep")
        except comfy_service.ComfyServiceError as exc:
            return back(f"/comfyui/import/{stage_id}", err=str(exc)[:500], request=request)
        return back(f"/comfyui/workflows/{record.workflow_id}?revision={record.revision}",
                    msg=f"Đã lưu {record.name} revision {record.revision}; không tốn phí", request=request)

    @app.get("/gpu/instances/{rental_id}/workflows", response_class=HTMLResponse)
    def machine_workflows(request: Request, rental_id: str):
        """S13: workflow đã Save trong ComfyUI gốc trên máy; chỉ GET tới máy, không tốn phí."""
        if gpu is None:
            return Response(status_code=404)
        try:
            rental = gpu.rental(rental_id)
            files = comfy.machine_workflows(rental_id)
        except KeyError:
            return Response("không có máy", status_code=404)
        except (comfy_service.ComfyServiceError, gpu_service.GPUServiceError) as exc:
            return back(f"/gpu/instances/{rental_id}", err=str(exc)[:500], request=request)
        return page(request, "gpu_workflows.html", rental=rental, files=files, csrf=csrf,
                    library=sorted(comfy.library(), key=lambda item: item["latest"].name.lower()))

    @app.post("/gpu/instances/{rental_id}/workflows/import")
    async def machine_workflow_import(request: Request, rental_id: str):
        """Lưu workflow đã chọn thành workflow mới hoặc revision mới (không ghi đè revision cũ); cần CSRF token."""
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        url = f"/gpu/instances/{rental_id}/workflows"
        target = str(form.get("target", "new"))
        try:
            stage_id = comfy.stage_from_machine(rental_id, str(form.get("path", "")),
                                                workflow_id=None if target == "new" else target)
        except (comfy_service.ComfyServiceError, gpu_service.GPUServiceError, KeyError) as exc:
            return back(url, err=str(exc)[:500], request=request)
        return back(f"/comfyui/import/{stage_id}", request=request)

    @app.get("/comfyui/workflows/{workflow_id}", response_class=HTMLResponse)
    def comfy_workflow_page(request: Request, workflow_id: str, revision: int | None = None, checked: str | None = None):
        """S16: bốn cổng, form chỉ gồm ô đã khai, chọn máy, ước tính, tự kiểm; dependency và duyệt (F04)."""
        try:
            record = comfy.revision(workflow_id, revision)
        except comfy_service.ComfyServiceError:
            return Response("không có workflow", status_code=404)
        manifest_view = comfy.manifest_view(record)
        machines = comfy.machine_options(workflow_id) if gpu is not None else []
        runs = comfy.runs(workflow_id=workflow_id)
        try:
            prompt = comfy.api_prompt(record) if record.runnable else {}
        except comfy_service.ComfyServiceError:
            prompt = {}
        defaults = {f["key"]: prompt.get(f["node"], {}).get("inputs", {}).get(f["input"]) for f in record.inputs}
        known = {m["rental"].rental_id for m in machines}
        return page(request, "comfyui_workflow.html", record=record, latest=comfy.revision(workflow_id),
                    revisions=comfy.revisions(workflow_id),
                    manifest=comfy.manifest(record.manifest_sha256) if record.manifest_sha256 else None,
                    manifest_view=manifest_view, runs=runs, machines=machines, defaults=defaults,
                    gates=views.workflow_gates(record, machines, runs, checked if checked in known else None),
                    license_warnings=comfy_service.license_warnings(manifest_view) if manifest_view else [],
                    characters=comfy.characters() if any(f["kind"] == "character" for f in record.inputs) else [],
                    seed_field=comfy_service.seed_field(record.inputs), seed_max=comfy_service.SEED_MAX,
                    max_attempts=comfy_service.MAX_ATTEMPTS, max_cap=comfy_service.MAX_CAP_USD,
                    judge_enabled=comfy.judge_enabled, autofix_open=record.kind == "fix",
                    new_run_id=comfy.new_run_id(), csrf=csrf, gpu_enabled=gpu is not None)

    @app.post("/comfyui/workflows/{workflow_id}/approve")
    async def comfy_approve(request: Request, workflow_id: str):
        """Duyệt manifest và input mapping trên web (F04); cần CSRF và ô đã đọc, license cần xác nhận riêng."""
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        revision = str(form.get("revision", ""))
        url = f"/comfyui/workflows/{workflow_id}?revision={revision}"
        if form.get("confirm") != "on":
            return back(url, err="đánh dấu đã đọc dependency trước khi duyệt", request=request)
        try:
            record = comfy.approve_revision(workflow_id, int(revision), accept_licenses=form.get("accept_licenses") == "on")
        except (comfy_service.ComfyServiceError, ValueError) as exc:
            return back(url, err=str(exc)[:500], request=request)
        return back(url, msg=f"Đã duyệt revision {record.revision}", request=request)

    @app.post("/comfyui/workflows/{workflow_id}/manifest")
    async def comfy_attach_manifest(request: Request, workflow_id: str):
        """Gắn manifest JSON khai tay cho revision ``needs_review``; vào chờ duyệt, chưa chạy được."""
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        revision = str(form.get("revision", ""))
        url = f"/comfyui/workflows/{workflow_id}?revision={revision}"
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return back(url, err="chọn file manifest JSON", request=request)
        raw = await upload.read()
        try:
            manifest = comfy_service.parse_manifest(raw[:1_000_000])
            comfy.attach_manifest(workflow_id, int(revision), manifest, note="khai trên web")
        except ValidationError as exc:
            return back(url, err=f"manifest không đọc được: {exc.errors()[0]['msg']}"[:500], request=request)
        except (comfy_service.ComfyServiceError, ValueError) as exc:
            return back(url, err=str(exc)[:500], request=request)
        return back(url + "#deps", msg="Đã gắn manifest; đọc rồi bấm Duyệt", request=request)

    @app.post("/comfyui/workflows/{workflow_id}/check")
    async def comfy_check_machine(request: Request, workflow_id: str):
        """Cổng 4: đọc /object_info của máy đã chọn (GET tới máy, không chạy gì)."""
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        revision, rental_id = str(form.get("revision", "")), str(form.get("rental_id", ""))
        url = f"/comfyui/workflows/{workflow_id}?revision={revision}"
        try:
            missing, invalid = comfy.check_machine(workflow_id, int(revision), rental_id)
        except (comfy_service.ComfyServiceError, ValueError) as exc:
            return back(url, err=str(exc)[:500], request=request)
        if missing or invalid:
            what = ("thiếu node: " + ", ".join(missing)) if missing else ("thiếu model hoặc giá trị: " + "; ".join(invalid[:5]))
            return back(url, err=f"Máy {rental_id} {what}"[:500], request=request)
        return back(url + f"&checked={rental_id}", msg=f"Máy {rental_id} có đủ node và model", request=request)

    @app.get("/comfyui/workflows/{workflow_id}/inputs", response_class=HTMLResponse)
    def comfy_inputs_page(request: Request, workflow_id: str, revision: int | None = None, extra: int = 0):
        """S17: khai ô cho form chạy; chọn node · input từ API JSON."""
        try:
            record = comfy.revision(workflow_id, revision)
            prompt = comfy.api_prompt(record)
        except comfy_service.ComfyServiceError as exc:
            return back(f"/comfyui/workflows/{workflow_id}", err=str(exc)[:500], request=request)
        extra = max(0, min(extra, comfy_service.MAX_FIELDS))
        blank = max(extra, 0 if record.inputs else 3) + 1
        rows = [*record.inputs, *({} for _ in range(min(blank, comfy_service.MAX_FIELDS - len(record.inputs))))]
        return page(request, "comfyui_inputs.html", record=record, rows=rows, extra=extra, csrf=csrf,
                    candidates=comfy_service.candidates(prompt), next_revision=comfy.revision(workflow_id).revision + 1)

    @app.post("/comfyui/workflows/{workflow_id}/inputs")
    async def comfy_inputs_save(request: Request, workflow_id: str):
        """Lưu mapping thành revision mới (cần duyệt lại); cần CSRF token."""
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        revision = str(form.get("revision", ""))
        url = f"/comfyui/workflows/{workflow_id}/inputs?revision={revision}"
        fields = []
        try:
            for i in range(1, comfy_service.MAX_FIELDS + 2):
                label = str(form.get(f"label_{i}", "")).strip()
                if not label:
                    continue
                kind = str(form.get(f"kind_{i}", ""))
                node, _, name = str(form.get(f"target_{i}", "")).partition("|")
                fields.append({"label": label, "kind": kind, "node": node, "input": name,
                               "required": form.get(f"required_{i}") == "on",
                               **comfy_service.parse_limit(kind, str(form.get(f"limit_{i}", "")))})
            record = comfy.save_inputs(workflow_id, int(revision), fields)
        except (comfy_service.InputError, comfy_service.ComfyServiceError, ValueError) as exc:
            return back(url, err=str(exc)[:500], request=request)
        return back(f"/comfyui/workflows/{workflow_id}?revision={record.revision}#deps",
                    msg=f"Đã lưu mapping thành revision {record.revision}; đọc rồi bấm Duyệt", request=request)

    @app.post("/comfyui/workflows/{workflow_id}/runs")
    async def comfy_start_run(request: Request, workflow_id: str):
        """Chạy revision đã duyệt với các ô đã khai, trên máy ``ready``; cần CSRF, ô xác nhận và run_id sinh trước
        (nonce). Bật tự kiểm thì mở nhóm lượt có trần chi phí."""
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        revision = str(form.get("revision", ""))
        url = f"/comfyui/workflows/{workflow_id}?revision={revision}"
        if form.get("confirm") != "on":
            return back(url, err="chạy workflow cần xác nhận (GPU đang tính phí)", request=request)
        try:
            record = comfy.revision(workflow_id, int(revision))
            values = {}
            for field in record.inputs:
                raw = form.get(f"in_{field['key']}")
                if field["kind"] == "image":
                    data = await raw.read() if raw is not None and hasattr(raw, "read") and raw.filename else b""
                    if data:
                        values[field["key"]] = comfy.save_input_image(data)
                elif raw is not None:
                    values[field["key"]] = str(raw)
            run_id, rental_id = str(form.get("run_id", "")), str(form.get("rental_id", ""))
            if form.get("autofix") == "on":
                group = comfy.start_group(run_id, workflow_id, record.revision, rental_id, values,
                                          judge=str(form.get("judge", "manual")),
                                          max_attempts=int(form.get("max_attempts") or 3),
                                          cap_usd=float(form.get("cap_usd") or 0.10))
                return back(f"/comfyui/groups/{group.group_id}", msg=f"Đã mở lượt 1/{group.max_attempts}", request=request)
            run = comfy.start_run(run_id, workflow_id, record.revision, rental_id, values)
        except (comfy_service.ComfyServiceError, gpu_service.GPUServiceError, ValueError) as exc:
            return back(url, err=str(exc)[:500], request=request)
        return back(f"/comfyui/runs/{run.run_id}", msg=f"{run.run_id}: {views.RUN_LABELS.get(run.status, run.status)}",
                    request=request)

    @app.get("/comfyui/groups/{group_id}", response_class=HTMLResponse)
    def comfy_group_page(request: Request, group_id: str):
        """S18: các lượt, chi phí đã dùng trên trần, lý do dừng, chọn kết quả."""
        try:
            group = comfy.group(group_id)
        except comfy_service.ComfyServiceError:
            return Response("không có nhóm lượt", status_code=404)
        record = comfy.revision(group.workflow_id, group.revision)
        seed = comfy_service.seed_field(record.inputs)
        rental = gpu.rental(group.rental_id) if gpu is not None else None
        runs = comfy.group_runs(group_id)
        attempts = [{"run": r, "image": comfy.verified_output(r.run_id, "image"),
                     "seed": r.inputs.get(seed["key"]) if seed else None,
                     "usd": (rental.offer.dph_total if rental else 0) * (r.gpu_seconds or 0) / 3600} for r in runs]
        return page(request, "comfyui_group.html", group=group, record=record, attempts=attempts, runs=runs, csrf=csrf,
                    machine=views.machine_label(rental) if rental else group.rental_id,
                    chosen=next((r for r in runs if r.run_id == group.chosen_run_id), None),
                    gpu_seconds=sum(r.gpu_seconds or 0 for r in runs),
                    auto_refresh=10 if group.status == "running" else None)

    @app.post("/comfyui/groups/{group_id}/{action}")
    async def comfy_group_action(request: Request, group_id: str, action: str):
        """refresh (một lượt poll của nhóm) | review (tự duyệt lượt mới nhất) | choose | stop; cần CSRF token."""
        if action not in ("refresh", "review", "choose", "stop"):
            return Response(status_code=404)
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        url = f"/comfyui/groups/{group_id}"
        run_id = str(form.get("run_id", ""))
        try:
            if action == "refresh":
                for run in comfy.group_runs(group_id):
                    comfy.refresh(run.run_id)
                group = comfy.advance(group_id)
            elif action == "review":
                group = comfy.review(group_id, run_id, form.get("verdict") == "pass", str(form.get("note", "")))
            elif action == "choose":
                group = comfy.choose(group_id, run_id)
            else:
                group = comfy.stop_group(group_id)
        except (comfy_service.ComfyServiceError, gpu_service.GPUServiceError) as exc:
            return back(url, err=str(exc)[:500], request=request)
        return back(url, msg=views.GROUP_LABELS[group.status], request=request)

    @app.get("/comfyui/runs/{run_id}", response_class=HTMLResponse)
    def comfy_run_page(request: Request, run_id: str):
        try:
            data = comfy.run_detail(run_id)
        except comfy_service.ComfyServiceError:
            return Response("không có run", status_code=404)
        origin = views.comfy_origin(data["revision"].note)
        audio = comfy.verified_output(run_id, "audio")
        shown = views.shown_inputs(data["revision"], data["run"])
        text = " ".join(f["value"] for f in shown if f["kind"] == "text")
        return page(request, "comfyui_run.html", **data, csrf=csrf, origin=origin, audio=audio, shown_inputs=shown,
                    audio_info=views.audio_info(data_dir / audio.path, audio.media_type) if audio else None,
                    words=len(text.split()) if audio else 0,
                    scenes=views.scene_targets(store, data_dir) if audio else [],
                    auto_refresh=5 if data["run"].status in ("checking", "submitting", "submit_unknown", "queued",
                                                           "running", "downloading") else None)

    @app.post("/comfyui/runs/{run_id}/{action}")
    async def comfy_run_action(request: Request, run_id: str, action: str):
        """refresh (một lượt poll) | retry-download | use (giọng đọc cho scene); cần CSRF token."""
        handlers = {"refresh": comfy.refresh, "retry-download": comfy.retry_download}
        if action not in (*handlers, "use"):
            return Response(status_code=404)
        form = await request.form()
        if not gpu_csrf_ok(form):
            return Response("CSRF token sai; tải lại trang", status_code=403)
        if action == "use":
            return comfy_run_use(request, run_id, form)
        url = f"/comfyui/runs/{run_id}"
        try:
            run = handlers[action](run_id)
            if run.group_id:
                comfy.advance(run.group_id)
        except (comfy_service.ComfyServiceError, gpu_service.GPUServiceError) as exc:
            return back(url, err=str(exc)[:500], request=request)
        return back(url, msg=f"{run_id}: {run.status}", request=request)

    def comfy_run_use(request: Request, run_id: str, form) -> Response:
        """S19: gắn giọng đọc đã kiểm SHA-256 vào một scene (ứng viên voice của Storyboard)."""
        url = f"/comfyui/runs/{run_id}"
        task_id, _, scene = str(form.get("target", "")).partition("/")
        audio = comfy.verified_output(run_id, "audio")
        if audio is None:
            return back(url, err="chưa có giọng đọc đã kiểm SHA-256 để dùng", request=request)
        path = safe_path(data_dir, audio.path)
        if path is None or comfy_service.file_sha256(path) != audio.sha256:
            return back(url, err="file giọng đọc không khớp SHA-256 đã kiểm", request=request)
        try:
            candidate = actions().upload(task_id, scene, "voice", path, f"comfyui:{run_id}",
                                         f"tự tạo bằng ComfyUI, run {run_id}", True)
        except (ActionError, ValueError, KeyError, OSError) as exc:
            return back(url, err=str(exc)[:500], request=request)
        return back(f"/tasks/{task_id}?scene={scene}", msg=f"Đã thêm giọng đọc {candidate} cho {scene}", request=request)

    # ------------------------------------------------------------ files

    @app.get("/files/{relative:path}")
    def files(relative: str):
        path = safe_path(data_dir, relative)
        # data/gpu/<rental_id>/ giữ WEB_PASSWORD từng rental (09 · s7.b2): không bao giờ phục vụ qua HTTP.
        if path is None or os.path.commonpath([os.path.realpath(data_dir / "gpu"), str(path)]) == os.path.realpath(
                data_dir / "gpu"):
            return Response(status_code=404)
        return FileResponse(path)

    app.state.store = store
    app.state.gpu = gpu
    app.state.comfy = comfy
    return app

