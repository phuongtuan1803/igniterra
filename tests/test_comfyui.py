"""Gate 4 lát D: thư viện workflow, chạy bằng ``prompt_id`` do client sinh, tải và kiểm output (09 · G06, G07 nửa
run, G08, G11).

ComfyUI giả là một ``ThreadingHTTPServer`` trên 127.0.0.1 sau lớp Bearer như Caddy của base-image:
``/object_info``, ``/prompt``, ``/queue``, ``/history``, ``/view``. Rental đi tới ``ready`` bằng ``FakeGPUProvider``
(provisioning đã có test riêng ở lát C); 0 USD, không gọi mạng ngoài.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from conftest import approve_gpu_manifest
from fastapi.testclient import TestClient

from studio.comfyui import runs as outputs
from studio.comfyui import runtime as comfy
from studio.comfyui import workflows
from studio.comfyui.manifest import WorkflowManifest, validate
from studio.comfyui.service import ComfyService, ComfyServiceError
from studio.gpu.adapters.fake import FakeGPUProvider
from studio.gpu.contracts import OfferFilters, Quote
from studio.gpu.service import GPUService, GPUServiceError, UnverifiedOutput
from studio.web import create_app

IMAGE = "example/comfyui@sha256:" + "0" * 64
PORTAL = {"scheme": "http", "timeout_s": 1}
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000") + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
CKPT = "flux-2-klein-4b.safetensors"
HF = "https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/resolve/" + "a" * 40 + "/" + CKPT
MODEL = {"url": HF, "dest": f"checkpoints/{CKPT}", "size_bytes": 8_000_000_000, "sha256": "b" * 64,
         "license": "Apache-2.0"}
NODE = {"repo": "https://github.com/example/ComfyUI-Example", "commit": "c" * 40, "dest": "ComfyUI-Example",
        "requirements": ["numpy>=1.26", "opencv-python-headless==4.10.0.84"]}
API_PROMPT = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
    "2": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 0}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["2", 0], "filename_prefix": "igniterra"}},
}
OBJECT_INFO = {
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [[CKPT, "other.safetensors"], {}]}}},
    "EmptyImage": {"input": {"required": {"width": ["INT", {}], "height": ["INT", {}]}}},
    "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING", {}]}}},
}
OUTPUT = {"filename": "igniterra_00001_.png", "subfolder": "", "type": "output"}


# ------------------------------------------------------------ ComfyUI giả


class Comfy:
    def __init__(self):
        self.token: str | None = None
        self.open = False                  # trả lời cả request không token (máy cố định trên LAN)
        self.object_info: dict = json.loads(json.dumps(OBJECT_INFO))
        self.prompt_mode = "ok"            # ok | slow_ok (nhận rồi mất response) | lost (mất cả request) | reject
        self.posts: list[dict] = []
        self.queue: dict[str, str] = {}    # prompt_id → running | pending
        self.history: dict[str, dict] = {}
        self.files: dict[str, tuple[bytes, str]] = {OUTPUT["filename"]: (PNG, "image/png")}
        self.view_mode = "ok"              # ok | error | truncate
        self.saved: dict[str, bytes] = {}  # workflow đã Save trong ComfyUI gốc (user/default/workflows)
        self.views = 0
        self.lock = threading.Lock()

    def finish(self, prompt_id: str, files: list[dict] | None = None, ok: bool = True) -> None:
        """Prompt chạy xong: rời /queue, vào /history với output."""
        self.queue.pop(prompt_id, None)
        stamp = 1_793_000_000_000
        self.history[prompt_id] = {
            "status": {"status_str": "success" if ok else "error", "completed": ok,
                       "messages": [["execution_start", {"prompt_id": prompt_id, "timestamp": stamp}],
                                    ["execution_success" if ok else "execution_error",
                                     {"prompt_id": prompt_id, "timestamp": stamp + 4000, "node_id": "3",
                                      **({} if ok else {"exception_message": "CUDA OOM"})}]]},
            "outputs": {"3": {"images": files if files is not None else [OUTPUT]}} if ok else {}}


STATE = Comfy()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_any()

    def do_POST(self):
        self.handle_any()

    def reply(self, status: int, body, content_type: str = "application/json", length: int | None = None) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(data) if length is None else length))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_any(self):
        url = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        authed = STATE.token is not None and self.headers.get("authorization") == f"Bearer {STATE.token}"
        if not authed and not STATE.open:
            return self.reply(401, {"error": "unauthorized"})
        if url.path == "/system_stats":
            return self.reply(200, {"system": {"comfyui_version": "0.3.99", "python_version": "3.12.7 (main)"},
                                    "devices": [{"name": "cuda:0 NVIDIA RTX A5000 : cudaMallocAsync", "type": "cuda",
                                                 "vram_total": 24 * 1024 ** 3}]})
        if url.path == "/object_info":
            return self.reply(200, STATE.object_info)
        if url.path == "/prompt" and self.command == "POST":
            body = json.loads(raw)
            prompt_id = body["prompt_id"]
            with STATE.lock:
                STATE.posts.append(body)
            if STATE.prompt_mode == "reject":
                return self.reply(400, {"error": "invalid prompt", "node_errors": {"1": "bad"}})
            if STATE.prompt_mode != "lost":
                STATE.queue[prompt_id] = "pending"
            if STATE.prompt_mode in ("slow_ok", "lost"):
                time.sleep(1.5)            # quá timeout của client: response mất
            return self.reply(200, {"prompt_id": prompt_id, "number": len(STATE.posts), "node_errors": {}})
        if url.path == "/history":
            keys = list(STATE.history)[-int(parse_qs(url.query).get("max_items", ["0"])[0] or 0):]
            return self.reply(200, {key: STATE.history[key] for key in keys})
        if url.path == "/api/userdata":
            return self.reply(200, [{"path": p, "size": len(b), "modified": 1_793_000_000 + i}
                                    for i, (p, b) in enumerate(STATE.saved.items())])
        if url.path.startswith("/api/userdata/"):
            name = unquote(url.path.removeprefix("/api/userdata/")).removeprefix("workflows/")
            if name not in STATE.saved:
                return self.reply(404, {"error": "not found"})
            return self.reply(200, STATE.saved[name])
        if url.path.startswith("/history/"):
            prompt_id = url.path.removeprefix("/history/")
            return self.reply(200, {prompt_id: STATE.history[prompt_id]} if prompt_id in STATE.history else {})
        if url.path == "/queue":
            return self.reply(200, {
                "queue_running": [[0, p, {}, {}, []] for p, s in STATE.queue.items() if s == "running"],
                "queue_pending": [[1, p, {}, {}, []] for p, s in STATE.queue.items() if s == "pending"]})
        if url.path == "/view":
            STATE.views += 1
            name = parse_qs(url.query).get("filename", [""])[0]
            if STATE.view_mode == "error":
                return self.reply(500, {"error": "boom"})
            if name not in STATE.files:
                return self.reply(404, {"error": "not found"})
            data, kind = STATE.files[name]
            if STATE.view_mode == "truncate":
                return self.reply(200, data[: len(data) // 2], kind, length=len(data))
            return self.reply(200, data, kind)
        return self.reply(404, {"error": "not found"})


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def fc(server):
    STATE.__init__()
    return STATE


@pytest.fixture
def fake_gpu():
    return FakeGPUProvider()


@pytest.fixture
def gpu(env, fake_gpu):
    return GPUService(env.store, fake_gpu, image=IMAGE, portal=PORTAL)


@pytest.fixture
def svc(env, gpu):
    return ComfyService(env.store, gpu)


def ready_rental(gpu: GPUService, fake_gpu: FakeGPUProvider, fc: Comfy, server: str) -> str:
    """Rental ``ready`` trỏ cổng ComfyUI về server giả (provisioning tới ready có test riêng ở lát C)."""
    approve_gpu_manifest(gpu.store)
    offer = next(o for o in fake_gpu.search_offers(OfferFilters()) if o.offer_id == "1002")
    rental = gpu.rent(gpu.new_rental_id(), Quote(offer_id=offer.offer_id, dph_total=offer.dph_total,
                                                 quoted_at=offer.quoted_at, confirmed=True), OfferFilters())
    fc.token = fake_gpu.specs[rental.provider_instance_id].env["WEB_PASSWORD"]
    fake_gpu.boot(rental.provider_instance_id, {"1111/tcp": server, "8188/tcp": server})
    assert gpu._move(gpu.rental(rental.rental_id), "ready")
    return rental.rental_id


def approved(svc: ComfyService, prompt: dict | None = None, manifest: WorkflowManifest | None = None) -> tuple[str, int]:
    """Workflow API đã nhập, gắn manifest và duyệt."""
    record = svc.import_workflow("flux", json.dumps(prompt or API_PROMPT).encode())
    record = svc.attach_manifest(record.workflow_id, record.revision, manifest or WorkflowManifest(models=[MODEL]))
    svc.approve_manifest(record.manifest_sha256)
    return record.workflow_id, record.revision


def start(svc, workflow_id, revision, rental_id):
    return svc.start_run(svc.new_run_id(), workflow_id, revision, rental_id)


def gui_workflow(nodes: list[dict]) -> bytes:
    return json.dumps({"last_node_id": len(nodes), "links": [], "nodes": nodes, "version": 0.4}).encode()


# ------------------------------------------------------------ G06: nhập, needs_review, không đoán


def test_g06_api_json_without_metadata_is_needs_review_and_cannot_run(env, svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    gpu_manifests_before = env.store.manifests()
    raw = json.dumps(API_PROMPT).encode()
    record = svc.import_workflow("flux api", raw)
    assert record.format == "api" and record.revision == 1 and record.runnable
    assert record.status == "needs_review" and record.manifest_sha256 is None
    assert record.source_sha256 == hashlib.sha256(raw).hexdigest() == record.api_sha256
    assert record.class_types == ["CheckpointLoaderSimple", "EmptyImage", "SaveImage"]
    assert any("không xác định được dependency" in gap for gap in record.draft["gaps"])
    assert record.draft["custom_nodes"] == [] and record.draft["models"] == []      # không đoán từ tên file
    assert record.draft["hints"] == [CKPT]
    # Không chạy, không cài: không có run, không POST, rental vẫn ready, manifest provisioning không đổi.
    with pytest.raises(ComfyServiceError, match="needs_review"):
        start(svc, record.workflow_id, record.revision, rental_id)
    assert fc.posts == [] and env.store.comfy_runs() == []
    assert gpu.rental(rental_id).status == "ready" and env.store.manifests() == gpu_manifests_before


def test_import_is_immutable_revision_and_same_content_is_not_duplicated(svc):
    first = svc.import_workflow("flux", json.dumps(API_PROMPT).encode())
    again = svc.import_workflow("flux", json.dumps(API_PROMPT).encode(), workflow_id=first.workflow_id)
    assert again.revision == first.revision == 1
    changed = {**API_PROMPT, "2": {**API_PROMPT["2"], "inputs": {**API_PROMPT["2"]["inputs"], "width": 128}}}
    second = svc.import_workflow("flux", json.dumps(changed).encode(), workflow_id=first.workflow_id)
    assert second.revision == 2 and second.source_sha256 != first.source_sha256
    assert svc.revision(first.workflow_id, 1).source_sha256 == first.source_sha256      # revision cũ không đổi
    assert [item["revisions"] for item in svc.library()] == [2]


@pytest.mark.parametrize("raw, reason", [
    (b"not json", "không phải JSON"),
    (b"[1, 2]", "JSON object"),
    (json.dumps({"1": {"inputs": {}}}).encode(), "không nhận ra dạng"),
    (json.dumps({"1": {"class_type": "SaveImage", "inputs": {"images": ["9", 0]}}}).encode(), "link tới node 9"),
])
def test_malformed_workflows_are_refused(svc, raw, reason):
    with pytest.raises(ComfyServiceError, match=reason):
        svc.import_workflow("x", raw)


def test_gui_json_dependencies_are_extracted_but_gaps_keep_it_needs_review(svc):
    nodes = [
        {"id": 1, "type": "CheckpointLoaderSimple",
         "properties": {"cnr_id": "comfy-core", "ver": "0.3.60",
                        "models": [{"name": CKPT, "url": HF, "directory": "checkpoints", "hash": "b" * 64,
                                    "hash_type": "SHA256"}]}},
        {"id": 2, "type": "ExampleNode", "properties": {"aux_id": "example/ComfyUI-Example", "ver": "c" * 40}},
        {"id": 3, "type": "KJNode", "properties": {"cnr_id": "comfyui-kjnodes", "ver": "1.1.0"}},
        {"id": 4, "type": "MysteryNode", "properties": {}},
        {"id": 5, "type": "Note", "properties": {}},
    ]
    record = svc.import_workflow("gui", gui_workflow(nodes))
    assert record.format == "gui" and not record.runnable and record.status == "needs_review"
    [node] = record.draft["custom_nodes"]
    assert node["repo"] == "https://github.com/example/ComfyUI-Example" and node["commit"] == "c" * 40
    [model] = record.draft["models"]
    assert model["url"] == HF and model["sha256"] == "b" * 64 and model["dest"] == f"checkpoints/{CKPT}"
    gaps = "\n".join(record.draft["gaps"])
    assert "requirements.txt" in gaps and "dung lượng và license" in gaps
    assert "comfyui-kjnodes" in gaps and "MysteryNode" in gaps and "Note" not in gaps
    with pytest.raises(ComfyServiceError, match="Export \\(API\\)"):
        svc._runnable(record)


def test_gui_json_with_only_core_nodes_gets_extracted_manifest_pending_review(svc):
    nodes = [{"id": 1, "type": "EmptyImage", "properties": {"cnr_id": "comfy-core", "ver": "0.3.60"}},
             {"id": 2, "type": "SaveImage", "properties": {"cnr_id": "comfy-core", "ver": "0.3.60"}}]
    api = {k: v for k, v in API_PROMPT.items() if k != "1"}
    record = svc.import_workflow("smoke", gui_workflow(nodes), api_raw=json.dumps(api).encode())
    assert record.draft["gaps"] == [] and record.manifest_source == "extracted"
    assert record.status == "pending_review" and record.runnable                    # vẫn phải duyệt
    assert record.class_types == ["EmptyImage", "SaveImage"]
    assert svc.approve_manifest(record.manifest_sha256).status == "approved"
    assert svc.revision(record.workflow_id).status == "approved"


# ------------------------------------------------------------ manifest: ghim nguồn, duyệt, kiểm lại lúc dùng


@pytest.mark.parametrize("change, reason", [
    ({"models": [{**MODEL, "url": HF.replace("a" * 40, "main")}]}, "resolve/<commit"),
    ({"models": [{**MODEL, "sha256": "abc"}]}, "SHA-256"),
    ({"models": [{**MODEL, "dest": "../../etc/passwd"}]}, "đích model"),
    ({"custom_nodes": [{**NODE, "commit": "main"}]}, "full SHA"),
    ({"custom_nodes": [{**NODE, "repo": "https://gitlab.com/x/y"}]}, "github.com"),
    ({"custom_nodes": [{**NODE, "requirements": ["--extra-index-url https://evil.example/simple"]}]}, "requirements"),
    ({"custom_nodes": [{**NODE, "requirements": ["torch @ https://evil.example/torch.whl"]}]}, "requirements"),
])
def test_workflow_manifest_requires_pinned_sources(change, reason):
    assert any(reason in p for p in validate(WorkflowManifest(**{"models": [MODEL], **change})))
    assert validate(WorkflowManifest(models=[MODEL], custom_nodes=[NODE])) == []


def test_non_commercial_license_needs_explicit_acceptance(svc):
    record = svc.import_workflow("flux", json.dumps(API_PROMPT).encode())
    manifest = WorkflowManifest(models=[{**MODEL, "license": "FLUX-1-dev-non-commercial"}])
    record = svc.attach_manifest(record.workflow_id, record.revision, manifest)
    with pytest.raises(ComfyServiceError, match="license"):
        svc.approve_manifest(record.manifest_sha256)
    assert svc.approve_manifest(record.manifest_sha256, accept_licenses=True).status == "approved"


def test_approved_revision_cannot_get_a_different_manifest(svc):
    workflow_id, revision = approved(svc)
    with pytest.raises(ComfyServiceError, match="revision mới"):
        svc.attach_manifest(workflow_id, revision, WorkflowManifest(models=[MODEL], custom_nodes=[NODE]))
    # Kể cả khi bên gọi giữ bản đọc trước lúc duyệt: điều kiện nằm trong câu UPDATE.
    stale = svc.revision(workflow_id, revision)
    other = WorkflowManifest(custom_nodes=[NODE])
    svc.store.add_comfy_manifest(other.sha256(), other.canonical_json())
    assert not svc.store.attach_comfy_manifest(workflow_id, revision, stale.version, other.sha256(), "operator")


def test_manifest_or_api_file_edited_in_sqlite_after_approval_is_refused(env, svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    workflow_id, revision = approved(svc)
    sha = svc.revision(workflow_id, revision).manifest_sha256
    tampered = json.loads(svc.manifest(sha).manifest_json)
    tampered["models"][0]["sha256"] = "f" * 64
    with sqlite3.connect(env.store.path) as db:
        db.execute("UPDATE comfy_manifests SET manifest_json=? WHERE manifest_sha256=?", (json.dumps(tampered), sha))
    with pytest.raises(ComfyServiceError, match="không khớp SHA-256"):
        start(svc, workflow_id, revision, rental_id)

    workflow_id, revision = approved(svc, manifest=WorkflowManifest(models=[MODEL], custom_nodes=[NODE]))
    with sqlite3.connect(env.store.path) as db:
        db.execute("UPDATE comfy_workflows SET api_json=? WHERE workflow_id=?",
                   (json.dumps({"1": {"class_type": "EvilNode", "inputs": {}}}), workflow_id))
    with pytest.raises(ComfyServiceError, match="file API không khớp SHA-256"):
        start(svc, workflow_id, revision, rental_id)
    assert fc.posts == [] and gpu.rental(rental_id).status == "ready"


# ------------------------------------------------------------ G07 (nửa run): /object_info trước khi submit


def test_g07_missing_class_type_blocks_run_and_records_which(env, svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    prompt = {**API_PROMPT, "4": {"class_type": "WanImageToVideo", "inputs": {}},
              "5": {"class_type": "ExampleNode", "inputs": {}}}
    workflow_id, revision = approved(svc, prompt)
    run = start(svc, workflow_id, revision, rental_id)
    assert run.status == "blocked" and run.error_code == "missing_class_type"
    assert "ExampleNode, WanImageToVideo" in run.detail and "không submit" in run.detail
    assert run.prompt_id is None and fc.posts == []
    assert gpu.rental(rental_id).status == "ready"              # máy được nhả cho run khác
    transitions = [e["transition"] for e in env.store.gpu_events(rental_id)]
    assert transitions[-2:] == ["ready→busy", "busy→ready"]


def test_g07_model_missing_on_runtime_blocks_run(svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    fc.object_info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"] = [["other.safetensors"], {}]
    workflow_id, revision = approved(svc)
    run = start(svc, workflow_id, revision, rental_id)
    assert run.status == "blocked" and run.error_code == "invalid_input" and CKPT in run.detail
    assert fc.posts == []


def test_check_object_info_reads_new_combo_format():
    info = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": ["COMBO", {"options": ["x.safetensors"]}]}}},
            "EmptyImage": {}, "SaveImage": {}}
    missing, invalid = workflows.check_object_info(API_PROMPT, info)
    assert missing == [] and len(invalid) == 1 and CKPT in invalid[0]


# ------------------------------------------------------------ chạy, theo dõi, output


def test_run_succeeds_only_after_output_is_downloaded_and_verified(env, svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    workflow_id, revision = approved(svc)
    run = start(svc, workflow_id, revision, rental_id)
    assert run.status == "queued" and gpu.rental(rental_id).status == "busy"
    [post] = fc.posts
    assert post["prompt_id"] == run.prompt_id and post["prompt"] == API_PROMPT
    assert post["client_id"] == f"igniterra:{run.run_id}"
    events = [e["transition"] for e in env.store.comfy_events(run.run_id)]
    assert events == ["checking", "checking→submitting", "submitting→queued"]     # prompt_id ghi trước POST

    fc.queue[run.prompt_id] = "running"
    run = svc.refresh(run.run_id)
    assert run.status == "running" and run.queue_state == "running" and run.last_event == "queue_running"

    fc.finish(run.prompt_id)
    run = svc.refresh(run.run_id)
    assert run.status == "succeeded" and run.current_node == "3" and run.last_event == "execution_success"
    assert gpu.rental(rental_id).status == "ready"
    [output] = svc.run_detail(run.run_id)["outputs"]
    target = env.store.path.parent / "comfyui" / "runs" / run.run_id / OUTPUT["filename"]
    assert output.status == "verified" and output.media_type == "image/png" and output.bytes == len(PNG)
    assert output.sha256 == hashlib.sha256(PNG).hexdigest() == hashlib.sha256(target.read_bytes()).hexdigest()
    assert output.path == f"comfyui/runs/{run.run_id}/{OUTPUT['filename']}"
    assert not list(target.parent.glob(".*.part"))
    assert svc.refresh(run.run_id).status == "succeeded" and len(fc.posts) == 1      # gọi lại không đổi gì
    assert gpu.unverified_runs(rental_id) == []


def test_one_run_per_instance(svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    workflow_id, revision = approved(svc)
    assert start(svc, workflow_id, revision, rental_id).status == "queued"
    with pytest.raises(ComfyServiceError, match="đang busy"):
        start(svc, workflow_id, revision, rental_id)
    assert len(fc.posts) == 1


def test_execution_error_fails_run_and_frees_rental(svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    run = start(svc, *approved(svc), rental_id)
    fc.finish(run.prompt_id, ok=False)
    run = svc.refresh(run.run_id)
    assert run.status == "failed" and run.error_code == "execution_error" and "CUDA OOM" in run.detail
    assert gpu.rental(rental_id).status == "ready"


def test_rejected_submit_fails_without_retry(svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    fc.prompt_mode = "reject"
    run = start(svc, *approved(svc), rental_id)
    assert run.status == "failed" and run.error_code == "submit_rejected" and len(fc.posts) == 1
    assert gpu.rental(rental_id).status == "ready"


# ------------------------------------------------------------ G08: POST /prompt mất response


def test_g08_lost_response_is_recovered_from_queue_and_history_never_resubmitted(env, svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    fc.prompt_mode = "slow_ok"                  # ComfyUI nhận và xếp hàng, nhưng response tới sau timeout của client
    run = start(svc, *approved(svc), rental_id)
    assert len(fc.posts) == 1
    events = env.store.comfy_events(run.run_id)
    assert any(e["transition"] == "submitting→submit_unknown" for e in events)
    # Tra /queue theo prompt_id đã ghi trước khi gửi: thấy prompt, không gửi lại.
    assert run.status == "queued" and run.queue_state == "pending"
    assert [e["transition"] for e in env.store.comfy_events(run.run_id)][-1] == "submit_unknown→queued"
    fc.finish(run.prompt_id)
    assert svc.refresh(run.run_id).status == "succeeded"
    assert len(fc.posts) == 1 and fc.posts[0]["prompt_id"] == run.prompt_id


def test_g08_lost_response_after_prompt_already_finished_is_read_from_history(env, svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    workflow_id, revision = approved(svc)
    original = comfy.ComfyUIRuntime.submit

    def submit_runs_then_times_out(self, prompt, prompt_id, client_id="igniterra"):
        original(self, prompt, prompt_id, client_id)      # tới ComfyUI thật
        fc.finish(prompt_id)                              # chạy xong trước khi client kịp tra
        raise comfy.ComfyUIUnknown("mất kết nối sau khi gửi POST /prompt")

    comfy.ComfyUIRuntime.submit = submit_runs_then_times_out
    try:
        run = start(svc, workflow_id, revision, rental_id)
    finally:
        comfy.ComfyUIRuntime.submit = original
    assert run.status == "succeeded" and len(fc.posts) == 1
    assert "submit_unknown→downloading" in [e["transition"] for e in env.store.comfy_events(run.run_id)]


def test_g08_prompt_absent_everywhere_is_failed_after_lookups_not_resubmitted(svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    fc.prompt_mode = "lost"                     # request không tới hàng đợi, response cũng mất
    run = start(svc, *approved(svc), rental_id)
    assert run.status == "submit_unknown" and run.lookup_misses == 1
    assert gpu.rental(rental_id).status == "busy"        # chưa rõ: vẫn giữ máy
    run = svc.refresh(run.run_id)
    assert run.status == "failed" and run.error_code == "prompt_lost" and "không submit lại" in run.detail
    assert len(fc.posts) == 1 and gpu.rental(rental_id).status == "ready"


def test_g08_submit_interrupted_after_writing_prompt_id_is_looked_up(env, svc, gpu, fake_gpu, fc, server):
    """Tiến trình chết giữa lúc ghi prompt_id và nhận response: poll tra theo prompt_id, không gửi lại."""
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    workflow_id, revision = approved(svc)
    original = comfy.ComfyUIRuntime.submit

    def crash(self, prompt, prompt_id, client_id="igniterra"):
        original(self, prompt, prompt_id, client_id)
        raise KeyboardInterrupt                           # process dừng, không ghi gì thêm

    comfy.ComfyUIRuntime.submit = crash
    try:
        with pytest.raises(KeyboardInterrupt):
            start(svc, workflow_id, revision, rental_id)
    finally:
        comfy.ComfyUIRuntime.submit = original
    [row] = env.store.comfy_runs()
    assert row["status"] == "submitting" and row["prompt_id"] == fc.posts[0]["prompt_id"]
    assert svc.refresh(row["run_id"]).status == "submitting"          # chưa quá hạn: có thể bên khác đang chờ
    svc.submit_grace_s = 0
    run = svc.refresh(row["run_id"])
    assert run.status == "queued" and len(fc.posts) == 1


# ------------------------------------------------------------ G11: output chưa kiểm thì destroy phải cảnh báo


def test_g11_download_failure_keeps_run_unverified_and_destroy_warns_first(env, svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    run = start(svc, *approved(svc), rental_id)
    fc.finish(run.prompt_id)
    fc.view_mode = "error"
    run = svc.refresh(run.run_id)
    assert run.status == "downloading" and run.download_attempts == 1
    assert gpu.rental(rental_id).status == "ready"       # GPU đã xong; output còn trên máy
    for _ in range(2):
        run = svc.refresh(run.run_id)
    assert run.status == "output_failed" and run.error_code == "download_failed" and run.download_attempts == 3
    assert svc.refresh(run.run_id).status == "output_failed"          # không tự tải lại nữa

    # Destroy không xác nhận bỏ output: từ chối trước khi gọi provider; không ghi operation nào.
    with pytest.raises(UnverifiedOutput, match=run.run_id):
        gpu.destroy(rental_id)
    assert fake_gpu.destroyed() == 0 and gpu.rental(rental_id).status == "ready"
    assert env.store.gpu_operations(rental_id, "destroy") == []

    # Tải lại thành công → succeeded → destroy không còn cảnh báo.
    fc.view_mode = "ok"
    assert svc.retry_download(run.run_id).status == "succeeded"
    assert gpu.destroy(rental_id).status == "destroyed" and fake_gpu.destroyed() == 1


def test_g11_operator_may_destroy_after_explicitly_accepting_lost_output(env, svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    run = start(svc, *approved(svc), rental_id)
    fc.finish(run.prompt_id)
    fc.view_mode = "truncate"                   # thiếu byte theo Content-Length: không ghi file dở dang
    run = svc.refresh(run.run_id)
    assert run.status == "downloading"
    run_dir = env.store.path.parent / "comfyui" / "runs" / run.run_id
    assert not run_dir.exists() or not any(run_dir.iterdir())
    rental = gpu.destroy(rental_id, accept_unverified_output=True)
    assert rental.status == "destroyed" and fake_gpu.destroyed() == 1
    run = svc.run(run.run_id)
    assert run.status == "abandoned" and run.error_code == "destroy_accepted"
    assert any(e["error_code"] == "unverified_output_accepted" for e in env.store.gpu_events(rental_id))


def test_g11_busy_rental_with_running_prompt_also_warns(svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    run = start(svc, *approved(svc), rental_id)
    assert gpu.rental(rental_id).status == "busy"
    with pytest.raises(UnverifiedOutput):
        gpu.destroy(rental_id)
    assert gpu.destroy(rental_id, accept_unverified_output=True).status == "destroyed"   # run treo không giữ máy mãi
    assert svc.run(run.run_id).status == "abandoned"


@pytest.mark.parametrize("window", ["after_first_check", "after_rental_read"])
@pytest.mark.parametrize("accept", [False, True])
def test_g11_run_claiming_rental_during_destroy_is_never_left_stuck(monkeypatch, env, svc, gpu, fake_gpu, fc, server,
                                                                   window, accept):
    """Run giành máy ``ready→busy`` giữa lượt kiểm run chưa kiểm đầu destroy và CAS ``→destroying``: không xác nhận
    thì destroy phải từ chối bằng chính run đó; có xác nhận thì run đó bị bỏ. Không bao giờ instance đã hủy mà run
    vẫn treo ở trạng thái chưa xong."""
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    workflow = approved(svc)
    sneaked = []

    def sneak_in():
        if not sneaked:
            sneaked.append(start(svc, *workflow, rental_id))
            assert gpu.rental(rental_id).status == "busy"

    if window == "after_first_check":          # đúng tái hiện của review: lượt đọc đầu trả [] rồi run chen vào
        real = env.store.comfy_runs

        def comfy_runs(**kwargs):
            rows = real(**kwargs)
            sneak_in()
            return rows
        monkeypatch.setattr(env.store, "comfy_runs", comfy_runs)
    else:                                      # sau lượt đọc rental, ngay trước CAS: version cũ, CAS cũng phải thua
        real_start = gpu._start_operation

        def start_operation(*args):
            op_id = real_start(*args)
            sneak_in()
            return op_id
        monkeypatch.setattr(gpu, "_start_operation", start_operation)

    if accept:
        if window == "after_rental_read":      # lượt đầu thua CAS vì version đổi; gọi lại thì bỏ run và hủy máy
            with pytest.raises(GPUServiceError):
                gpu.destroy(rental_id, accept_unverified_output=True)
        assert gpu.destroy(rental_id, accept_unverified_output=True).status == "destroyed"
        assert fake_gpu.destroyed() == 1
        run = svc.run(sneaked[0].run_id)
        assert run.status == "abandoned" and run.error_code == "destroy_accepted"
    else:
        with pytest.raises(UnverifiedOutput) as raised:
            gpu.destroy(rental_id)
        [run] = sneaked
        assert [r["run_id"] for r in raised.value.runs] == [run.run_id]
        assert fake_gpu.destroyed() == 0 and gpu.rental(rental_id).status == "busy"
        assert svc.run(run.run_id).status == "queued"            # còn chạy bình thường, chưa mất gì
        assert [op["status"] for op in env.store.gpu_operations(rental_id, "destroy")] == ["rejected"]


def test_g11_web_destroy_shows_warning_and_needs_separate_acceptance(env, fc, server):
    config = {**env.config, "gpu": {"provider": "fake", "image": IMAGE, "portal": PORTAL}}
    client = TestClient(create_app(config), follow_redirects=False)
    gpu, svc = client.app.state.gpu, client.app.state.comfy
    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    run = start(svc, *approved(svc), rental_id)
    fc.finish(run.prompt_id)
    fc.view_mode = "error"
    svc.refresh(run.run_id)
    page = client.get(f"/gpu/instances/{rental_id}").text
    assert "Output chưa tải/kiểm" in page and run.run_id in page and 'name="accept_unverified"' in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]

    response = client.post(f"/gpu/instances/{rental_id}/destroy", data={"csrf": csrf, "confirm": "on"})
    assert response.status_code == 303 and "ch%C6%B0a+t%E1%BA%A3i" in response.headers["location"]   # "chưa tải"
    assert gpu.rental(rental_id).status == "ready" and gpu.provider.destroyed() == 0

    response = client.post(f"/gpu/instances/{rental_id}/destroy",
                           data={"csrf": csrf, "confirm": "on", "accept_unverified": "on"})
    assert response.status_code == 303 and gpu.rental(rental_id).status == "destroyed"
    assert svc.run(run.run_id).status == "abandoned"


@pytest.mark.parametrize("files, stored, code", [
    ([{**OUTPUT, "filename": "fake.png"}], {"fake.png": (JPEG, "image/png")}, "media_mismatch"),
    ([{**OUTPUT, "filename": "clip.mp4"}], {"clip.mp4": (PNG, "image/png")}, "media_mismatch"),
    ([{**OUTPUT, "filename": "notes.txt"}], {"notes.txt": (b"hello", "text/plain")}, "media_unsupported"),
    ([{**OUTPUT, "filename": "../../studio.db"}], {}, "unsafe_path"),
    ([{**OUTPUT, "subfolder": "../../gpu"}], {}, "unsafe_path"),
])
def test_invalid_output_is_never_marked_verified(env, svc, gpu, fake_gpu, fc, server, files, stored, code):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    fc.files.update(stored)
    run = start(svc, *approved(svc), rental_id)
    fc.finish(run.prompt_id, files)
    run = svc.refresh(run.run_id)
    assert run.status == "output_failed" and run.error_code == code
    [output] = svc.run_detail(run.run_id)["outputs"]
    assert output.status == "failed" and output.sha256 is None
    files_on_disk = [p for p in (env.store.path.parent / "comfyui").rglob("*") if p.is_file()]
    assert files_on_disk == []                  # file chưa kiểm không nằm lại trên đĩa
    with pytest.raises(UnverifiedOutput):
        gpu.destroy(rental_id)


def test_no_output_is_failed_not_succeeded(svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    run = start(svc, *approved(svc), rental_id)
    fc.finish(run.prompt_id, files=[])
    run = svc.refresh(run.run_id)
    assert run.status == "failed" and run.error_code == "no_output"


def test_media_sniffing_and_output_target(tmp_path):
    assert outputs.media_type("a.png", PNG, "image/png") == "image/png"
    assert outputs.media_type("a.mp4", b"\x00\x00\x00\x18ftypisom", "video/mp4") == "video/mp4"
    assert outputs.media_type("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 ", "") == "image/webp"
    with pytest.raises(outputs.OutputInvalid, match="server báo"):
        outputs.media_type("a.png", PNG, "video/mp4")
    target = outputs.output_target(tmp_path / "run", "sub/dir", "a.png", "output")
    assert target == (tmp_path / "run" / "sub" / "dir" / "a.png").resolve()
    (tmp_path / "outside").mkdir()
    (tmp_path / "run" / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(outputs.OutputInvalid, match="ra ngoài"):
        outputs.output_target(tmp_path / "run", "link", "a.png", "output")


# ------------------------------------------------------------ CAS: poll và nút bấm chạy cùng lúc


def test_stale_run_snapshot_loses_cas_and_does_not_release_rental_twice(env, svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    workflow_id, revision = approved(svc)
    run = start(svc, workflow_id, revision, rental_id)
    stale = svc.run(run.run_id)
    fc.finish(run.prompt_id)
    assert svc.refresh(run.run_id).status == "succeeded"
    # Lượt poll thứ hai giữ bản đọc cũ (queued): CAS thua, không ghi, không nhả máy lần nữa.
    assert not svc._move(stale, "failed", error_code="prompt_lost")
    assert svc.run(run.run_id).status == "succeeded"
    # Run mới giành máy; lượt nhả muộn của run cũ không được nhả máy đang giữ cho run mới.
    second = start(svc, workflow_id, revision, rental_id)
    assert gpu.rental(rental_id).status == "busy"
    assert not gpu.release_after_run(rental_id, run.run_id)
    assert gpu.rental(rental_id).status == "busy" and svc.run(second.run_id).status == "queued"


def test_release_waits_for_a_run_that_has_not_claimed_yet(env, svc, gpu, fake_gpu, fc, server):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    workflow_id, revision = approved(svc)
    first = start(svc, workflow_id, revision, rental_id)
    # Run thứ hai vừa ghi checking (chưa giành máy) đúng lúc run đầu xong.
    env.store.add_comfy_run("cr_000000000002", workflow_id, revision, "x" * 64, svc.revision(workflow_id).manifest_sha256,
                            rental_id)
    fc.finish(first.prompt_id)
    assert svc.refresh(first.run_id).status == "succeeded"
    assert gpu.rental(rental_id).status == "busy"             # không nhả: còn run khác đang chạy
    svc.submit_grace_s = 0
    run = svc.refresh("cr_000000000002")                      # run kẹt ở checking quá hạn → blocked, nhả máy
    assert run.status == "blocked" and run.error_code == "interrupted"
    assert gpu.rental(rental_id).status == "ready"


def test_claim_lost_to_another_run_blocks_without_releasing(env, svc, gpu, fake_gpu, fc, server, monkeypatch):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    workflow_id, revision = approved(svc)
    first = start(svc, workflow_id, revision, rental_id)
    real_rental = gpu.rental

    def looks_ready(rid):                                     # bản đọc cũ: rental còn ready lúc kiểm
        rental = real_rental(rid)
        return rental.model_copy(update={"status": "ready"}) if rid == rental_id else rental

    monkeypatch.setattr(gpu, "rental", looks_ready)
    monkeypatch.setattr(gpu, "claim_for_run", lambda rid, run_id: False)
    run = start(svc, workflow_id, revision, rental_id)
    monkeypatch.undo()
    assert run.status == "blocked" and run.error_code == "rental_busy"
    assert gpu.rental(rental_id).status == "busy" and svc.run(first.run_id).status == "queued"
    assert len(fc.posts) == 1


def test_poll_keeps_going_when_one_run_errors(env, svc, gpu, fake_gpu, fc, server, monkeypatch):
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    run = start(svc, *approved(svc), rental_id)
    fc.finish(run.prompt_id)
    original = svc.refresh
    calls = []

    def flaky(run_id):
        calls.append(run_id)
        if len(calls) == 1:
            raise ComfyServiceError("lỗi tạm")
        return original(run_id)

    monkeypatch.setattr(svc, "refresh", flaky)
    assert [r.status for r in svc.poll()] == ["queued"]
    assert any(e["error_code"] == "poll_error" for e in env.store.comfy_events(run.run_id))
    assert [r.status for r in svc.poll()] == ["succeeded"]


# ------------------------------------------------------------ web, CLI, ranh giới import


def test_web_routes_require_csrf_and_confirmation(env, fc, server):
    config = {**env.config, "gpu": {"provider": "fake", "image": IMAGE, "portal": PORTAL}}
    client = TestClient(create_app(config), follow_redirects=False)
    gpu, svc = client.app.state.gpu, client.app.state.comfy
    page = client.get("/comfyui/workflows").text
    assert "Thư viện" in page
    csrf = page.split('name="csrf" value="')[1].split('"')[0]
    files = {"file": ("flux.json", json.dumps(API_PROMPT).encode(), "application/json")}
    assert client.post("/comfyui/workflows", data={"csrf": "wrong"}, files=files).status_code == 403
    response = client.post("/comfyui/workflows", data={"csrf": csrf, "name": "flux"}, files=files)
    assert response.status_code == 303 and "needs_review" in response.headers["location"]
    [item] = svc.library()
    workflow_id = item["latest"].workflow_id
    html = client.get(f"/comfyui/workflows/{workflow_id}").text
    assert "needs_review" in html and "comfyui manifest attach" in html and 'name="run_id"' not in html

    record = svc.attach_manifest(workflow_id, 1, WorkflowManifest(models=[MODEL]))
    svc.approve_manifest(record.manifest_sha256)
    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    html = client.get(f"/comfyui/workflows/{workflow_id}").text
    run_id = html.split('name="run_id" value="')[1].split('"')[0]
    form = {"csrf": csrf, "run_id": run_id, "revision": "1", "rental_id": rental_id}
    assert client.post(f"/comfyui/workflows/{workflow_id}/runs", data={**form, "csrf": "x"}).status_code == 403
    response = client.post(f"/comfyui/workflows/{workflow_id}/runs", data=form)
    assert "c%E1%BA%A7n+x%C3%A1c+nh%E1%BA%ADn" in response.headers["location"] and fc.posts == []   # "cần xác nhận"
    response = client.post(f"/comfyui/workflows/{workflow_id}/runs", data={**form, "confirm": "on"})
    assert response.headers["location"].startswith(f"/comfyui/runs/{run_id}")
    response = client.post(f"/comfyui/workflows/{workflow_id}/runs", data={**form, "confirm": "on"})
    assert len(fc.posts) == 1                                                # cùng run_id (bấm hai lần): không chạy thêm

    fc.finish(fc.posts[0]["prompt_id"])
    assert client.post(f"/comfyui/runs/{run_id}/refresh", data={"csrf": "x"}).status_code == 403
    client.post(f"/comfyui/runs/{run_id}/refresh", data={"csrf": csrf})
    html = client.get(f"/comfyui/runs/{run_id}").text
    assert "succeeded" in html and f"/files/comfyui/runs/{run_id}/{OUTPUT['filename']}" in html
    password = (env.store.path.parent / "gpu" / rental_id / "web_password").read_text(encoding="utf-8")
    assert password not in html
    assert client.get(f"/files/comfyui/runs/{run_id}/{OUTPUT['filename']}").content == PNG


def test_cli_imports_attaches_and_approves(env, tmp_path, capsys):
    from studio import cli

    config = {**env.config, "gpu": {"provider": "fake", "image": IMAGE}}
    source = tmp_path / "flux_api.json"
    source.write_text(json.dumps(API_PROMPT), encoding="utf-8")
    cli.cmd_comfyui(argparse.Namespace(comfyui_command="import", file=str(source), api=None, name=None,
                                       workflow_id=None, note=None), config, env.store)
    out = capsys.readouterr().out
    workflow_id = out.split()[0]
    assert "needs_review" in out and "thiếu:" in out
    manifest_file = tmp_path / "manifest.json"
    manifest = WorkflowManifest(models=[MODEL], custom_nodes=[NODE])
    manifest_file.write_text(manifest.model_dump_json(), encoding="utf-8")
    cli.cmd_comfyui(argparse.Namespace(comfyui_command="manifest", manifest_command="attach", workflow_id=workflow_id,
                                       revision=1, file=str(manifest_file), note=None), config, env.store)
    assert f"{manifest.sha256()} pending_review" in capsys.readouterr().out
    cli.cmd_comfyui(argparse.Namespace(comfyui_command="manifest", manifest_command="approve", sha256=manifest.sha256(),
                                       accept_licenses=False), config, env.store)
    assert env.store.workflow_revision(workflow_id)["manifest_sha256"] == manifest.sha256()
    assert env.store.comfy_manifest(manifest.sha256())["status"] == "approved"
    parser = cli.build_parser()
    assert parser.parse_args(["comfyui", "run", workflow_id, "1", "r_000000000001"]).func is cli.cmd_comfyui


def test_comfy_manifest_is_not_merged_into_gpu_provisioning(env, svc):
    """Duyệt dependency của workflow là bản khai (audit, license), không sinh hay đổi manifest provisioning."""
    approved(svc, manifest=WorkflowManifest(models=[MODEL], custom_nodes=[NODE]))
    assert env.store.manifests() == []


def test_comfyui_modules_never_import_mpt_or_web():
    studio = Path(__file__).resolve().parent.parent / "studio"
    for path in (studio / "comfyui").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import app" not in text and "from app" not in text and "studio.web" not in text, path
