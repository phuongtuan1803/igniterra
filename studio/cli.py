"""CLI Gate 2: ``python -m studio <lệnh>`` (06 · CLI). Không có logic riêng: gọi pipeline và gates.Actions."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from studio import config as studio_config
from studio import costs, pipeline
from studio.contracts import Brief, Review
from studio.gates import ActionError, Actions
from studio.series import SeriesError
from studio.store import Store
from studio.taskdir import TaskDir


def _store(config: dict) -> Store:
    return Store(Path(config["data_dir"]) / "studio.db")


def cmd_task_new(args, config, store):
    brief = Brief.model_validate_json(Path(args.brief).read_text(encoding="utf-8"))
    overrides = json.loads(args.overrides) if args.overrides else {}
    task_id = Actions(store, config).create_task(brief, args.series, overrides)
    print(task_id)


def cmd_task_show(args, config, store):
    task = store.task(args.task)
    td = TaskDir(Path(config["data_dir"]), args.task)
    print(f"{task['id']} · {task['brief']['title']} · {task['status']}"
          + (f" ({task['wait_reason']})" if task["wait_reason"] else "")
          + f" · plan rev {task['plan_rev']} · bước {task['current_step'] or '-'}")
    if task["wait_detail"]:
        print(f"  {task['wait_detail']}")
    assets = td.assets()
    for scene, kinds in assets.items():
        parts = []
        for kind, slot in kinds.items():
            parts.append(f"{kind}={slot['selected']} ({len(slot['candidates'])})")
        print(f"  {scene}: " + ", ".join(parts))
    for rev in td.render_revisions():
        manifest = json.loads((td.root / "renders" / rev / "manifest.json").read_text(encoding="utf-8"))
        print(f"  render {rev}: {manifest['duration_s']}s {manifest['width']}x{manifest['height']} → {td.root / 'renders' / rev / 'final_9x16.mp4'}")
    usd, missing = costs.total(td.costs_path)
    results = [e for e in costs.read(td.costs_path) if e.status != "pending"]
    counts = {status: sum(e.status == status for e in results) for status in ("ok", "failed", "unknown")}
    price = f"${usd:.3f}" + (f" + {missing} lượt chưa có giá (đối chiếu dashboard provider)" if missing else "")
    print(f"  lượt gọi trả phí: {counts['ok']} ok, {counts['failed']} failed, {counts['unknown']} unknown · ước tính {price}")
    for orphan in costs.orphans(td.costs_path):
        print(f"  ⚠ pending mồ côi: {orphan.request_id}")
    if args.runs:
        for run in store.runs(args.task):
            print(f"    {run['step']:<10} {run['scene']:<4} a{run['attempt']} {run['status']:<8} {run['output_path'] or ''} {(run['error'] or '')[:160]}")


def cmd_task_list(args, config, store):
    for task in store.tasks():
        print(f"{task['id']}  {task['status']:<10} {task['wait_reason'] or '':<18} {task['current_step'] or ''}")


def cmd_worker(args, config, store):
    if not args.skip_preflight:
        problems = pipeline.preflight(config)
        if problems:
            sys.exit("preflight trượt:\n  " + "\n  ".join(problems))
    pipeline.worker(store, config, once=args.once, task_id=args.task)


def cmd_gate(args, config, store):
    print(Actions(store, config).decide(args.task, args.decision, args.note))


def cmd_review(args, config, store):
    data = json.loads(Path(args.checklist).read_text(encoding="utf-8"))
    if args.minutes is not None:
        data["active_minutes"] = args.minutes
    review = Review.model_validate(data)
    render_rev, accepted = Actions(store, config).review(args.task, review)
    print(f"đã ghi phiếu duyệt {args.task} {render_rev}: accepted={accepted}")


def cmd_pause(args, config, store):
    print(Actions(store, config).pause(args.task))


def cmd_resume(args, config, store):
    Actions(store, config).resume(args.task)
    print(f"{args.task} → pending")


def cmd_cancel(args, config, store):
    Actions(store, config).cancel(args.task, args.note or "")
    print(f"{args.task} → failed")


def cmd_select(args, config, store):
    kind = Actions(store, config).select(args.task, args.scene, args.candidate)
    print(f"{args.task} {args.scene}: {kind} = {args.candidate}")


def cmd_scene_edit(args, config, store):
    revision = Actions(store, config).edit_scene(
        args.task, args.scene, note=args.note or "", narration=args.narration, visual=args.visual,
        size=args.size, angle=args.angle, movement=args.movement, mode=args.mode)
    print(f"{args.task}: ScenePlan rev {revision}")


def cmd_unknown(args, config, store):
    request_id = Actions(store, config).unknown(args.task, args.scene, args.decision, args.note)
    print(f"{request_id}: {args.decision}")


def cmd_rerun(args, config, store):
    Actions(store, config).rerun(args.task, args.scene, args.step, args.note)
    print(f"{args.task} {args.scene}: chạy lại {args.step}")


def cmd_upload(args, config, store):
    candidate = Actions(store, config).upload(args.task, args.scene, args.kind, Path(args.file), args.source,
                                              args.rights, args.ai_generated)
    print(f"{args.task} {args.scene}: {candidate} (selected)")


def cmd_run_step(args, config, store):
    ctx = pipeline.make_ctx(store, config, args.task)
    step = pipeline.STEPS[args.step]
    if args.scene:
        status, item, result = pipeline.run_item(ctx, step, args.scene)
        if status == "done":
            pipeline._commit(ctx, step, item, result)
        print(status)
    else:
        print(pipeline.run_step(ctx, step))


def cmd_web(args, config, store):
    import uvicorn

    from studio.web import create_app

    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="info")


def cmd_series(args, config, store):
    from studio.series import SeriesActions, SeriesDir, list_series

    sa = SeriesActions(store, config)
    if args.series_command == "list":
        print("\n".join(list_series(Path(config["data_dir"]))))
    elif args.series_command == "new":
        sd = sa.create_from_task(args.name, args.from_task) if args.from_task else sa.create(args.name)
        print(sd.bible_path)
    elif args.series_command == "show":
        sd = SeriesDir(Path(config["data_dir"]), args.name)
        print(sd.bible_path.read_text(encoding="utf-8"))
        for key in sd.bible().characters:
            print(f"# {key}: ứng viên " + ", ".join(sd.rel(p) for p in sd.candidates(key)))
        for job in store.series_jobs(args.name):
            print(f"# job {job['id']} {job['character']}/{job['view']} {job['status']} {job['output_path'] or ''} {job['error'] or ''}")
    elif args.series_command == "character":
        sa.set_character(args.name, args.key, args.description)
    elif args.series_command == "ref":
        print(f"job #{sa.request_ref(args.name, args.key, args.view, args.note)} (worker sẽ chạy)")
    elif args.series_command == "approve":
        print(sa.approve_refs(args.name, args.key, args.candidates).sheet)


def cmd_gpu(args, config, store):
    """Manifest provisioning (thêm, đọc, duyệt) và poll provisioning; chỉ gọi ``studio.gpu.service``."""
    from pydantic import ValidationError

    from studio.gpu import service as gpu_service
    from studio.gpu.provisioning import ProvisioningManifest

    gpu = gpu_service.from_config(store, config)
    if gpu is None:
        sys.exit("[gpu] provider = \"none\": bật provider trước")
    try:
        if args.gpu_command == "poll":
            result = gpu.tick()            # provisioning + guardrail + reconcile destroy, như luồng guard của worker
            for rental in result["polled"]:
                print(f"{rental.rental_id}  {rental.status}")
            _print_enforced(result["enforced"])
        elif args.gpu_command == "enforce-limits":
            enforced = gpu.enforce_limits()
            _print_enforced(enforced)
            if not enforced:
                print(f"không rental nào vượt giới hạn ({gpu.limits.model_dump()})")
        elif args.manifest_command == "add":
            try:
                manifest = ProvisioningManifest.model_validate_json(Path(args.file).read_text(encoding="utf-8"))
            except ValidationError as exc:
                sys.exit(f"manifest sai dạng: {exc}")
            record = gpu.propose_manifest(manifest, args.note)
            print(gpu.manifest_yaml(record.manifest_sha256))
            print(f"# {record.manifest_sha256} {record.status}; đọc kỹ rồi: gpu manifest approve {record.manifest_sha256}")
        elif args.manifest_command == "show":
            print(gpu.manifest_yaml(args.sha256))
        elif args.manifest_command == "approve":
            record = gpu.approve_manifest(args.sha256)
            print(f"{record.manifest_sha256} {record.status}")
        elif args.manifest_command == "list":
            for record in gpu.manifests():
                print(f"{record.manifest_sha256}  {record.status:<14} {record.note or ''}")
    except gpu_service.GPUServiceError as exc:
        sys.exit(f"lỗi: {exc}")


def _print_enforced(enforced) -> None:
    for item in enforced:
        kinds = ",".join(b.kind for b in item.breaches)
        line = f"{item.rental_id}  {item.status}  vượt [{kinds}] → {item.outcome}"
        if item.runs:
            line += f"  (run chưa kiểm output: {', '.join(item.runs)}; cần người vận hành)"
        print(line)


def cmd_comfyui(args, config, store):
    """Thư viện workflow (nhập, gắn và duyệt manifest), chạy, poll; chỉ gọi ``studio.comfyui.service``."""
    from pydantic import ValidationError

    from studio.comfyui import service as comfy_service
    from studio.comfyui.manifest import WorkflowManifest
    from studio.gpu import service as gpu_service

    comfy = comfy_service.from_config(store, config, gpu_service.from_config(store, config))
    try:
        command = args.comfyui_command
        if command == "import":
            record = comfy.import_workflow(args.name or Path(args.file).stem, Path(args.file).read_bytes(),
                                           api_raw=Path(args.api).read_bytes() if args.api else None,
                                           workflow_id=args.workflow_id, note=args.note)
            print(f"{record.workflow_id} rev {record.revision} {record.format} {record.status}")
            for gap in record.draft.get("gaps", []):
                print(f"  thiếu: {gap}")
        elif command == "list":
            for item in comfy.library():
                w = item["latest"]
                print(f"{w.workflow_id}  rev {w.revision}/{item['revisions']}  {w.status:<14} {w.format}  {w.name}")
        elif command == "show":
            record = comfy.revision(args.workflow_id, args.revision)
            print(json.dumps({**record.model_dump(mode="json"), "status": record.status}, ensure_ascii=False, indent=2))
            if record.manifest_sha256:
                print(comfy.manifest(record.manifest_sha256).manifest_json)
        elif command == "manifest" and args.manifest_command == "attach":
            try:
                manifest = WorkflowManifest.model_validate_json(Path(args.file).read_text(encoding="utf-8"))
            except ValidationError as exc:
                sys.exit(f"manifest sai dạng: {exc}")
            record = comfy.attach_manifest(args.workflow_id, args.revision, manifest, args.note)
            print(manifest.model_dump_json(indent=2))
            print(f"# {record.manifest_sha256} {record.status}; đọc kỹ rồi: comfyui manifest approve {record.manifest_sha256}")
        elif command == "manifest" and args.manifest_command == "approve":
            record = comfy.approve_manifest(args.sha256, accept_licenses=args.accept_licenses)
            print(f"{record.manifest_sha256} {record.status}")
        elif command == "run":
            run = comfy.start_run(comfy.new_run_id(), args.workflow_id, args.revision, args.rental_id)
            print(f"{run.run_id}  {run.status}  {run.error_code or ''} {run.detail or ''}")
        elif command == "poll":
            for run in comfy.poll():
                print(f"{run.run_id}  {run.status}  {run.error_code or ''}")
        elif command == "retry-download":
            run = comfy.retry_download(args.run_id)
            print(f"{run.run_id}  {run.status}  {run.error_code or ''}")
    except (comfy_service.ComfyServiceError, gpu_service.GPUServiceError) as exc:
        sys.exit(f"lỗi: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m studio")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    task = sub.add_parser("task").add_subparsers(dest="task_command", required=True)
    new = task.add_parser("new", help="tạo task pending từ brief.json")
    new.add_argument("--brief", required=True)
    new.add_argument("--series")
    new.add_argument("--overrides", help='JSON ghi đè cấu hình, ví dụ \'{"adapters": {"image": "fake"}}\'')
    new.set_defaults(func=cmd_task_new)
    show = task.add_parser("show")
    show.add_argument("task")
    show.add_argument("--runs", action="store_true")
    show.set_defaults(func=cmd_task_show)
    task.add_parser("list").set_defaults(func=cmd_task_list)
    for name, func in (("pause", cmd_pause), ("resume", cmd_resume)):
        p = task.add_parser(name)
        p.add_argument("task")
        p.set_defaults(func=func)
    cancel = task.add_parser("cancel")
    cancel.add_argument("task")
    cancel.add_argument("--note")
    cancel.set_defaults(func=cmd_cancel)

    worker = sub.add_parser("worker", help="chạy worker")
    worker.add_argument("--once", action="store_true", help="dừng khi hết task pending")
    worker.add_argument("--task", help="chỉ chạy task này")
    worker.add_argument("--skip-preflight", action="store_true")
    worker.set_defaults(func=cmd_worker)

    gate = sub.add_parser("gate")
    gate.add_argument("task")
    gate.add_argument("decision", choices=["approve", "reject"])
    gate.add_argument("--note")
    gate.set_defaults(func=cmd_gate)

    review = sub.add_parser("review", help="ghi phiếu duyệt C01–C09 cho bản render mới nhất")
    review.add_argument("task")
    review.add_argument("--checklist", required=True)
    review.add_argument("--minutes", type=float)
    review.set_defaults(func=cmd_review)

    select = sub.add_parser("select")
    select.add_argument("task")
    select.add_argument("scene")
    select.add_argument("candidate")
    select.set_defaults(func=cmd_select)

    scene = sub.add_parser("scene").add_subparsers(dest="scene_command", required=True)
    edit = scene.add_parser("edit")
    edit.add_argument("task")
    edit.add_argument("scene")
    for field in ("narration", "visual", "size", "angle", "movement", "note"):
        edit.add_argument(f"--{field}")
    edit.add_argument("--mode", choices=["still", "motion"])
    edit.set_defaults(func=cmd_scene_edit)

    unknown = sub.add_parser("unknown")
    unknown.add_argument("task")
    unknown.add_argument("scene")
    unknown.add_argument("decision", choices=["retry", "skip"])
    unknown.add_argument("--note")
    unknown.set_defaults(func=cmd_unknown)

    rerun = sub.add_parser("rerun", help="chạy lại một bước của scene kèm ghi chú vào prompt")
    rerun.add_argument("task")
    rerun.add_argument("scene")
    rerun.add_argument("--step", default="keyframes")
    rerun.add_argument("--note", required=True)
    rerun.set_defaults(func=cmd_rerun)

    upload = sub.add_parser("upload")
    upload.add_argument("task")
    upload.add_argument("scene")
    upload.add_argument("kind", choices=["image", "clip", "voice"])
    upload.add_argument("file")
    upload.add_argument("--source", required=True)
    upload.add_argument("--rights", required=True)
    upload.add_argument("--ai-generated", action="store_true")
    upload.set_defaults(func=cmd_upload)

    web = sub.add_parser("web", help="trang review (Gate 3)")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8600)
    web.set_defaults(func=cmd_web)

    series = sub.add_parser("series").add_subparsers(dest="series_command", required=True)
    series.add_parser("list")
    s_new = series.add_parser("new")
    s_new.add_argument("name")
    s_new.add_argument("--from-task")
    s_show = series.add_parser("show")
    s_show.add_argument("name")
    s_char = series.add_parser("character")
    s_char.add_argument("name")
    s_char.add_argument("key")
    s_char.add_argument("description")
    s_ref = series.add_parser("ref", help="xếp job sinh ảnh tham chiếu (worker chạy, trả phí)")
    s_ref.add_argument("name")
    s_ref.add_argument("key")
    s_ref.add_argument("view", choices=["front", "side", "three_quarter"])
    s_ref.add_argument("--note")
    s_approve = series.add_parser("approve", help="duyệt ảnh ứng viên → refs + character sheet")
    s_approve.add_argument("name")
    s_approve.add_argument("key")
    s_approve.add_argument("candidates", nargs="+")
    for p in series.choices.values():
        p.set_defaults(func=cmd_series)

    gpu = sub.add_parser("gpu", help="GPU thuê (Gate 4): manifest provisioning, poll").add_subparsers(
        dest="gpu_command", required=True)
    gpu.add_parser("poll", help="một lượt: kiểm provisioning, guardrail giới hạn, reconcile destroy").set_defaults(
        func=cmd_gpu)
    gpu.add_parser("enforce-limits", help="chỉ guardrail: destroy rental vượt idle TTL/thời gian/chi phí "
                                          "(không destroy khi còn output chưa kiểm)").set_defaults(func=cmd_gpu)
    manifest = gpu.add_parser("manifest", help="manifest provisioning đã duyệt").add_subparsers(
        dest="manifest_command", required=True)
    m_add = manifest.add_parser("add", help="thêm manifest JSON (pending_review) và in YAML để đọc")
    m_add.add_argument("file")
    m_add.add_argument("--note")
    for name in ("show", "approve"):
        manifest.add_parser(name).add_argument("sha256")
    manifest.add_parser("list")
    for p in manifest.choices.values():
        p.set_defaults(func=cmd_gpu)

    comfyui = sub.add_parser("comfyui", help="ComfyUI workflow (Gate 4): thư viện, manifest, chạy, poll").add_subparsers(
        dest="comfyui_command", required=True)
    c_import = comfyui.add_parser("import", help="nhập workflow JSON thành revision mới")
    c_import.add_argument("file")
    c_import.add_argument("--api", help="bản Export (API) kèm theo khi FILE là GUI JSON")
    c_import.add_argument("--name")
    c_import.add_argument("--workflow-id", help="thêm revision cho workflow đã có")
    c_import.add_argument("--note")
    comfyui.add_parser("list")
    c_show = comfyui.add_parser("show")
    c_show.add_argument("workflow_id")
    c_show.add_argument("--revision", type=int)
    c_run = comfyui.add_parser("run", help="chạy revision đã duyệt trên rental ready (GPU đang tính phí)")
    c_run.add_argument("workflow_id")
    c_run.add_argument("revision", type=int)
    c_run.add_argument("rental_id")
    comfyui.add_parser("poll", help="theo dõi các run chưa xong một lượt")
    comfyui.add_parser("retry-download").add_argument("run_id")
    c_manifest = comfyui.add_parser("manifest", help="manifest dependency của workflow").add_subparsers(
        dest="manifest_command", required=True)
    c_attach = c_manifest.add_parser("attach", help="gắn manifest JSON (pending_review) vào một revision")
    c_attach.add_argument("workflow_id")
    c_attach.add_argument("revision", type=int)
    c_attach.add_argument("file")
    c_attach.add_argument("--note")
    c_approve = c_manifest.add_parser("approve")
    c_approve.add_argument("sha256")
    c_approve.add_argument("--accept-licenses", action="store_true", help="xác nhận license ngoài danh sách thương mại")
    for p in [*comfyui.choices.values(), *c_manifest.choices.values()]:
        p.set_defaults(func=cmd_comfyui)

    run_step = sub.add_parser("run-step", help="chạy một bước khi debug")
    run_step.add_argument("step", choices=sorted(pipeline.STEPS))
    run_step.add_argument("--task", required=True)
    run_step.add_argument("--scene")
    run_step.set_defaults(func=cmd_run_step)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = studio_config.load()
    store = _store(config)
    try:
        args.func(args, config, store)
    except (ActionError, KeyError, SeriesError) as exc:
        print(f"lỗi: {exc}", file=sys.stderr)
        return 1
    return 0
