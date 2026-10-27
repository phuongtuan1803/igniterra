"""IGN-UX giai đoạn 2: idle tính cả job trong ComfyUI gốc, và lưu workflow từ máy về thư viện (10 · s5; màn S12, S13).

ComfyUI giả của ``test_comfyui`` (``/queue``, ``/history``, ``/api/userdata``) trên 127.0.0.1, máy thuê qua
``FakeGPUProvider``. 0 USD, không gọi mạng ngoài.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from studio.comfyui.service import ComfyService, ComfyServiceError
from studio.gpu.adapters.fake import FakeGPUProvider
from studio.gpu.limits import Limits
from studio.gpu.service import GPUService
from studio.web import create_app
from test_comfyui import IMAGE, PORTAL, approved, fc, gui_workflow, ready_rental, server  # noqa: F401
from test_gpu import Clock
from test_gpu_fixed import TOKEN, add_host, csrf_of

GUI = gui_workflow([{"id": 1, "type": "EmptyImage", "properties": {"cnr_id": "comfy-core", "ver": "0.3.60"}},
                    {"id": 2, "type": "SaveImage", "properties": {"cnr_id": "comfy-core", "ver": "0.3.60"}}])


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gpu(env, clock):
    return GPUService(env.store, FakeGPUProvider(clock=clock), image=IMAGE, portal=PORTAL, clock=clock,
                      limits=Limits(idle_ttl_minutes=5, max_runtime_minutes=600, max_hourly_usd=1))


def deadline(gpu, rental_id):
    return gpu.rental(rental_id).idle_deadline


# ------------------------------------------------------------ idle theo ComfyUI gốc


def test_job_in_native_comfyui_extends_idle_and_idle_machine_still_stops(gpu, clock, fc, server):
    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    start = deadline(gpu, rental_id)
    clock.now += timedelta(minutes=4)
    fc.queue["native-1"] = "running"                       # bạn bấm Queue trong ComfyUI gốc
    assert gpu.tick()["native_activity"] == [rental_id]
    rental = gpu.rental(rental_id)
    assert rental.idle_deadline == clock.now + timedelta(minutes=5) > start
    assert rental.native_activity_at == clock.now and rental.status == "ready"
    assert fc.posts == []                                  # chỉ đọc, không gửi gì tới ComfyUI

    fc.finish("native-1")                                  # job xong giữa hai lượt: history đổi đầu
    clock.now += timedelta(minutes=4)
    assert gpu.tick()["native_activity"] == [rental_id]

    clock.now += timedelta(minutes=6)                      # quên tắt: không job nào nữa
    result = gpu.tick()
    assert result["native_activity"] == [] and [e.outcome for e in result["enforced"]] == ["destroy_sent"]
    assert gpu.rental(rental_id).status in ("destroying", "destroyed")


def test_open_tab_without_jobs_does_not_extend_idle(gpu, clock, fc, server):
    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    fc.finish("older-job")                                 # history đã có job từ trước
    start = deadline(gpu, rental_id)
    clock.now += timedelta(minutes=1)
    assert gpu.tick()["native_activity"] == []             # lượt đầu chỉ ghi mốc history
    clock.now += timedelta(minutes=1)
    assert gpu.tick()["native_activity"] == []
    rental = gpu.rental(rental_id)
    assert rental.idle_deadline == start and rental.native_activity_at is None
    assert rental.native_history_head == "older-job"


def test_unreachable_comfyui_keeps_idle_deadline(gpu, clock, fc, server):
    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    start = deadline(gpu, rental_id)
    fc.token = "đổi-mật-khẩu"                             # 401: không đọc được hàng đợi
    fc.queue["native-1"] = "running"
    clock.now += timedelta(minutes=2)
    assert gpu.tick()["native_activity"] == [] and deadline(gpu, rental_id) == start


def test_fixed_hosts_are_not_polled_for_idle(gpu, clock, fc, server):
    host = add_host(gpu, fc, server)
    fc.queue["native-1"] = "running"
    assert gpu.watch_native_activity() == [] and gpu.rental(host.rental_id).native_activity_at is None


# ------------------------------------------------------------ lưu workflow từ máy (S13)


def test_save_workflow_from_machine_as_new_or_new_revision(env, gpu, fc, server):
    comfy = ComfyService(env.store, gpu)
    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    fc.saved = {"portrait-fix.json": GUI, "tts/vi-draft.json": GUI.replace(b"0.3.60", b"0.3.61"), "notes.txt": b"x"}
    files = comfy.machine_workflows(rental_id)
    assert [f["path"] for f in files] == ["tts/vi-draft.json", "portrait-fix.json"]     # mới sửa trước, bỏ .txt

    first = comfy.import_from_machine(rental_id, "portrait-fix.json")
    assert (first.name, first.revision, first.format) == ("portrait-fix", 1, "gui")
    assert first.note == f"lưu từ máy {rental_id}: portrait-fix.json"
    again = comfy.import_from_machine(rental_id, "tts/vi-draft.json", workflow_id=first.workflow_id)
    assert (again.workflow_id, again.revision, again.name) == (first.workflow_id, 2, "portrait-fix")
    assert comfy.revision(first.workflow_id, 1).source_sha256 == first.source_sha256      # rev 1 không đổi
    assert again.status != "approved"                      # dependency vẫn phải duyệt

    for bad in ("../studio.json", "/etc/passwd.json", "a/../../b.json", "missing.json"):
        with pytest.raises(ComfyServiceError):
            comfy.import_from_machine(rental_id, bad)


def test_save_from_machine_web_flow(env, fc, server):
    config = {**env.config, "gpu": {"provider": "fake", "image": IMAGE, "portal": PORTAL}}
    client = TestClient(create_app(config), follow_redirects=False)
    gpu = client.app.state.gpu
    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    detail = client.get(f"/gpu/instances/{rental_id}").text
    assert f'href="/gpu/instances/{rental_id}/workflows"' in detail and "chưa thấy" in detail

    fc.saved = {}
    empty = client.get(f"/gpu/instances/{rental_id}/workflows").text
    assert "Chưa có workflow nào được Save" in empty and 'href="/comfyui/workflows"' in empty

    fc.saved = {"portrait-fix.json": GUI}
    listing = client.get(f"/gpu/instances/{rental_id}/workflows").text
    assert 'value="portrait-fix.json"' in listing and '<option value="new">Workflow mới</option>' in listing
    form = {"csrf": csrf_of(listing), "path": "portrait-fix.json", "target": "new"}
    assert client.post(f"/gpu/instances/{rental_id}/workflows/import", data={**form, "csrf": "sai"}).status_code == 403
    staged = client.post(f"/gpu/instances/{rental_id}/workflows/import", data=form)
    assert staged.status_code == 303 and re.match(r"/comfyui/import/st_[0-9a-f]{12}$", staged.headers["location"])
    check = client.get(staged.headers["location"]).text                    # S15: nguồn là máy nào, file nào
    assert f"lưu từ máy {rental_id}: portrait-fix.json" in check and "Chưa có bản chạy được" in check
    saved = client.post(staged.headers["location"], data={"csrf": form["csrf"], "action": "save", "kind": "fix"})
    assert saved.status_code == 303 and re.match(r"/comfyui/workflows/wf_[0-9a-f]+\?revision=1", saved.headers["location"])
    assert client.get(saved.headers["location"]).status_code == 200
    [item] = client.app.state.comfy.library()
    assert item["latest"].kind == "fix" and item["latest"].note == f"lưu từ máy {rental_id}: portrait-fix.json"

    fc.token = "sai"                                       # máy không đọc được: quay về trang máy, có lỗi
    failed = client.get(f"/gpu/instances/{rental_id}/workflows")
    assert failed.status_code == 303 and failed.headers["location"].startswith(f"/gpu/instances/{rental_id}?err=")


def test_save_from_fixed_host(env, gpu, fc, server):
    comfy = ComfyService(env.store, gpu)
    host = add_host(gpu, fc, server)
    fc.saved = {"vps.json": GUI}
    record = comfy.import_from_machine(host.rental_id, "vps.json")
    assert record.revision == 1 and record.class_types == ["EmptyImage", "SaveImage"]
