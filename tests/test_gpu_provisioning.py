"""Gate 4 lát C: manifest provisioning đã duyệt, health + smoke tới ``ready`` (09 · Kiểm chứng G05, G07).

Instance giả là một ``ThreadingHTTPServer`` trên 127.0.0.1 đóng cả Instance Portal (tiến độ provisioning,
``/.provisioning_complete``) lẫn ComfyUI (``/system_stats``, ``/prompt``, ``/queue``, ``/history``, ``/view``) sau
lớp xác thực Bearer, như Caddy của base-image. Provider là ``FakeGPUProvider``; 0 USD, không gọi mạng ngoài.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sqlite3
import stat
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import COMFYUI_REF, approve_gpu_manifest, gpu_manifest
from studio.comfyui import runtime as comfy
from studio.gpu.adapters.fake import FakeGPUProvider
from studio.gpu.contracts import InstanceSpec, OfferFilters, Quote
from studio.gpu.provisioning import ProvisioningManifest, StepReport, classify, render, validate
from studio.gpu.service import GPUService, GPUServiceError
from studio.web import create_app

STUDIO = Path(__file__).resolve().parent.parent / "studio"
IMAGE = "example/comfyui@sha256:" + "0" * 64
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000")
HF = "https://huggingface.co/black-forest-labs/FLUX.2-klein-4B/resolve/" + "a" * 40 + "/flux-2-klein-4b.safetensors"
MODEL = {"url": HF, "dest": "diffusion_models/flux-2-klein-4b.safetensors", "size_bytes": 8_000_000_000,
         "sha256": "b" * 64, "license": "Apache-2.0"}
NODE = {"url": "https://github.com/example/ComfyUI-Example", "ref": "c" * 40, "dest": "ComfyUI-Example"}
DONE = {"status": "done", "attempt": 1}


# ------------------------------------------------------------ instance giả: Instance Portal + ComfyUI


class Instance:
    def __init__(self):
        self.token: str | None = None
        self.open_comfyui = False           # cổng ComfyUI không xác thực (AUTH_EXCLUDE) → phải chặn
        self.report: dict = {"complete": False, "steps": {}}
        self.comfy_up = True
        self.smoke = "ok"                   # ok | error | drop | slow_ok | ignore_id
        self.history: dict = {}
        self.posts: list[dict] = []
        self.requests: list[tuple[str, str, bool]] = []
        self.lock = threading.Lock()


INST = Instance()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_any()

    def do_POST(self):
        self.handle_any()

    def reply(self, status: int, body, content_type: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_any(self):
        url = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        authed = INST.token is not None and self.headers.get("authorization") == f"Bearer {INST.token}"
        with INST.lock:
            INST.requests.append((self.command, url.path, authed))
        portal = url.path == "/provisioning/status"
        if not authed and (portal or not INST.open_comfyui):
            return self.reply(401, {"error": "unauthorized"})
        if portal:
            return self.reply(200, INST.report)
        if not INST.comfy_up:
            return self.reply(502, {"error": "bad gateway"})
        if url.path == "/system_stats":
            return self.reply(200, {"system": {"os": "posix", "comfyui_version": "0.3.99", "python_version": "3.12"},
                                    "devices": [{"name": "cuda:0 NVIDIA GeForce RTX 4090", "vram_total": 25_000_000_000}]})
        if url.path == "/prompt" and self.command == "POST":
            body = json.loads(raw)
            prompt_id = body.get("prompt_id")
            with INST.lock:
                INST.posts.append(body)
            if INST.smoke == "ignore_id":
                return self.reply(200, {"prompt_id": "server-made-id", "number": 1, "node_errors": {}})
            if INST.smoke != "drop":
                ok = INST.smoke != "error"
                INST.history[prompt_id] = {
                    "status": {"status_str": "success" if ok else "error", "completed": ok,
                               "messages": [] if ok else [["execution_error", {"exception_message": "CUDA OOM"}]]},
                    "outputs": {"2": {"images": [{"filename": "igniterra_smoke_00001_.png", "subfolder": "",
                                                  "type": "output"}]}} if ok else {}}
            if INST.smoke == "slow_ok":
                time.sleep(1.5)             # quá timeout của client: response mất, prompt đã chạy
            return self.reply(200, {"prompt_id": prompt_id, "number": 1, "node_errors": {}})
        if url.path.startswith("/history/"):
            prompt_id = url.path.removeprefix("/history/")
            return self.reply(200, {prompt_id: INST.history[prompt_id]} if prompt_id in INST.history else {})
        if url.path == "/queue":
            return self.reply(200, {"queue_running": [], "queue_pending": []})
        if url.path == "/view":
            query = parse_qs(url.query)
            if query.get("filename") == ["igniterra_smoke_00001_.png"] and query.get("type") == ["output"]:
                return self.reply(200, PNG, "image/png")
            return self.reply(404, {"error": "not found"})
        return self.reply(404, {"error": "not found"})


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def inst(server):
    INST.__init__()
    return INST


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake_gpu(clock):
    return FakeGPUProvider(clock=clock)


PORTAL = {"scheme": "http", "timeout_s": 1}


@pytest.fixture
def svc(env, fake_gpu, clock):
    return GPUService(env.store, fake_gpu, image=IMAGE, clock=clock, portal=PORTAL)


def quote(provider, offer_id: str = "1002") -> Quote:
    offer = next(o for o in provider.search_offers(OfferFilters()) if o.offer_id == offer_id)
    return Quote(offer_id=offer.offer_id, dph_total=offer.dph_total, quoted_at=offer.quoted_at, confirmed=True)


def rent(svc, fake_gpu, inst, server, manifest: ProvisioningManifest | None = None, *, boot: bool = True,
         disk_gb: float = 100) -> str:
    """Thuê với manifest đã duyệt, rồi cho host lên với cổng trỏ tới instance giả và WEB_PASSWORD đã nhận."""
    sha = approve_gpu_manifest(svc.store, manifest or gpu_manifest())
    rental_id = svc.new_rental_id()
    rental = svc.rent(rental_id, quote(fake_gpu), OfferFilters(disk_gb=disk_gb), manifest_sha256=sha)
    assert rental.status == "provisioning"
    instance_id = rental.provider_instance_id
    inst.token = fake_gpu.specs[instance_id].env["WEB_PASSWORD"]      # instance nhận mật khẩu qua env
    if boot:
        fake_gpu.boot(instance_id, {"1111/tcp": server, "8188/tcp": server})
    return rental_id


def steps(env, rental_id: str) -> dict[str, tuple[str, int, str | None]]:
    return {s["step_key"]: (s["status"], s["attempt"], s["error_code"]) for s in env.store.provision_steps(rental_id)}


def events(env, rental_id: str) -> list[dict]:
    return env.store.gpu_events(rental_id)


def all_steps_done(manifest: ProvisioningManifest | None = None) -> dict:
    return {"complete": True, "disk_free_gb": 200,
            "steps": {"runtime_setup": DONE, "custom_nodes": DONE, "models": DONE}}


# ------------------------------------------------------------ manifest: ghim commit, duyệt trước khi thuê


@pytest.mark.parametrize("change, reason", [
    ({"comfyui_ref": "v0.3.60"}, "full commit SHA"),
    ({"comfyui_ref": "master"}, "full commit SHA"),
    ({"custom_nodes": [{**NODE, "ref": "main"}]}, "full commit SHA"),
    ({"custom_nodes": [{**NODE, "url": "https://gitlab.com/x/y"}]}, "github.com"),
    ({"custom_nodes": [{**NODE, "dest": ".."}]}, "custom node lạ"),
    ({"downloads": [{**MODEL, "url": HF.replace("a" * 40, "main")}]}, "resolve/<commit"),
    ({"downloads": [{**MODEL, "url": "https://example.com/model.safetensors"}]}, "huggingface.co"),
    ({"downloads": [{**MODEL, "dest": "../../etc/passwd"}]}, "đích model"),
    ({"downloads": [{**MODEL, "sha256": "abc"}]}, "SHA-256"),
    ({"comfyui_url": "https://github.com/someone/ComfyUI-fork"}, "ComfyUI phải từ"),
])
def test_manifest_validation_requires_pinned_sources(change, reason):
    assert any(reason in p for p in validate(gpu_manifest(**change)))


def test_manifest_failure_policy_is_destroy_with_small_retries():
    manifest = gpu_manifest(custom_nodes=[NODE], downloads=[MODEL])
    assert validate(manifest) == []
    provisioner = manifest.provisioner()
    assert provisioner["on_failure"] == {"action": "destroy", "max_retries": 2}
    assert [r["ref"] for r in provisioner["git_repos"]] == [COMFYUI_REF, "c" * 40]
    assert provisioner["downloads"][0]["url"] == HF
    assert provisioner["downloads"][0]["dest"] == "/workspace/ComfyUI/models/diffusion_models/flux-2-klein-4b.safetensors"
    with pytest.raises(ValidationError):
        ProvisioningManifest.model_validate({**manifest.model_dump(), "on_failure": {"action": "continue"}})
    with pytest.raises(ValidationError):
        gpu_manifest(max_retries=10)
    text = render(manifest, manifest.sha256())
    assert 'action: "destroy"' in text and "max_retries: 2" in text and f'ref: "{COMFYUI_REF}"' in text


def test_rent_requires_an_approved_manifest(env, fake_gpu, clock):
    svc = GPUService(env.store, fake_gpu, image=IMAGE, clock=clock)
    with pytest.raises(GPUServiceError, match="manifest provisioning đã duyệt"):
        svc.rent(svc.new_rental_id(), quote(fake_gpu), OfferFilters())
    record = svc.propose_manifest(gpu_manifest())
    assert record.status == "pending_review"
    with pytest.raises(GPUServiceError, match="manifest provisioning đã duyệt"):
        svc.rent(svc.new_rental_id(), quote(fake_gpu), OfferFilters(), manifest_sha256=record.manifest_sha256)
    assert fake_gpu.created() == 0 and env.store.rentals() == []
    with pytest.raises(GPUServiceError, match="full commit SHA"):
        svc.propose_manifest(gpu_manifest(comfyui_ref="master"))
    assert svc.approve_manifest(record.manifest_sha256).status == "approved"
    rental = svc.rent(svc.new_rental_id(), quote(fake_gpu), OfferFilters())
    assert rental.status == "provisioning" and rental.manifest_sha256 == record.manifest_sha256


def test_manifest_edited_in_sqlite_after_approval_is_refused(env, fake_gpu, clock):
    svc = GPUService(env.store, fake_gpu, image=IMAGE, clock=clock)
    sha = approve_gpu_manifest(env.store)
    tampered = json.loads(svc.manifest(sha).manifest_json)
    tampered["comfyui"]["ref"] = "f" * 40
    with sqlite3.connect(env.store.path) as db:
        db.execute("UPDATE gpu_manifests SET manifest_json=? WHERE manifest_sha256=?", (json.dumps(tampered), sha))
    with pytest.raises(GPUServiceError, match="không khớp SHA-256"):
        svc.rent(svc.new_rental_id(), quote(fake_gpu), OfferFilters())
    assert fake_gpu.created() == 0


def test_g07_manifest_needing_more_disk_than_requested_is_refused_before_create(env, svc, fake_gpu):
    sha = approve_gpu_manifest(env.store, gpu_manifest(downloads=[{**MODEL, "size_bytes": 40_000_000_000}]))
    with pytest.raises(GPUServiceError, match="GB disk"):
        svc.rent(svc.new_rental_id(), quote(fake_gpu), OfferFilters(disk_gb=60), manifest_sha256=sha)
    assert fake_gpu.created() == 0 and env.store.rentals() == []


# ------------------------------------------------------------ WEB_PASSWORD, ENABLE_AUTH (09 · s5.b2, s7.b2)


def test_rent_writes_manifest_and_private_password_never_in_sqlite_or_html(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server)
    folder = env.store.path.parent / "gpu" / rental_id
    password = (folder / "web_password").read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(folder / "web_password").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    yaml_text = (folder / "provisioning.yaml").read_text(encoding="utf-8")
    assert svc.rental(rental_id).manifest_sha256 in yaml_text and 'action: "destroy"' in yaml_text
    spec = fake_gpu.specs[svc.rental(rental_id).provider_instance_id]
    assert spec.env == {"WEB_PASSWORD": password, "ENABLE_AUTH": "true",
                        "PROVISIONING_MANIFEST": "/etc/igniterra/provisioning.yaml"}
    assert password not in repr(spec)
    with sqlite3.connect(env.store.path) as db:
        assert password not in "\n".join(db.iterdump())
    # Trang rental và /files không lộ mật khẩu hay file trong data/gpu/.
    config = {**env.config, "gpu": {"provider": "fake", "image": IMAGE}}
    client = TestClient(create_app(config), follow_redirects=False)
    assert client.get(f"/files/gpu/{rental_id}/web_password").status_code == 404
    assert client.get(f"/files/gpu/{rental_id}/provisioning.yaml").status_code == 404


def test_password_is_redacted_from_events(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server)
    password = (env.store.path.parent / "gpu" / rental_id / "web_password").read_text(encoding="utf-8")
    fresh = GPUService(env.store, fake_gpu, image=IMAGE, portal=PORTAL)     # sau khởi động lại vẫn biết bí mật
    assert password not in fresh._clean(f"401 Bearer {password}") and "<WEB_PASSWORD>" in fresh._clean(password)


@pytest.mark.parametrize("env_vars", [{"AUTH_EXCLUDE": "8188"}, {"ENABLE_AUTH": "false"}])
def test_instance_spec_never_opens_unauthenticated_ports(env_vars):
    with pytest.raises(ValidationError):
        InstanceSpec(label="igniterra:r_000000000001", image=IMAGE, disk_gb=60, env=env_vars)


# ------------------------------------------------------------ ready chỉ khi đủ bằng chứng (09 · s4.b2)


def test_ready_only_with_full_evidence(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server, gpu_manifest(custom_nodes=[NODE], downloads=[MODEL]), boot=False)
    instance_id = svc.rental(rental_id).provider_instance_id

    # Host chưa chạy.
    assert svc.check_provisioning(rental_id).status == "provisioning"
    assert steps(env, rental_id)["host_boot"] == ("running", 1, None)

    # Host chạy nhưng portal chưa có cổng → chưa có kết nối có xác thực.
    fake_gpu.boot(instance_id, ports={})
    assert svc.check_provisioning(rental_id).status == "provisioning"
    assert steps(env, rental_id)["host_boot"][0] == "done" and steps(env, rental_id)["transport_ready"][0] == "running"

    # Có cổng; provisioner đang cài custom node, model chưa tới.
    fake_gpu.boot(instance_id, {"1111/tcp": server, "8188/tcp": server})
    inst.report = {"complete": False, "disk_free_gb": 200, "steps": {
        "runtime_setup": DONE, "custom_nodes": {"status": "running", "attempt": 1}, "models": {"status": "pending"}}}
    assert svc.check_provisioning(rental_id).status == "provisioning"
    current = steps(env, rental_id)
    assert current["transport_ready"][0] == "done" and current["runtime_setup"][0] == "done"
    assert current["custom_nodes"][0] == "running" and current["models"][0] == "pending"

    # Mọi bước provisioner xong nhưng chưa có /.provisioning_complete → chưa health.
    inst.report = {"complete": False, "disk_free_gb": 200, "steps": {
        "runtime_setup": DONE, "custom_nodes": DONE,
        "models": {"status": "done", "attempt": 1, "bytes_done": 8_000_000_000, "bytes_total": 8_000_000_000}}}
    inst.report["files"] = {"/workspace/ComfyUI/models/" + MODEL["dest"]: MODEL["sha256"]}
    assert svc.check_provisioning(rental_id).status == "provisioning"
    assert steps(env, rental_id)["health_smoke"][0] == "pending"
    models = next(s for s in env.store.provision_steps(rental_id) if s["step_key"] == "models")
    assert models["bytes_done"] == models["bytes_total"] == 8_000_000_000

    # Có marker nhưng ComfyUI chưa trả /system_stats.
    inst.report["complete"] = True
    inst.comfy_up = False
    assert svc.check_provisioning(rental_id).status == "provisioning"
    assert steps(env, rental_id)["health_smoke"][0] == "running" and inst.posts == []

    # ComfyUI lên: smoke ra file → ready.
    inst.comfy_up = True
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "ready"
    assert all(status == "done" for status, _, _ in steps(env, rental_id).values())

    [post] = inst.posts
    [smoke_op] = env.store.gpu_operations(rental_id, "smoke")
    assert post["prompt_id"] == smoke_op["op_id"] and smoke_op["status"] == "confirmed"
    assert post["prompt"] == comfy.smoke_prompt()
    evidence = json.loads(next(e for e in events(env, rental_id) if e["transition"] == "ready_evidence")["detail"])
    assert evidence["provisioning_complete"] and evidence["comfyui_version"] == "0.3.99"
    assert evidence["smoke"]["bytes"] == len(PNG) and len(evidence["smoke"]["sha256"]) == 64
    # Mọi request tới ComfyUI để lấy bằng chứng đều có Bearer; request không token chỉ để kiểm cổng bị chặn.
    assert all(authed for method, path, authed in inst.requests if path in ("/prompt", "/view") or path.startswith("/history"))
    assert any(path == "/system_stats" and not authed for method, path, authed in inst.requests)
    assert fake_gpu.destroyed() == 0
    assert svc.check_provisioning(rental_id).status == "ready" and len(inst.posts) == 1     # gọi lại không submit thêm


def test_models_without_instance_checksum_are_marked_unverified(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server, gpu_manifest(downloads=[MODEL]))
    inst.report = all_steps_done()
    assert svc.check_provisioning(rental_id).status == "ready"
    assert any(e["error_code"] == "checksum_unverified" for e in events(env, rental_id))


def test_model_checksum_mismatch_is_provision_failed(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server, gpu_manifest(downloads=[MODEL]))
    inst.report = {**all_steps_done(), "files": {MODEL["dest"]: "0" * 64}}
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "destroyed" and steps(env, rental_id)["models"][::2] == ("failed", "checksum_mismatch")


# ------------------------------------------------------------ G05: host chạy, pip hoặc model lỗi


def test_g05_pip_failure_past_retry_budget_is_provision_failed_and_destroyed(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server)
    pip_error = "ERROR: Could not install packages due to an OSError: HTTPSConnectionPool(host='pypi.org')"

    # Lượt 1 và 2 hỏng: provisioner còn lượt (max_retries 2) → vẫn provisioning, ghi mỗi lượt một sự kiện.
    for attempt in (1, 2):
        inst.report = {"complete": False, "steps": {"runtime_setup": {"status": "failed", "attempt": attempt,
                                                                      "error": pip_error}}}
        assert svc.check_provisioning(rental_id).status == "provisioning"
        assert svc.check_provisioning(rental_id).status == "provisioning"
    assert [e["detail"][:6] for e in events(env, rental_id) if e["transition"] == "runtime_setup_retry"] == ["lượt 1", "lượt 2"]
    assert steps(env, rental_id)["runtime_setup"] == ("running", 2, None)

    # Lượt 3 (= 1 + max_retries) hỏng → provision_failed, không ready, destroy theo on_failure.
    inst.report["steps"]["runtime_setup"]["attempt"] = 3
    rental = svc.check_provisioning(rental_id)
    transitions = [e["transition"] for e in events(env, rental_id)]
    assert "provisioning→provision_failed" in transitions and "provisioning→ready" not in transitions
    assert transitions.index("provisioning→provision_failed") < transitions.index("provision_failed→destroying")
    failed = next(e for e in events(env, rental_id) if e["transition"] == "provisioning→provision_failed")
    assert failed["error_code"] == "pip_failed" and "runtime_setup" in failed["detail"] and "pypi.org" in failed["detail"]
    assert rental.status == "destroyed" and fake_gpu.destroyed() == 1 and fake_gpu.instances == {}
    current = steps(env, rental_id)
    assert current["runtime_setup"] == ("failed", 3, "pip_failed")
    assert {current[k][0] for k in ("custom_nodes", "models", "health_smoke", "ready")} == {"skipped"}
    assert inst.posts == []


def test_g05_model_download_gave_up_is_provision_failed_and_destroyed(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server, gpu_manifest(downloads=[MODEL]))
    inst.report = {"complete": False, "disk_free_gb": 500, "steps": {
        "runtime_setup": DONE, "custom_nodes": {"status": "done"},
        "models": {"status": "failed", "attempt": 1, "final": True, "error": "404 Client Error: Entry Not Found"}}}
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "destroyed" and fake_gpu.destroyed() == 1
    assert steps(env, rental_id)["models"][::2] == ("failed", "model_download_failed")
    assert steps(env, rental_id)["custom_nodes"][0] == "skipped"        # manifest không có custom node


def test_g05_destroy_failure_after_provision_failed_keeps_record(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server)
    fake_gpu.fail["destroy_instance"] = "timeout_before_send"
    inst.report = {"steps": {"runtime_setup": {"status": "failed", "attempt": 3, "error": "pip exploded"}}}
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "destroy_unknown"                         # vẫn có thể tính phí: giữ record, reconcile
    assert "provisioning→provision_failed" in [e["transition"] for e in events(env, rental_id)]


def test_provisioning_timeout_is_provision_failed_and_destroyed(env, svc, fake_gpu, inst, server, clock):
    rental_id = rent(svc, fake_gpu, inst, server)
    inst.report = {"steps": {"runtime_setup": {"status": "running", "attempt": 1}}}
    assert svc.check_provisioning(rental_id).status == "provisioning"
    clock.now += timedelta(seconds=svc.provision_timeout_s + 1)
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "destroyed"
    assert steps(env, rental_id)["runtime_setup"][::2] == ("failed", "provision_timeout")


def test_host_gone_during_provisioning_is_provision_failed(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server)
    fake_gpu.instances.clear()
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "destroyed" and steps(env, rental_id)["host_boot"][::2] == ("failed", "instance_missing")


# ------------------------------------------------------------ G07 (nửa provisioning): disk, dependency xung đột


@pytest.mark.parametrize("step_key, error, code", [
    ("runtime_setup", "OSError: [Errno 28] No space left on device: '/venv/lib'", "disk_insufficient"),
    ("models", "write failed: No space left on device", "disk_insufficient"),
    ("custom_nodes", "ERROR: ResolutionImpossible: for help visit https://pip.pypa.io", "dependency_conflict"),
    ("runtime_setup", ("ERROR: Cannot install torch==2.5 and xformers because these package versions have "
                       "conflicting dependencies."), "dependency_conflict"),
])
def test_g07_disk_or_dependency_conflict_blocks_without_retry_and_records_cause(env, svc, fake_gpu, inst, server,
                                                                               step_key, error, code):
    rental_id = rent(svc, fake_gpu, inst, server, gpu_manifest(custom_nodes=[NODE], downloads=[MODEL]))
    reported = {"runtime_setup": DONE, "custom_nodes": DONE, "models": DONE}
    order = ["runtime_setup", "custom_nodes", "models"]
    for key in order[order.index(step_key):]:
        reported[key] = {"status": "pending"}
    reported[step_key] = {"status": "failed", "attempt": 1, "error": error}
    inst.report = {"complete": False, "steps": reported}
    rental = svc.check_provisioning(rental_id)
    # Lượt đầu đã dừng (thử lại không qua được), không chờ hết max_retries; nguyên nhân ghi ở bước và sự kiện.
    assert rental.status == "destroyed" and fake_gpu.destroyed() == 1
    assert steps(env, rental_id)[step_key] == ("failed", 1, code)
    failed = next(e for e in events(env, rental_id) if e["transition"] == "provisioning→provision_failed")
    assert failed["error_code"] == code and step_key in failed["detail"]
    assert not any(e["transition"].endswith("_retry") for e in events(env, rental_id))


def test_g07_free_disk_below_remaining_model_bytes_fails_early(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server, gpu_manifest(downloads=[MODEL]))
    inst.report = {"complete": False, "disk_free_gb": 3.5, "steps": {
        "runtime_setup": DONE, "models": {"status": "running", "attempt": 1, "bytes_done": 1_000_000_000,
                                          "bytes_total": 8_000_000_000}}}
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "destroyed"
    assert steps(env, rental_id)["models"][::2] == ("failed", "disk_insufficient")
    detail = next(e for e in events(env, rental_id) if e["transition"] == "provisioning→provision_failed")["detail"]
    assert "3.5 GB" in detail and "7.00 GB" in detail


def test_classify_falls_back_to_step_code():
    assert classify("runtime_setup", StepReport(status="failed", error="Segmentation fault")) == "pip_failed"
    assert classify("custom_nodes", StepReport(status="failed", error="git clone failed")) == "custom_node_failed"
    assert classify("models", StepReport(status="failed", error_code="disk_insufficient")) == "disk_insufficient"


# ------------------------------------------------------------ xác thực và smoke


def test_unauthenticated_comfyui_port_is_never_ready(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server)
    inst.open_comfyui = True                    # như đặt AUTH_EXCLUDE cho cổng ComfyUI
    inst.report = all_steps_done()
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "destroyed" and steps(env, rental_id)["transport_ready"][::2] == ("failed", "auth_open")
    assert inst.posts == []


def test_wrong_password_is_auth_rejected(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server)
    inst.token = "someone-elses-password"
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "destroyed" and steps(env, rental_id)["transport_ready"][::2] == ("failed", "auth_rejected")


def test_smoke_error_is_provision_failed(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server)
    inst.report, inst.smoke = all_steps_done(), "error"
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "destroyed"
    assert steps(env, rental_id)["health_smoke"][::2] == ("failed", "smoke_failed")
    assert "CUDA OOM" in next(e for e in events(env, rental_id) if e["error_code"] == "smoke_failed")["detail"]


def test_smoke_lost_response_is_looked_up_not_resubmitted(env, svc, fake_gpu, inst, server):
    """G08 ở mức smoke: POST /prompt mất response → tra /queue, /history theo prompt_id, không gửi thêm."""
    rental_id = rent(svc, fake_gpu, inst, server)
    inst.report, inst.smoke = all_steps_done(), "slow_ok"
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "ready" and len(inst.posts) == 1
    [op] = env.store.gpu_operations(rental_id, "smoke")
    assert op["status"] == "confirmed"
    assert any(e["error_code"] == "smoke_submit_unknown" for e in events(env, rental_id))


def test_smoke_prompt_absent_everywhere_gets_a_new_prompt_id_within_budget(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server, gpu_manifest(max_retries=1))
    inst.report, inst.smoke = all_steps_done(), "drop"
    for _ in range(2):
        assert svc.check_provisioning(rental_id).status == "provisioning"
    ids = [op["op_id"] for op in env.store.gpu_operations(rental_id, "smoke")]
    assert len(ids) == 2 and len(set(ids)) == 2 and [p["prompt_id"] for p in inst.posts] == ids
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "destroyed" and steps(env, rental_id)["health_smoke"][::2] == ("failed", "smoke_lost")
    assert len(inst.posts) == 2


def test_runtime_that_ignores_client_prompt_id_is_refused(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server)
    inst.report, inst.smoke = all_steps_done(), "ignore_id"
    rental = svc.check_provisioning(rental_id)
    assert rental.status == "destroyed" and steps(env, rental_id)["health_smoke"][::2] == ("failed", "smoke_rejected")


def test_runtime_client_maps_errors_and_refuses_unsafe_view_paths(inst, server):
    inst.token = "tok-123456"
    runtime = comfy.ComfyUIRuntime(f"http://{server}", "tok-123456", timeout_s=1)
    assert "tok-123456" not in repr(runtime)
    assert runtime.system_stats()["system"]["comfyui_version"] == "0.3.99"
    with pytest.raises(comfy.ComfyUIAuthError):
        comfy.ComfyUIRuntime(f"http://{server}", "wrong", timeout_s=1).system_stats()
    with pytest.raises(comfy.ComfyUIUnavailable):
        comfy.ComfyUIRuntime("http://127.0.0.1:9", "x", timeout_s=1).system_stats()
    assert runtime.history("does-not-exist") is None
    for filename, subfolder in [("../studio.db", ""), ("/etc/passwd", ""), ("a.png", "../x"), ("a.png", "/abs")]:
        with pytest.raises(comfy.ComfyUIRejected):
            runtime.view(filename, subfolder)
    assert runtime.view("igniterra_smoke_00001_.png") == PNG


# ------------------------------------------------------------ nối vào reconcile, web, CLI, ranh giới import


def test_reconcile_and_startup_recover_drive_provisioning(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server)
    inst.report = all_steps_done()
    fresh = GPUService(env.store, fake_gpu, image=IMAGE, portal=PORTAL, clock=svc.clock)   # worker khởi động lại
    fresh.recover()
    assert svc.rental(rental_id).status == "ready"


def test_web_rental_page_shows_manifest_and_check_button(env, inst, server):
    approve_gpu_manifest(env.store, gpu_manifest(downloads=[MODEL]))
    config = {**env.config, "gpu": {"provider": "fake", "image": IMAGE, "portal": PORTAL}}
    client = TestClient(create_app(config), follow_redirects=False)
    page = client.get("/gpu").text
    assert "Manifest đã duyệt" in page and "on_failure: destroy" in page
    gpu = client.app.state.gpu
    rental_id = gpu.new_rental_id()
    gpu.rent(rental_id, quote(gpu.provider), OfferFilters(disk_gb=100))
    html = client.get(f"/gpu/instances/{rental_id}").text
    assert "Kiểm provisioning" in html and MODEL["sha256"][:16] in html and HF in html
    password = (env.store.path.parent / "gpu" / rental_id / "web_password").read_text(encoding="utf-8")
    assert password not in html


def test_web_without_approved_manifest_says_so(env):
    config = {**env.config, "gpu": {"provider": "fake", "image": IMAGE}}
    page = TestClient(create_app(config)).get("/gpu").text
    assert "Chưa có manifest provisioning đã duyệt" in page


def test_cli_adds_and_approves_manifest(env, tmp_path, capsys):
    from studio import cli

    config = {**env.config, "gpu": {"provider": "fake", "image": IMAGE}}
    source = tmp_path / "manifest.json"
    source.write_text(gpu_manifest(downloads=[MODEL]).model_dump_json(), encoding="utf-8")
    cli.cmd_gpu(argparse.Namespace(gpu_command="manifest", manifest_command="add", file=str(source), note="flux"),
                config, env.store)
    out = capsys.readouterr().out
    sha = gpu_manifest(downloads=[MODEL]).sha256()
    assert "git_repos:" in out and f"{sha} pending_review" in out
    cli.cmd_gpu(argparse.Namespace(gpu_command="manifest", manifest_command="approve", sha256=sha), config, env.store)
    assert env.store.manifest(sha)["status"] == "approved"


def imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_comfyui_code_is_never_imported_and_web_only_uses_service():
    """ComfyUI (GPL-3.0) chỉ chạy như process riêng; web không gọi runtime hay probe trực tiếp (09 · s2.b3)."""
    comfy_modules = ("comfy", "comfy_extras", "comfy_execution", "nodes", "folder_paths", "execution", "server")
    for path in [*(STUDIO / "gpu").rglob("*.py"), *(STUDIO / "comfyui").rglob("*.py")]:
        assert not {n for n in imports(path) if n.split(".")[0] in comfy_modules}, path
    for path in (STUDIO / "web").rglob("*.py"):
        names = imports(path)
        assert not {n for n in names if n.startswith(("studio.comfyui", "studio.gpu.probe", "studio.gpu.adapters"))}, path


# ------------------------------------------------------------ race: destroy song song với lúc phát hiện lỗi provisioning


def test_destroy_racing_provisioning_failure_returns_destroyed_rental_without_raising(env, svc, fake_gpu, inst, server,
                                                                                     monkeypatch):
    from studio.gpu import service as service_module

    rental_id = rent(svc, fake_gpu, inst, server)
    inst.report = {"steps": {"runtime_setup": {"status": "failed", "attempt": 3, "error": "pip exploded"}}}
    stale = svc.rental(rental_id)                       # bản check_provisioning() đang giữ
    assert svc.destroy(rental_id).status == "destroyed"  # nút Destroy thắng race
    failure = service_module._Fail("runtime_setup", "pip_failed", "pip exploded")
    assert svc._fail_provisioning(stale, failure).status == "destroyed"

    # Cùng race qua đường check_provisioning: destroy xảy ra giữa lúc đọc rental và lúc phát hiện lỗi.
    other = rent(svc, fake_gpu, inst, server)
    original = svc._advance

    def advance_then_destroyed_elsewhere(rental, manifest):
        svc.destroy(rental.rental_id)
        original(rental, manifest)

    monkeypatch.setattr(svc, "_advance", advance_then_destroyed_elsewhere)
    assert svc.check_provisioning(other).status == "destroyed"
    assert fake_gpu.destroyed() == 2                    # mỗi rental đúng một lượt destroy thật
    assert "provisioning→provision_failed" not in [e["transition"] for e in events(env, other)]


def test_poll_keeps_going_when_one_rental_errors(env, svc, fake_gpu, inst, server, monkeypatch):
    first, second = rent(svc, fake_gpu, inst, server), rent(svc, fake_gpu, inst, server)
    inst.report = all_steps_done()
    original = svc.check_provisioning

    def flaky(rental_id):
        if rental_id == first:
            raise GPUServiceError("chuyển trạng thái không hợp lệ")
        return original(rental_id)

    monkeypatch.setattr(svc, "check_provisioning", flaky)
    results = {r.rental_id: r.status for r in svc.poll()}
    assert results == {first: "provisioning", second: "ready"}
    assert any(e["error_code"] == "poll_error" for e in events(env, first))


def test_concurrent_step_start_counts_one_attempt(env, svc, fake_gpu, inst, server):
    rental_id = rent(svc, fake_gpu, inst, server, boot=False)
    stale = {row["step_key"]: row for row in env.store.provision_steps(rental_id)}
    svc._start(dict(stale), rental_id, "host_boot")
    svc._start(dict(stale), rental_id, "host_boot")     # lượt poll thứ hai với snapshot cũ
    assert steps(env, rental_id)["host_boot"] == ("running", 1, None)
