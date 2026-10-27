"""IGN-UX giai đoạn 1: máy cố định (VPS có GPU) và nút "Mở ComfyUI" (10 · s4, J06; màn S10, S11, S12).

Máy cố định là ComfyUI giả của ``test_comfyui`` trên 127.0.0.1 (địa chỉ nội bộ, Bearer token). Máy thuê đi qua
``FakeGPUProvider``. 0 USD, không gọi mạng ngoài.
"""

from __future__ import annotations

import os
import re
import stat
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from studio.comfyui.service import ComfyService, ComfyServiceError
from studio.gpu import service as gpu_service
from studio.gpu.adapters.fake import FakeGPUProvider
from studio.gpu.limits import Limits
from studio.gpu.service import GPUService, GPUServiceError, UnverifiedOutput
from studio.web import create_app
from test_comfyui import IMAGE, PORTAL, STATE, approved, fc, ready_rental, server, start  # noqa: F401
from test_gpu import Clock

TOKEN = "fixed-host-token-123456"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gpu(env, clock):
    return GPUService(env.store, FakeGPUProvider(clock=clock), image=IMAGE, portal=PORTAL, clock=clock,
                      limits=Limits(idle_ttl_minutes=5, max_runtime_minutes=10, max_hourly_usd=1))


def add_host(gpu: GPUService, fc, server: str, **kwargs):
    fc.token = TOKEN
    args = {"name": "VPS Hà Nội", "url": f"http://{server}", "token": TOKEN, "hourly_usd": 0.2, **kwargs}
    return gpu.add_fixed_host(gpu.new_rental_id(), **args)


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


# ------------------------------------------------------------ địa chỉ


@pytest.mark.parametrize("url, private", [
    ("https://gpu.example.vn:8188", False),
    ("https://gpu.example.vn/comfy/", False),
    ("http://127.0.0.1:8188", True),
    ("http://192.168.1.20:8188", True),
    ("http://100.101.102.103:8188", True),       # Tailscale
    ("http://gpu.tail1234.ts.net:8188", True),
    ("http://localhost:8188", True),
])
def test_fixed_url_accepts_https_or_private_http(url, private):
    normalized, is_private = gpu_service._normalize_fixed_url(url)
    assert is_private is private and not normalized.endswith("/")


@pytest.mark.parametrize("url, reason", [
    ("http://gpu.example.vn:8188", "https"),
    ("http://8.8.8.8:8188", "https"),
    ("ftp://gpu.example.vn", "https://"),
    ("https://user:pass@gpu.example.vn", "mật khẩu"),
    ("https://gpu.example.vn/?token=abc", "query"),
    ("https://gpu.example.vn:99999", "cổng"),
    ("", "https://"),
])
def test_fixed_url_refuses_public_http_and_inline_secrets(url, reason):
    with pytest.raises(GPUServiceError, match=reason):
        gpu_service._normalize_fixed_url(url)


# ------------------------------------------------------------ thêm, chạy, gỡ


def test_fixed_host_is_checked_then_ready_without_entering_rental_guardrails(env, gpu, fc, server):
    host = add_host(gpu, fc, server)
    assert host.status == "ready" and host.provider_key == "fixed" and host.idle_deadline is None
    assert (host.offer.offer_id, host.offer.gpu_name, host.offer.gpu_ram_gb) == ("VPS Hà Nội", "NVIDIA RTX A5000", 24)
    assert host.offer.dph_total == 0.2 and host.provider_instance_id is None
    assert gpu.rentals() == [] and [h.rental_id for h in gpu.machines()] == [host.rental_id]
    token_file = gpu.rental_dir(host.rental_id) / gpu_service.PASSWORD_FILE
    assert token_file.read_text() == TOKEN and stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert TOKEN not in str(env.store.rental(host.rental_id)) and TOKEN not in str(env.store.gpu_events(host.rental_id))
    assert gpu.redact(f"x {TOKEN} y") == "x <WEB_PASSWORD> y"
    assert gpu.comfy_url(host.rental_id) == f"http://{server}"
    assert gpu.cost(host.rental_id)["accrued_est"] == 0 and env.store.cost_samples(host.rental_id) == []

    gpu.clock.now += timedelta(hours=3)                      # quá idle TTL, max_runtime, nhưng không phải máy thuê
    gpu.tick()
    assert gpu.rental(host.rental_id).status == "ready" and gpu.over_limits() == []
    assert not any(call[0] in ("destroy_instance", "stop_instance") for call in gpu.provider.calls)


def test_fixed_host_refuses_wrong_or_missing_token_and_saves_nothing(env, gpu, fc, server):
    fc.token = TOKEN
    with pytest.raises(GPUServiceError, match="bị từ chối"):
        gpu.add_fixed_host(gpu.new_rental_id(), "VPS", f"http://{server}", "sai")
    with pytest.raises(GPUServiceError, match="cần xác thực"):
        gpu.add_fixed_host(gpu.new_rental_id(), "VPS", f"http://{server}", "")
    with pytest.raises(GPUServiceError, match="không kết nối"):
        gpu.add_fixed_host(gpu.new_rental_id(), "VPS", "http://127.0.0.1:9", TOKEN)
    assert env.store.rentals() == []
    add_host(gpu, fc, server, name="VPS")
    with pytest.raises(GPUServiceError, match="đã có"):
        add_host(gpu, fc, server, name="VPS")


def test_public_host_answering_without_token_is_refused(gpu, fc, server, monkeypatch):
    monkeypatch.setattr(gpu_service, "_normalize_fixed_url", lambda url: (url, False))
    fc.open = True
    with pytest.raises(GPUServiceError, match="công khai"):
        add_host(gpu, fc, server)
    fc.open = False
    assert add_host(gpu, fc, server).status == "ready"


def test_private_host_without_auth_is_allowed(gpu, fc, server):
    fc.open = True
    host = gpu.add_fixed_host(gpu.new_rental_id(), "LAN box", f"http://{server}", "")
    assert host.status == "ready"
    assert not (gpu.rental_dir(host.rental_id) / gpu_service.PASSWORD_FILE).exists()


def test_workflow_runs_on_fixed_host_one_run_at_a_time(env, gpu, fc, server):
    comfy = ComfyService(env.store, gpu)
    host = add_host(gpu, fc, server)
    assert [r.rental_id for r in comfy.ready_rentals()] == [host.rental_id]
    run = start(comfy, *approved(comfy), host.rental_id)
    assert run.status == "queued" and gpu.rental(host.rental_id).status == "busy"
    with pytest.raises(ComfyServiceError, match="busy"):
        start(comfy, *approved(comfy), host.rental_id)
    fc.finish(run.prompt_id)
    assert comfy.refresh(run.run_id).status == "succeeded"
    host = gpu.rental(host.rental_id)
    assert host.status == "ready" and host.idle_deadline is None and env.store.cost_samples(host.rental_id) == []


def test_fixed_host_has_no_stop_or_destroy_and_remove_respects_g11(env, gpu, fc, server):
    comfy = ComfyService(env.store, gpu)
    host = add_host(gpu, fc, server)
    for action in (gpu.stop, gpu.destroy):
        with pytest.raises(GPUServiceError, match="máy cố định"):
            action(host.rental_id)
    with pytest.raises(GPUServiceError, match="máy cố định"):
        gpu.web_login(host.rental_id)
    run = start(comfy, *approved(comfy), host.rental_id)
    with pytest.raises(GPUServiceError, match="busy"):
        gpu.remove_fixed_host(host.rental_id)
    fc.finish(run.prompt_id)
    fc.view_mode = "error"
    assert comfy.refresh(run.run_id).status != "succeeded"
    with pytest.raises(UnverifiedOutput):
        gpu.remove_fixed_host(host.rental_id)
    removed = gpu.remove_fixed_host(host.rental_id, accept_unverified_output=True)
    assert removed.status == "destroyed" and comfy.run(run.run_id).status == "abandoned"
    assert gpu.fixed_hosts() == [] and not (gpu.rental_dir(host.rental_id) / gpu_service.PASSWORD_FILE).exists()
    assert add_host(gpu, fc, server).status == "ready"         # tên cũ dùng lại được sau khi gỡ


# ------------------------------------------------------------ web: S10, S11, S12


def fixed_web(env, provider: str = "fixed") -> TestClient:
    config = {**env.config, "gpu": {"provider": provider, "image": IMAGE, "portal": PORTAL}}
    return TestClient(create_app(config), follow_redirects=False)


def test_fixed_only_web_adds_host_lists_it_and_opens_comfyui(env, fc, server):
    fc.token = TOKEN
    client = fixed_web(env)
    page = client.get("/gpu").text
    assert "Thêm máy cố định" in page and 'data-tab="gpu-offers"' not in page and "Tìm GPU thuê" not in page
    form = client.get("/gpu/hosts/new").text
    rental_id = re.search(r'name="rental_id" value="(r_[0-9a-f]{12})"', form).group(1)
    data = {"csrf": csrf_of(form), "rental_id": rental_id, "name": "VPS Hà Nội", "url": f"http://{server}",
            "token": TOKEN, "hourly": "0.2"}
    assert client.post("/gpu/hosts", data={**data, "csrf": "sai"}).status_code == 403

    check = client.post("/gpu/hosts", data={**data, "intent": "check"}, headers={"HX-Request": "true"})
    assert check.status_code == 200 and "Kết nối được" in check.text and "NVIDIA RTX A5000" in check.text
    assert "<html" not in check.text and env.store.rentals() == []
    bad = client.post("/gpu/hosts", data={**data, "token": "sai"})
    assert bad.status_code == 200 and "bị từ chối" in bad.text and env.store.rentals() == []

    saved = client.post("/gpu/hosts", data=data)
    assert saved.status_code == 303 and saved.headers["location"].startswith(f"/gpu/instances/{rental_id}")
    listing = client.get("/gpu").text
    assert "Cố định · VPS" in listing and "tự khai, ngoài ngân sách" in listing
    assert f'href="/gpu/instances/{rental_id}/comfyui"' in listing and "Mở ComfyUI" in listing
    detail = client.get(f"/gpu/instances/{rental_id}").text
    assert "Gỡ khỏi danh sách" in detail and "Destroy" not in detail and "Hiện mật khẩu" not in detail
    for html in (listing, detail, check.text):
        assert TOKEN not in html
    opened = client.get(f"/gpu/instances/{rental_id}/comfyui")
    assert opened.status_code == 303 and opened.headers["location"] == f"http://{server}/"

    removed = client.post(f"/gpu/instances/{rental_id}/remove", data={"csrf": csrf_of(detail), "confirm": "on"})
    assert removed.status_code == 303 and client.app.state.gpu.rental(rental_id).status == "destroyed"
    assert f"/gpu/instances/{rental_id}/comfyui" not in client.get("/gpu").text


def test_rented_machine_opens_comfyui_and_shows_login_only_on_post(env, fc, server):
    client = fixed_web(env, "fake")
    gpu = client.app.state.gpu
    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    password = fc.token
    listing = client.get("/gpu").text
    assert "Thuê · Fake" in listing and f'href="/gpu/instances/{rental_id}/comfyui"' in listing
    detail = client.get(f"/gpu/instances/{rental_id}").text
    assert "Hiện mật khẩu đăng nhập" in detail and password not in detail and password not in listing
    opened = client.get(f"/gpu/instances/{rental_id}/comfyui")
    assert opened.status_code == 303 and opened.headers["location"] == f"http://{server}/"
    assert password not in opened.headers["location"]
    assert client.post(f"/gpu/instances/{rental_id}/login", data={"csrf": "sai"}).status_code == 403
    login = client.post(f"/gpu/instances/{rental_id}/login", data={"csrf": csrf_of(detail)})
    assert login.status_code == 200 and password in login.text and "<code>fake</code>" in login.text
    assert login.headers["cache-control"] == "no-store"


def test_open_comfyui_refuses_machine_that_is_not_ready(env, fc, server):
    client = fixed_web(env, "fake")
    gpu = client.app.state.gpu
    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    gpu.destroy(rental_id)
    response = client.get(f"/gpu/instances/{rental_id}/comfyui")
    assert response.status_code == 303 and response.headers["location"].startswith(f"/gpu/instances/{rental_id}?err=")
