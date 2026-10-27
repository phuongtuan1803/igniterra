"""Isolated browser fixture. Fake providers only; never loads studio.toml from the repo.

Run with: uv run python tests/ui/fixture_server.py
All writes are in a new /tmp directory; only loopback socket connections are permitted.
Fixture-only control routes drive fake worker/provider outcomes, not production code.
"""
from __future__ import annotations

import json
import os
import sys
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

import uvicorn
from fastapi import Request
from fastapi.staticfiles import StaticFiles
from conftest import Env, approve_gpu_manifest, fake_plan_data
import test_comfyui as runtime_fixture
from studio.comfyui.manifest import WorkflowManifest
from studio.gpu import adapters
from studio.gpu.adapters.fake import FakeGPUProvider
from studio.gpu.contracts import OfferFilters, Quote
from studio.providers import fake
from studio.series import SeriesActions
from studio.web import create_app


def loopback_only(event, args):
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise RuntimeError(f"Browser fixture blocked a non-loopback connection: {address[0]}")


sys.addaudithook(loopback_only)
os.environ.pop("STUDIO_BASIC_AUTH", None)
fake.reset()
fake.SCRIPT["plan"] = fake_plan_data()
TEMP = tempfile.TemporaryDirectory(prefix="igniterra-ui-")
env = Env(Path(TEMP.name))
FIXED_TOKEN = "fixture-fixed-host-token"   # máy cố định (VPS) giả trên cùng server loopback


class RuntimeHandler(runtime_fixture.Handler):
    """Authenticated local Portal + ComfyUI using the existing regression runtime."""

    def handle_any(self):
        path = urlparse(self.path).path
        token = self.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in [FIXED_TOKEN, *(spec.env["WEB_PASSWORD"] for spec in provider.specs.values())]:
            return self.reply(401, {"error": "unauthorized"})
        runtime_fixture.STATE.token = token
        if path in ("/capabilities", "/provisioning/status"):
            return self.reply(200, {"complete": True, "disk_free_gb": 200, "steps": {
                key: {"status": "done", "attempt": 1} for key in ("runtime_setup", "custom_nodes", "models")}})
        if path == "/system_stats":
            return self.reply(200, {"system": {"comfyui_version": "fixture"}, "devices": [{"name": "Fake RTX 4090"}]})
        super().handle_any()
        # Smoke jobs complete immediately; user workflow jobs wait for fixture tick.
        for post in runtime_fixture.STATE.posts:
            if str(post.get("client_id", "")).startswith("igniterra:"):
                continue
            pid = post["prompt_id"]
            runtime_fixture.STATE.finish(pid)


server = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
address = f"127.0.0.1:{server.server_address[1]}"


class BrowserGPU(FakeGPUProvider):
    def create_instance(self, offer, spec):
        result = super().create_instance(offer, spec)
        self.boot(result.provider_instance_id, {"1111/tcp": address, "8188/tcp": address})
        return result


provider = BrowserGPU()
# The adapter factory returns this fake instance; no real adapter is instantiated.
adapters.get = lambda config: provider
config = {**env.config, "gpu": {"provider": "fake", "image": runtime_fixture.IMAGE,
                                "portal": {"scheme": "http", "timeout_s": 1}}}
app = create_app(config)
gpu, comfy = app.state.gpu, app.state.comfy
approve_gpu_manifest(app.state.store)

# Generate one real fake-provider keyframe task and one small fake render for review.
keyframes = env.new_task()
assert env.run(keyframes) == "waiting"
final = env.new_task()
assert env.run(final) == "waiting"
env.actions.decide(final, "approve")
assert env.run(final) == "waiting"
assert env.task(final)["wait_reason"] == "gate:final"
failed = env.new_task(name="brief-01")
env.store.set_status(failed, "failed", None, "Lỗi minh họa trong fixture")
paused = env.new_task()
env.store.set_status(paused, "waiting", "paused")
SeriesActions(env.store, env.config).create_from_task("browser-series", keyframes)

# Valid PNG from generated keyframes replaces the deliberately minimal unit-test PNG.
image = env.td(keyframes).selected("s01", "image")
runtime_fixture.STATE.files[runtime_fixture.OUTPUT["filename"]] = (
    (env.td(keyframes).root / image.path).read_bytes(), "image/png")

def rent():
    offer = provider.search_offers(OfferFilters())[0]
    return gpu.rent(gpu.new_rental_id(), Quote(offer_id=offer.offer_id, dph_total=offer.dph_total,
                    quoted_at=offer.quoted_at, confirmed=True), OfferFilters())


ready = rent()
gpu.reconcile(ready.rental_id)
assert gpu.rental(ready.rental_id).status == "ready"
runtime_fixture.STATE.saved = {name: json.dumps({"last_node_id": 1, "links": [], "version": 0.4, "nodes": [
    {"id": 1, "type": "SaveImage", "properties": {"cnr_id": "comfy-core", "ver": "0.3.60"}}]}).encode()
    for name in ("portrait-fix.json", "flux-klein-test.json", "tts/vi-draft.json")}
fixed = gpu.add_fixed_host(gpu.new_rental_id(), "VPS Hà Nội · A5000", f"http://{address}", FIXED_TOKEN, 0.2)
revision = comfy.import_workflow("Browser approved workflow", json.dumps(runtime_fixture.API_PROMPT).encode())
revision = comfy.attach_manifest(revision.workflow_id, revision.revision, WorkflowManifest(models=[runtime_fixture.MODEL]))
comfy.approve_manifest(revision.manifest_sha256)
pending = comfy.import_workflow("Browser pending workflow", json.dumps(runtime_fixture.API_PROMPT).encode())

metadata = {"keyframes": keyframes, "final": final, "failed": failed, "paused": paused,
            "ready": ready.rental_id, "fixed": fixed.rental_id, "workflow": revision.workflow_id, "pending": pending.workflow_id}


@app.get("/__fixture")
def fixture_state():
    return {**metadata, "rentals": [{"id": r.rental_id, "status": r.status} for r in gpu.rentals()],
            "runs": [{"id": r.run_id, "status": r.status} for r in comfy.runs()],
            "provider_calls": len(provider.calls), "posts": len(runtime_fixture.STATE.posts)}


@app.post("/__fixture/tick")
async def fixture_tick(request: Request):
    payload = await request.json()
    if payload.get("task"):
        task_id = payload["task"]
        if env.task(task_id)["status"] == "pending":
            env.run(task_id)
    if payload.get("fail_download"):
        runtime_fixture.STATE.view_mode = "error"
    if payload.get("restore_download"):
        runtime_fixture.STATE.view_mode = "ok"
    if payload.get("finish_runs"):
        for run in comfy.runs():
            if run.prompt_id and run.status in ("queued", "running", "submit_unknown"):
                runtime_fixture.STATE.finish(run.prompt_id)
        comfy.poll()
    if payload.get("approve_workflow"):
        workflow_id = payload["approve_workflow"]
        record = comfy.revision(workflow_id)
        manifest_file = Path(TEMP.name) / "browser-workflow-manifest.json"
        manifest_file.write_text(WorkflowManifest().model_dump_json())
        cli_env = {**os.environ, "STUDIO_ROOT": str(ROOT), "STUDIO_CONFIG": str(env.toml)}
        subprocess.run([sys.executable, "-m", "studio", "comfyui", "manifest", "attach", workflow_id,
                        str(record.revision), str(manifest_file)], env=cli_env, check=True, capture_output=True)
        record = comfy.revision(workflow_id)
        subprocess.run([sys.executable, "-m", "studio", "comfyui", "manifest", "approve", record.manifest_sha256],
                       env=cli_env, check=True, capture_output=True)
    if payload.get("destroy_unknown"):
        provider.fail["destroy_instance"] = "timeout_before_send"
    return fixture_state()


app.mount("/__reference", StaticFiles(directory=ROOT / "docs/ux/reference"), name="reference")
print("Isolated fake UI fixture ready on http://127.0.0.1:8765", flush=True)
uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
