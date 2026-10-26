"""VastGPUProvider với server REST giả trên 127.0.0.1 (09 · Kiểm chứng: "Vast REST giả"), 0 USD, không gọi mạng.

Server mô phỏng hình dạng JSON của ``/bundles/``, ``/asks/{id}/``, ``/instances/`` và ghi lại mọi request để
kiểm điều server thật sự nhận (label, ``cancel_unavail``, Bearer), không chỉ điều client nói đã gửi.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from conftest import approve_gpu_manifest
from studio import config as studio_config
from studio.gpu import adapters
from studio.gpu import costs
from studio.gpu.adapters.base import GPUError, GPUProvider, GPUUnknown
from studio.gpu.adapters.vast import VastGPUProvider
from studio.gpu.contracts import InstanceSpec, OfferFilters, Quote
from studio.gpu.service import GPUService

KEY = "test-vast-key-0123456789"
IMAGE = "example/comfyui@sha256:" + "0" * 64
NOW = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent.parent

OFFERS = [   # gpu_ram theo MB như API; reliability2 là trường vast-cli hiện
    {"id": 1002, "gpu_name": "RTX 4090", "num_gpus": 1, "gpu_ram": 24564, "disk_space": 120.5, "reliability2": 0.991,
     "cuda_max_good": 12.4, "verified": True, "rentable": True, "dph_total": 0.35, "storage_cost": 0.1,
     "inet_up_cost": 0.01, "inet_down_cost": 0.02, "geolocation": "Vietnam, VN"},
    {"id": 1004, "gpu_name": "A100 SXM4", "num_gpus": 1, "gpu_ram": 81920, "disk_space": 200, "reliability": 0.995,
     "cuda_max_good": 12.2, "verification": "verified", "rentable": True, "dph_total": 1.1, "storage_cost": 0.15},
    {"id": 1099, "gpu_name": "broken"},                                     # thiếu trường bắt buộc: bỏ
]


class State:
    def __init__(self):
        self.requests: list[dict] = []
        self.instances: dict[int, dict] = {}
        self.fail: dict[str, str] = {}          # "METHOD /path-prefix" → mode, dùng một lần
        self.next_id = 7001
        self.lock = threading.Lock()


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.handle_any()

    def do_POST(self):
        self.handle_any()

    def do_PUT(self):
        self.handle_any()

    def do_DELETE(self):
        self.handle_any()

    def handle_any(self):
        url = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        body = json.loads(raw) if raw else None
        route = f"{self.command} {url.path}"
        with STATE.lock:
            STATE.requests.append({"method": self.command, "path": url.path, "query": parse_qs(url.query),
                                   "auth": self.headers.get("authorization"), "body": body})
            mode = next((STATE.fail.pop(k) for k in list(STATE.fail) if route.startswith(k)), None)
        if self.headers.get("authorization") != f"Bearer {KEY}":
            return self.reply(401, {"error": "invalid api key"})
        if mode in ("429", "500", "503", "401"):
            return self.reply(int(mode), {"error": f"mode {mode}"})
        if mode == "timeout_noop":                      # chậm và không làm gì
            time.sleep(1.5)
            return self.reply(500, {"error": "late"})
        if mode == "badjson":
            return self.reply(200, None, raw=b"<html>gateway</html>")
        if mode == "refused":
            return self.reply(200, {"success": False, "msg": "no_such_ask"})
        status, data = self.route(url.path, body)
        if mode == "timeout":                           # provider đã làm, response tới muộn hơn timeout client
            time.sleep(1.5)
        self.reply(status, data)

    def route(self, path: str, body):
        with STATE.lock:
            if path == "/api/v0/bundles/":
                return 200, {"offers": OFFERS}
            if m := re.fullmatch(r"/api/v0/asks/(\d+)/", path):
                offer = next(o for o in OFFERS if o["id"] == int(m.group(1)))
                instance_id, STATE.next_id = STATE.next_id, STATE.next_id + 1
                STATE.instances[instance_id] = {"id": instance_id, "label": body["label"], "actual_status": None,
                                                "dph_total": offer["dph_total"], "public_ipaddr": "203.0.113.9",
                                                "ports": {}}
                return 200, {"success": True, "new_contract": instance_id}
            if path == "/api/v0/instances/":
                return 200, {"instances": list(STATE.instances.values())}
            if m := re.fullmatch(r"/api/v0/instances/(\d+)/", path):
                instance_id = int(m.group(1))
                if self.command == "GET":
                    return 200, {"instances": STATE.instances.get(instance_id)}
                if instance_id not in STATE.instances:
                    return 404, {"success": False, "error": "no_such_instance"}
                if self.command == "PUT":
                    STATE.instances[instance_id]["actual_status"] = body["state"]
                    return 200, {"success": True}
                if self.command == "DELETE":
                    del STATE.instances[instance_id]
                    return 200, {"success": True, "msg": "destroying"}
        return 404, {"error": "no route"}

    def reply(self, status: int, data, raw: bytes | None = None):
        payload = raw if raw is not None else json.dumps(data).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):   # client đã bỏ đi vì timeout
            pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/api/v0"
    httpd.shutdown()


@pytest.fixture
def state():
    STATE.__init__()
    return STATE


@pytest.fixture
def config(server, state, monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", KEY)
    return {"gpu": {"provider": "vast", "image": IMAGE, "vast": {"base_url": server, "timeout_s": 0.5,
                                                                 "connect_timeout_s": 0.5}}}


@pytest.fixture
def vast(config):
    return VastGPUProvider(config, clock=lambda: NOW)


def seen(state, method: str, path: str) -> list[dict]:
    return [r for r in state.requests if r["method"] == method and r["path"] == path]


def spec(rental_id: str = "r_00000000000a") -> InstanceSpec:
    return InstanceSpec(label=f"igniterra:{rental_id}", image=IMAGE, disk_gb=60)


# ------------------------------------------------------------ đăng ký, khóa


def test_registered_and_satisfies_protocol(config):
    provider = adapters.get(config)
    assert isinstance(provider, VastGPUProvider) and isinstance(provider, GPUProvider)
    assert adapters.REGISTRY["vast"] == "studio.gpu.adapters.vast:VastGPUProvider"


def test_missing_key_is_refused_before_any_call(config, state, monkeypatch):
    monkeypatch.delenv("VAST_API_KEY")
    with pytest.raises(GPUError, match="VAST_API_KEY"):
        VastGPUProvider(config)
    assert state.requests == []


def test_key_is_redacted(monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", KEY)
    assert "VAST_API_KEY" in studio_config.SECRET_ENV
    assert KEY not in studio_config.redact(f"401 Bearer {KEY} sai") and "<VAST_API_KEY>" in studio_config.redact(KEY)


def test_vastai_package_is_not_a_dependency():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "vastai" not in text


# ------------------------------------------------------------ endpoint


def test_search_offers_posts_bundles_and_maps_units(vast, state):
    offers = vast.search_offers(OfferFilters(min_gpu_ram_gb=16, disk_gb=60, min_reliability=0.95, min_cuda=12.0,
                                             max_dph=2.0, limit=5))
    [request] = seen(state, "POST", "/api/v0/bundles/")
    query = request["body"]
    assert request["auth"] == f"Bearer {KEY}"
    assert query["rentable"] == {"eq": True} and query["verified"] == {"eq": True}
    assert query["gpu_ram"] == {"gte": 16000} and query["disk_space"] == {"gte": 60}
    assert query["reliability"] == {"gte": 0.95} and query["cuda_max_good"] == {"gte": 12.0}
    assert query["dph_total"] == {"lte": 2.0} and query["limit"] == 5 and query["allocated_storage"] == 60
    assert [o.offer_id for o in offers] == ["1002", "1004"]                 # dòng thiếu trường bị bỏ
    first, second = offers
    assert first.gpu_ram_gb == pytest.approx(24.564) and first.disk_space_gb == 120.5
    assert first.reliability == 0.991 and first.verified and first.storage_cost == 0.1
    assert second.reliability == 0.995 and second.verified                  # verification == "verified"
    assert all(o.quoted_at == NOW and o.provider_key == "vast" for o in offers)


def test_search_without_verified_only_does_not_filter_verified(vast, state):
    vast.search_offers(OfferFilters(verified_only=False))
    assert "verified" not in seen(state, "POST", "/api/v0/bundles/")[0]["body"]


def test_create_sends_label_and_cancel_unavail(vast, state):
    offer = vast.search_offers(OfferFilters())[0]
    instance = vast.create_instance(offer, spec())
    [request] = seen(state, "PUT", "/api/v0/asks/1002/")
    body = request["body"]
    assert body["label"] == "igniterra:r_00000000000a" and body["cancel_unavail"] is True
    assert body["image"] == IMAGE and body["disk"] == 60
    assert request["auth"] == f"Bearer {KEY}"
    assert instance.provider_instance_id == "7001" and instance.label == "igniterra:r_00000000000a"


def test_create_refuses_label_without_app_prefix(vast, state):
    offer = vast.search_offers(OfferFilters())[0]
    bad = InstanceSpec(label="someone-else", image=IMAGE, disk_gb=60)
    with pytest.raises(GPUError):
        vast.create_instance(offer, bad)
    assert seen(state, "PUT", "/api/v0/asks/1002/") == []


def test_get_instance_maps_status_ports_price(vast, state):
    state.instances[42] = {"id": 42, "label": "igniterra:r_1", "actual_status": "running", "dph_total": 0.351,
                           "public_ipaddr": "203.0.113.9",
                           "ports": {"8188/tcp": [{"HostIp": "0.0.0.0", "HostPort": "40188"}], "22/tcp": []}}
    instance = vast.get_instance("42")
    assert instance.actual_status == "running" and instance.dph_total == 0.351
    assert instance.ports == {"8188/tcp": "203.0.113.9:40188"}
    assert seen(state, "GET", "/api/v0/instances/42/")[0]["query"] == {"owner": ["me"]}
    assert vast.get_instance("43") is None                                  # {"instances": null}: không còn


def test_list_owned_instances_keeps_only_app_label_prefix(vast, state):
    for instance_id, label in [(1, "igniterra:r_aaaaaaaaaaaa"), (2, "other-project"), (3, None),
                               (4, "xigniterra:r_bbbbbbbbbbbb"), (5, "igniterra:r_cccccccccccc")]:
        state.instances[instance_id] = {"id": instance_id, "label": label, "actual_status": "running"}
    owned = vast.list_owned_instances()
    assert [i.provider_instance_id for i in owned] == ["1", "5"]
    assert len(seen(state, "GET", "/api/v0/instances/")) == 1


def test_stop_and_destroy_requests(vast, state):
    state.instances[9] = {"id": 9, "label": "igniterra:r_1", "actual_status": "running"}
    vast.stop_instance("9")
    assert seen(state, "PUT", "/api/v0/instances/9/")[0]["body"] == {"state": "stopped"}
    assert vast.get_instance("9").actual_status == "stopped"
    vast.destroy_instance("9")
    assert len(seen(state, "DELETE", "/api/v0/instances/9/")) == 1
    assert vast.get_instance("9") is None and vast.list_owned_instances() == []


def test_live_verified_price_includes_disk_once_and_stopped_keeps_disk(env, svc, vast):
    offer = vast.search_offers(OfferFilters())[0]
    assert offer.dph_total_includes_disk
    assert costs.hourly(offer, 60) == offer.dph_total
    rental = svc.rent(svc.new_rental_id(), Quote(offer_id=offer.offer_id, dph_total=offer.dph_total,
                                               quoted_at=offer.quoted_at, confirmed=True), OfferFilters())
    cost = svc.cost(rental.rental_id)
    disk = offer.storage_cost * 60 / 720
    assert cost["dph_total_includes_disk"]
    assert cost["compute_per_h"] == pytest.approx(offer.dph_total - disk)
    assert cost["compute_per_h"] + cost["disk_per_h"] == pytest.approx(offer.dph_total)
    svc.stop(rental.rental_id)
    assert svc.cost(rental.rental_id)["compute_per_h"] == 0
    assert svc.cost(rental.rental_id)["disk_per_h"] == pytest.approx(disk)


# ------------------------------------------------------------ ánh xạ lỗi: GPUError (chưa làm) / GPUUnknown (không rõ)


@pytest.mark.parametrize("mode, unknown", [("429", False), ("401", False), ("refused", False), ("500", True),
                                           ("503", True), ("timeout", True), ("badjson", True)])
def test_error_mapping(vast, state, mode, unknown):
    offer = vast.search_offers(OfferFilters())[0]
    state.fail["PUT /api/v0/asks/"] = mode
    with pytest.raises(GPUError) as error:
        vast.create_instance(offer, spec())
    assert isinstance(error.value, GPUUnknown) is unknown, error.value


def test_bad_key_is_rejected_not_unknown(config, state, monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", "wrong-key-xyz")
    with pytest.raises(GPUError) as error:
        VastGPUProvider(config).list_owned_instances()
    assert not isinstance(error.value, GPUUnknown) and "401" in str(error.value)


def test_connection_refused_is_gpu_error(config):
    config["gpu"]["vast"]["base_url"] = "http://127.0.0.1:9/api/v0"
    with pytest.raises(GPUError) as error:
        VastGPUProvider(config).list_owned_instances()
    assert not isinstance(error.value, GPUUnknown)


def test_connect_timeout_is_gpu_error(vast, monkeypatch):
    """Không mở được kết nối thì request chưa tới provider (giả lập ConnectTimeout, không cần mạng)."""
    def connect_timeout(*args, **kwargs):
        raise requests.exceptions.ConnectTimeout("connect timed out")

    monkeypatch.setattr(requests, "request", connect_timeout)
    with pytest.raises(GPUError) as error:
        vast.destroy_instance("1")
    assert not isinstance(error.value, GPUUnknown)


def test_timeout_after_provider_received_create_is_unknown(vast, state):
    offer = vast.search_offers(OfferFilters())[0]
    state.fail["PUT /api/v0/asks/"] = "timeout"
    with pytest.raises(GPUUnknown):
        vast.create_instance(offer, spec())
    assert len(seen(state, "PUT", "/api/v0/asks/1002/")) == 1               # provider đã nhận
    [owned] = vast.list_owned_instances()                                    # và đã tạo
    assert owned.label == "igniterra:r_00000000000a"


def test_get_404_means_gone(vast, state, monkeypatch):
    state.fail["GET /api/v0/instances/77/"] = "401"
    with pytest.raises(GPUError):
        vast.get_instance("77")

    class NotFound:
        status_code, text = 404, '{"error": "not found"}'

    monkeypatch.setattr(requests, "request", lambda *a, **k: NotFound())
    assert vast.get_instance("77") is None


# ------------------------------------------------------------ service + adapter thật (G04, G09)


@pytest.fixture
def svc(env, vast):
    approve_gpu_manifest(env.store)
    return GPUService(env.store, vast, image=IMAGE, clock=lambda: NOW)


def rent(svc, vast) -> str:
    offer = vast.search_offers(OfferFilters())[0]
    rental_id = svc.new_rental_id()
    quote = Quote(offer_id=offer.offer_id, dph_total=offer.dph_total, quoted_at=offer.quoted_at, confirmed=True)
    svc.rent(rental_id, quote, OfferFilters())
    return rental_id


def test_g04_create_timeout_reconciles_by_label_against_rest(env, svc, vast, state):
    state.fail["PUT /api/v0/asks/"] = "timeout"
    rental_id = rent(svc, vast)
    rental = svc.rental(rental_id)
    assert rental.status == "provisioning" and rental.provider_instance_id == "7001"
    events = [e["transition"] for e in env.store.gpu_events(rental_id)]
    assert "creating→create_unknown" in events and "create_unknown→provisioning" in events
    assert len([r for r in state.requests if r["method"] == "PUT" and r["path"].startswith("/api/v0/asks/")]) == 1


def test_rent_sends_auth_env_and_manifest_onstart_but_never_stores_password(env, svc, vast, state):
    """Lát C: server REST thật sự nhận WEB_PASSWORD + ENABLE_AUTH, không AUTH_EXCLUDE; mật khẩu không vào SQLite."""
    import base64
    import sqlite3

    rental_id = rent(svc, vast)
    [request] = seen(state, "PUT", "/api/v0/asks/1002/")
    sent_env = request["body"]["env"]
    password = (env.store.path.parent / "gpu" / rental_id / "web_password").read_text(encoding="utf-8")
    assert sent_env["WEB_PASSWORD"] == password and len(password) >= 24
    assert sent_env["ENABLE_AUTH"] == "true" and "AUTH_EXCLUDE" not in sent_env
    assert sent_env["PROVISIONING_MANIFEST"] == "/etc/igniterra/provisioning.yaml"
    encoded = re.search(r"'([A-Za-z0-9+/=]+)' \| base64 -d", request["body"]["onstart"]).group(1)
    yaml_text = (env.store.path.parent / "gpu" / rental_id / "provisioning.yaml").read_text(encoding="utf-8")
    assert base64.b64decode(encoded).decode() == yaml_text and "action: \"destroy\"" in yaml_text
    with sqlite3.connect(env.store.path) as db:
        dump = "\n".join(db.iterdump())
    assert password not in dump


def test_g04_5xx_before_acting_stays_unknown_and_never_recreates(env, svc, vast, state):
    state.fail["PUT /api/v0/asks/"] = "500"
    rental_id = rent(svc, vast)
    assert svc.rental(rental_id).status == "create_unknown"
    assert svc.reconcile(rental_id).status == "create_unknown"
    assert len([r for r in state.requests if r["path"].startswith("/api/v0/asks/")]) == 1


def test_g04_429_is_rejected_not_unknown(env, svc, vast, state):
    state.fail["PUT /api/v0/asks/"] = "429"
    rental_id = rent(svc, vast)
    assert svc.rental(rental_id).status == "create_failed"
    assert env.store.gpu_operations(rental_id)[0]["status"] == "rejected" and state.instances == {}


def test_g09_destroy_timeout_keeps_record_then_confirms_gone(env, svc, vast, state):
    rental_id = rent(svc, vast)
    state.fail["DELETE /api/v0/instances/"] = "timeout"
    rental = svc.destroy(rental_id)
    # Server đã xóa trước khi response tới muộn: reconcile ngay trong lượt và thấy đã mất khỏi get và list.
    assert rental.status == "destroyed" and rental.destroy_confirmed_at is not None
    assert "destroying→destroy_unknown" in [e["transition"] for e in env.store.gpu_events(rental_id)]
    assert len(seen(state, "DELETE", "/api/v0/instances/7001/")) == 1


def test_g09_destroy_5xx_stays_unknown_and_is_not_resent(env, svc, vast, state):
    rental_id = rent(svc, vast)
    state.fail["DELETE /api/v0/instances/"] = "500"
    assert svc.destroy(rental_id).status == "destroy_unknown"
    assert svc.reconcile(rental_id).status == "destroy_unknown"             # vẫn còn trong list
    with pytest.raises(Exception, match="không gửi destroy lần nữa"):
        svc.destroy(rental_id)
    assert len(seen(state, "DELETE", "/api/v0/instances/7001/")) == 1
    del state.instances[7001]                                               # người vận hành hủy tay
    assert svc.reconcile(rental_id).status == "destroyed"
