"""Gate 4 lát A: GPU thuê với FakeGPUProvider, 0 USD, không gọi mạng (09 · Kiểm chứng G01–G04)."""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio.gpu import adapters
from studio.gpu.adapters.base import GPUError, GPUProvider
from studio.gpu.adapters.fake import FakeGPUProvider
from studio.gpu.contracts import Capabilities, Instance, InstanceSpec, Offer, OfferFilters, Quote
from studio.gpu.service import GPUService, GPUServiceError, check_offer
from studio.web import create_app

STUDIO = Path(__file__).resolve().parent.parent / "studio"
IMAGE = "example/comfyui@sha256:" + "0" * 64


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def fake_gpu(clock):
    return FakeGPUProvider(clock=clock)


@pytest.fixture
def svc(env, fake_gpu, clock):
    return GPUService(env.store, fake_gpu, image=IMAGE, clock=clock)


def quote(provider, offer_id: str, *, confirmed: bool = True, filters: OfferFilters | None = None) -> Quote:
    offer = next(o for o in provider.search_offers(filters or OfferFilters(min_gpu_ram_gb=0, disk_gb=0, min_reliability=0,
                                                                            verified_only=False))
                 if o.offer_id == offer_id)
    return Quote(offer_id=offer.offer_id, dph_total=offer.dph_total, quoted_at=offer.quoted_at, confirmed=confirmed)


def transitions(env, rental_id: str) -> list[str]:
    return [e["transition"] for e in env.store.gpu_events(rental_id)]


def gpu_web(env, provider: str = "fake") -> TestClient:
    config = {**env.config, "gpu": {"provider": provider, "image": IMAGE, "quote_ttl_s": 300}}
    return TestClient(create_app(config), follow_redirects=False)


def rent_form(client: TestClient, offer_id: str, **extra) -> dict:
    """Lấy form thuê như người dùng thấy trên trang (csrf, rental_id, báo giá)."""
    return rent_form_from(client.get("/gpu/offers").text, offer_id, **extra)


def rent_form_from(html: str, offer_id: str, **extra) -> dict:
    form = re.search(rf'<form method="post" action="/gpu/instances" class="offer-rent">(?:(?!</form>).)*'
                     rf'name="offer_id" value="{offer_id}"(?:(?!</form>).)*</form>', html, re.S).group(0)
    fields = dict(re.findall(r'type="hidden" name="(\w+)" value="([^"]*)"', form))
    return {**fields, "confirm": "on", **extra}


# ------------------------------------------------------------ G01: đổi provider không sửa service, web


def imports(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_g01_service_and_web_never_import_concrete_adapter():
    web = list((STUDIO / "web").rglob("*.py"))
    for path in web:
        assert not {n for n in imports(path) if n.startswith("studio.gpu.adapters")}, path
    concrete = {n for n in imports(STUDIO / "gpu" / "service.py") if n.startswith("studio.gpu.adapters.")}
    assert concrete <= {"studio.gpu.adapters.base"}
    texts = [p.read_text(encoding="utf-8") for p in [STUDIO / "gpu" / "service.py", *web,
                                                     *(STUDIO / "web" / "templates").glob("*gpu*.html")]]
    assert not any("vast" in text.lower() for text in texts)
    assert "FakeGPUProvider" not in (STUDIO / "gpu" / "service.py").read_text(encoding="utf-8")


class OtherProvider:
    """Provider viết độc lập (không kế thừa Fake): service và web phải chạy y nguyên."""

    key = "other"

    def __init__(self, config: dict | None = None):
        self.created: list[InstanceSpec] = []

    def capabilities(self) -> Capabilities:
        return Capabilities(supports_stop=False)

    def search_offers(self, filters: OfferFilters) -> list[Offer]:
        return [Offer(provider_key=self.key, offer_id="x1", gpu_name="L40S", gpu_ram_gb=48, disk_space_gb=500,
                      reliability=0.99, verified=True, dph_total=0.8, quoted_at=datetime.now(timezone.utc))]

    def create_instance(self, offer: Offer, spec: InstanceSpec) -> Instance:
        self.created.append(spec)
        return Instance(provider_instance_id="o-1", label=spec.label, actual_status="loading")

    def get_instance(self, instance_id: str) -> Instance | None:
        return None

    def list_owned_instances(self) -> list[Instance]:
        return []

    def stop_instance(self, instance_id: str) -> None:
        raise NotImplementedError

    def destroy_instance(self, instance_id: str) -> None:
        pass


def test_g01_swap_provider_by_config_only(env, monkeypatch):
    assert isinstance(FakeGPUProvider(), GPUProvider) and isinstance(OtherProvider(), GPUProvider)
    monkeypatch.setitem(adapters.REGISTRY, "other", f"{__name__}:OtherProvider")
    client = gpu_web(env, "other")
    assert "Provider · other" in client.get("/gpu").text
    response = client.post("/gpu/instances", data=rent_form(client, "x1"))
    assert response.status_code == 303 and response.headers["location"].startswith("/gpu/instances/r_")
    provider = client.app.state.gpu.provider
    assert isinstance(provider, OtherProvider) and len(provider.created) == 1
    assert provider.created[0].label.startswith("igniterra:r_") and provider.created[0].cancel_unavail
    rental = env.store.rentals()[0]
    assert rental["status"] == "provisioning" and rental["provider_key"] == "other"


def test_gpu_page_when_provider_off(env):
    client = TestClient(create_app(env.config), follow_redirects=False)
    page = client.get("/gpu")
    assert page.status_code == 200 and "GPU provider chưa bật" in page.text and 'href="/gpu"' in page.text
    assert client.get("/gpu/instances/r_000000000000").status_code == 404


# ------------------------------------------------------------ G02: offer rẻ nhưng thiếu VRAM/disk


def test_g02_search_drops_cheap_offer_missing_vram_or_disk(env, clock):
    provider = FakeGPUProvider(clock=clock, honor_filters=False)   # provider không lọc giúp
    svc = GPUService(env.store, provider, image=IMAGE, clock=clock)
    offers = svc.search_offers(OfferFilters())
    assert [o.offer_id for o in offers] == ["1002", "1004"]
    cheapest = min(provider.search_offers(OfferFilters()), key=lambda o: o.dph_total)
    assert cheapest.offer_id == "1001" and len(check_offer(cheapest, OfferFilters())) == 2


@pytest.mark.parametrize("offer_id, reason", [("1001", "VRAM 12 GB < 16 GB"), ("1003", "disk 40 GB < 60 GB")])
@pytest.mark.parametrize("honor_filters", [False, True])
def test_g02_rent_rejects_offer_missing_vram_or_disk(env, clock, offer_id, reason, honor_filters):
    provider = FakeGPUProvider(clock=clock, honor_filters=honor_filters)
    svc = GPUService(env.store, provider, image=IMAGE, clock=clock)
    with pytest.raises(GPUServiceError) as error:
        svc.rent(svc.new_rental_id(), quote(provider, offer_id), OfferFilters())
    assert (reason in str(error.value)) if not honor_filters else "không khớp yêu cầu" in str(error.value)
    assert provider.created() == 0 and env.store.rentals() == []


# ------------------------------------------------------------ G03: báo giá quá hạn hoặc chưa xác nhận


def test_g03_unconfirmed_quote_is_not_created(env, svc, fake_gpu):
    with pytest.raises(GPUServiceError, match="chưa được xác nhận"):
        svc.rent(svc.new_rental_id(), quote(fake_gpu, "1002", confirmed=False), OfferFilters())
    assert fake_gpu.created() == 0 and env.store.rentals() == []


def test_g03_expired_quote_is_not_created(env, svc, fake_gpu, clock):
    stale = quote(fake_gpu, "1002")
    clock.now += timedelta(seconds=301)
    with pytest.raises(GPUServiceError, match="quá hạn"):
        svc.rent(svc.new_rental_id(), stale, OfferFilters())
    assert fake_gpu.created() == 0 and env.store.rentals() == []
    clock.now -= timedelta(seconds=2)                       # còn hạn thì thuê được
    assert svc.rent(svc.new_rental_id(), stale, OfferFilters()).status == "provisioning"


def test_g03_price_changed_since_quote_is_not_created(env, svc, fake_gpu):
    seen = quote(fake_gpu, "1002")
    fake_gpu.reprice("1002", 0.52)
    with pytest.raises(GPUServiceError, match="giá đổi"):
        svc.rent(svc.new_rental_id(), seen, OfferFilters())
    assert fake_gpu.created() == 0


def test_g03_web_requires_confirmation_csrf_and_single_use_nonce(env):
    client = gpu_web(env)
    provider = client.app.state.gpu.provider
    form = rent_form(client, "1002")
    unconfirmed = client.post("/gpu/instances", data={k: v for k, v in form.items() if k != "confirm"})
    assert "err=" in unconfirmed.headers["location"]
    assert client.post("/gpu/instances", data={**form, "csrf": "sai"}).status_code == 403
    expired = client.post("/gpu/instances", data={**form, "quoted_at": "2026-01-01T00:00:00+00:00"})
    assert "err=" in expired.headers["location"]
    assert provider.created() == 0
    first = client.post("/gpu/instances", data=form)
    second = client.post("/gpu/instances", data=form)            # bấm hai lần: cùng rental_id
    assert first.headers["location"].startswith("/gpu/instances/") and "err=" in second.headers["location"]
    assert provider.created() == 1 and len(env.store.rentals()) == 1


def test_each_offer_row_has_its_own_nonce(env):
    """Thuê offer khác trên cùng trang đã render không bị báo nhầm là bấm hai lần."""
    client = gpu_web(env)
    html = client.get("/gpu/offers").text
    nonces = re.findall(r'name="rental_id" value="(r_[0-9a-f]{12})"', html)
    assert len(nonces) == 2 and len(set(nonces)) == 2
    forms = {offer_id: rent_form_from(html, offer_id) for offer_id in ("1002", "1004")}
    for form in forms.values():
        assert client.post("/gpu/instances", data=form).headers["location"].startswith("/gpu/instances/r_")
    assert client.app.state.gpu.provider.created() == 2


# ------------------------------------------------------------ G04: create tới provider nhưng timeout


def test_g04_create_timeout_finds_instance_by_label(env, svc, fake_gpu):
    fake_gpu.fail["create_instance"] = "timeout_after_send"
    rental_id = svc.new_rental_id()
    rental = svc.rent(rental_id, quote(fake_gpu, "1002"), OfferFilters())
    assert rental.status == "provisioning" and rental.provider_instance_id == "fake-1"
    assert rental.label == f"igniterra:{rental_id}"
    assert transitions(env, rental_id) == ["draft", "draft→creating", "creating→create_unknown",
                                           "create_unknown→provisioning"]
    assert fake_gpu.created() == 1 and len(fake_gpu.instances) == 1
    names = [c[0] for c in fake_gpu.calls]
    assert names.index("list_owned_instances") > names.index("create_instance")
    [op] = env.store.gpu_operations(rental_id)
    assert op["action"] == "create" and op["status"] == "confirmed"
    assert {s.step_key: s.status for s in svc.timeline(rental_id)["steps"]}["instance_create"] == "done"


def test_g04_not_found_stays_unknown_and_never_recreates(env, svc, fake_gpu):
    fake_gpu.fail["create_instance"] = "timeout_before_send"
    rental_id = svc.new_rental_id()
    offer_quote = quote(fake_gpu, "1002")
    assert svc.rent(rental_id, offer_quote, OfferFilters()).status == "create_unknown"
    assert svc.reconcile(rental_id).status == "create_unknown"
    events = env.store.gpu_events(rental_id)
    assert [e["error_code"] for e in events if e["transition"] == "reconcile"] == ["not_found", "not_found"]
    with pytest.raises(GPUServiceError, match="đã được dùng"):
        svc.rent(rental_id, offer_quote, OfferFilters())
    assert fake_gpu.created() == 1
    # Provider hiện instance muộn: reconcile gắn vào rental, vẫn chỉ một lượt create.
    fake_gpu.instances["fake-9"] = Instance(provider_instance_id="fake-9", label=f"igniterra:{rental_id}",
                                            actual_status="loading")
    rental = svc.reconcile(rental_id)
    assert rental.status == "provisioning" and rental.provider_instance_id == "fake-9"
    assert fake_gpu.created() == 1
    assert env.store.gpu_operations(rental_id)[0]["status"] == "confirmed"


def test_g04_web_shows_unknown_and_reconciles(env):
    client = gpu_web(env)
    provider = client.app.state.gpu.provider
    provider.fail["create_instance"] = "timeout_before_send"
    location = client.post("/gpu/instances", data=rent_form(client, "1002")).headers["location"]
    rental_id = location.split("/")[-1].split("?")[0]
    detail = client.get(f"/gpu/instances/{rental_id}").text
    assert "create_unknown" in detail and "reconcile" in detail and f"igniterra:{rental_id}" in detail
    provider.instances["fake-7"] = Instance(provider_instance_id="fake-7", label=f"igniterra:{rental_id}",
                                            actual_status="running")
    csrf = re.search(r'name="csrf" value="([^"]+)"', detail).group(1)
    assert client.post(f"/gpu/instances/{rental_id}/reconcile", data={"csrf": "sai"}).status_code == 403
    done = client.post(f"/gpu/instances/{rental_id}/reconcile", data={"csrf": csrf})
    assert "provisioning" in done.headers["location"]
    assert provider.created() == 1
    listing = client.get("/gpu").text
    assert rental_id in listing and "provisioning" in listing


def test_create_rejected_is_final_without_retry(env, svc, fake_gpu):
    fake_gpu.fail["create_instance"] = "reject"
    rental_id = svc.new_rental_id()
    assert svc.rent(rental_id, quote(fake_gpu, "1002"), OfferFilters()).status == "create_failed"
    assert env.store.gpu_operations(rental_id)[0]["status"] == "rejected" and fake_gpu.created() == 1


def test_rental_cas_checks_status_version_and_transition_table(env, svc, fake_gpu):
    rental = svc.rent(svc.new_rental_id(), quote(fake_gpu, "1002"), OfferFilters())
    store = env.store
    assert not store.transition_rental(rental.rental_id, rental.version - 1, "ready", expect="provisioning")
    assert not store.transition_rental(rental.rental_id, rental.version, "ready", expect="create_unknown")
    with pytest.raises(GPUServiceError, match="không hợp lệ"):
        svc._move(rental, "creating")                         # provisioning→creating không có trong TRANSITIONS
    assert store.transition_rental(rental.rental_id, rental.version, "ready", expect="provisioning")


def unknown_rental(svc, fake_gpu) -> str:
    fake_gpu.fail["create_instance"] = "timeout_before_send"
    rental_id = svc.new_rental_id()
    assert svc.rent(rental_id, quote(fake_gpu, "1002"), OfferFilters()).status == "create_unknown"
    return rental_id


def test_g04_duplicate_label_is_not_auto_attached(env, svc, fake_gpu):
    rental_id = unknown_rental(svc, fake_gpu)
    for instance_id in ("fake-8", "fake-3"):
        fake_gpu.instances[instance_id] = Instance(provider_instance_id=instance_id, label=f"igniterra:{rental_id}",
                                                   actual_status="running")
    rental = svc.reconcile(rental_id)
    assert rental.status == "create_unknown" and rental.provider_instance_id is None
    [event] = [e for e in env.store.gpu_events(rental_id) if e["error_code"] == "duplicate_label"]
    assert "fake-3, fake-8" in event["detail"]
    assert fake_gpu.created() == 1 and env.store.gpu_operations(rental_id)[0]["status"] == "unknown"


def test_g04_concurrent_reconcile_attaches_once(env, svc, fake_gpu, monkeypatch):
    """Worker và nút Reconcile cùng đọc create_unknown trước khi bên nào ghi: chỉ một bên chuyển trạng thái."""
    import threading

    rental_id = unknown_rental(svc, fake_gpu)
    fake_gpu.instances["fake-5"] = Instance(provider_instance_id="fake-5", label=f"igniterra:{rental_id}",
                                            actual_status="running")
    barrier = threading.Barrier(2)
    original = fake_gpu.list_owned_instances

    def both_read_then_list():
        barrier.wait(timeout=5)                               # cả hai đã qua bước đọc status
        return original()

    monkeypatch.setattr(fake_gpu, "list_owned_instances", both_read_then_list)
    results = []
    threads = [threading.Thread(target=lambda: results.append(svc.reconcile(rental_id))) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [r.status for r in results] == ["provisioning", "provisioning"]
    rental = svc.rental(rental_id)
    assert rental.provider_instance_id == "fake-5"
    moves = [t for t in transitions(env, rental_id) if t.endswith("→provisioning")]
    assert moves == ["create_unknown→provisioning"]
    assert rental.version == 3                                # draft→creating→create_unknown→provisioning


def test_unpinned_image_is_refused_before_create(env, fake_gpu, clock):
    svc = GPUService(env.store, fake_gpu, image="example/comfyui:latest", clock=clock)
    with pytest.raises(GPUServiceError, match="digest"):
        svc.rent(svc.new_rental_id(), quote(fake_gpu, "1002"), OfferFilters())
    assert fake_gpu.created() == 0 and env.store.rentals() == []


# ------------------------------------------------------------ G09: destroy lỗi hoặc timeout (lát B)


def rented(svc, fake_gpu) -> str:
    rental_id = svc.new_rental_id()
    assert svc.rent(rental_id, quote(fake_gpu, "1002"), OfferFilters()).status == "provisioning"
    return rental_id


def test_g09_destroy_confirmed_only_when_instance_gone(env, svc, fake_gpu):
    rental_id = rented(svc, fake_gpu)
    fake_gpu.fail["destroy_instance"] = "linger"                 # API chỉ trả "destroying"
    rental = svc.destroy(rental_id)
    assert rental.status == "destroying" and rental.destroy_confirmed_at is None
    assert env.store.gpu_operations(rental_id, "destroy")[0]["status"] == "accepted"
    assert svc.reconcile(rental_id).status == "destroying"
    fake_gpu.finish_destroying()
    rental = svc.reconcile(rental_id)
    assert rental.status == "destroyed" and rental.destroy_confirmed_at is not None
    assert env.store.gpu_operations(rental_id, "destroy")[0]["status"] == "confirmed"
    assert fake_gpu.destroyed() == 1


def test_g09_destroy_timeout_after_send_reconciles_to_destroyed(env, svc, fake_gpu):
    rental_id = rented(svc, fake_gpu)
    fake_gpu.fail["destroy_instance"] = "timeout_after_send"
    assert svc.destroy(rental_id).status == "destroyed"
    assert transitions(env, rental_id)[-3:] == ["provisioning→destroying", "destroying→destroy_unknown",
                                                "destroy_unknown→destroyed"]
    assert fake_gpu.destroyed() == 1


def test_g09_destroy_unknown_keeps_record_and_never_destroys_twice(env, svc, fake_gpu):
    rental_id = rented(svc, fake_gpu)
    fake_gpu.fail["destroy_instance"] = "timeout_before_send"
    rental = svc.destroy(rental_id)
    assert rental.status == "destroy_unknown" and rental.provider_instance_id == "fake-1"
    assert env.store.gpu_operations(rental_id, "destroy")[0]["status"] == "unknown"
    assert [e["error_code"] for e in env.store.gpu_events(rental_id)][-2:] == ["destroy_timeout", "still_present"]
    with pytest.raises(GPUServiceError, match="không gửi destroy lần nữa"):
        svc.destroy(rental_id)
    assert svc.reconcile(rental_id).status == "destroy_unknown"
    assert fake_gpu.destroyed() == 1
    del fake_gpu.instances["fake-1"]                                 # hủy tay trên trang provider
    assert svc.reconcile(rental_id).status == "destroyed"
    assert fake_gpu.destroyed() == 1


def test_g09_destroy_rejected_may_be_resent(env, svc, fake_gpu):
    rental_id = rented(svc, fake_gpu)
    fake_gpu.fail["destroy_instance"] = "reject"
    assert svc.destroy(rental_id).status == "destroy_unknown"
    assert env.store.gpu_operations(rental_id, "destroy")[0]["status"] == "rejected"
    assert svc.destroy(rental_id).status == "destroyed"              # lượt trước chắc chắn chưa làm gì
    assert fake_gpu.destroyed() == 2


@pytest.mark.parametrize("op_ids", [("f" * 32, "0" * 32), ("0" * 32, "f" * 32)])
def test_g09_same_second_retry_never_resends_after_unknown(env, svc, fake_gpu, monkeypatch, op_ids):
    """Hai lượt destroy cùng giây (bấm hai lần): op_id ngẫu nhiên không được quyết định lượt nào là "cuối"."""
    import uuid as uuid_module

    from studio import store as store_module

    rental_id = rented(svc, fake_gpu)
    ids = iter(op_ids)
    monkeypatch.setattr(store_module, "utc_now", lambda: "2026-10-26T08:00:00+00:00")
    monkeypatch.setattr("studio.gpu.service.uuid.uuid4", lambda: uuid_module.UUID(next(ids)))
    fake_gpu.fail["destroy_instance"] = "reject"
    assert svc.destroy(rental_id).status == "destroy_unknown"
    fake_gpu.fail["destroy_instance"] = "timeout_before_send"
    assert svc.destroy(rental_id).status == "destroy_unknown"
    with pytest.raises(GPUServiceError, match="không gửi destroy lần nữa"):
        svc.destroy(rental_id)
    assert fake_gpu.destroyed() == 2
    assert [op["status"] for op in env.store.gpu_operations(rental_id, "destroy")] == ["rejected", "unknown"]
    del fake_gpu.instances["fake-1"]
    assert svc.reconcile(rental_id).status == "destroyed"
    assert [op["status"] for op in env.store.gpu_operations(rental_id, "destroy")] == ["rejected", "confirmed"]


def test_destroy_refused_without_instance_or_after_destroyed(env, svc, fake_gpu):
    rental_id = unknown_rental(svc, fake_gpu)
    with pytest.raises(GPUServiceError, match="create_unknown"):
        svc.destroy(rental_id)
    done = rented(svc, fake_gpu)
    svc.destroy(done)
    with pytest.raises(GPUServiceError, match="destroyed"):
        svc.destroy(done)
    assert fake_gpu.destroyed() == 1


def test_stop_then_destroy(env, svc, fake_gpu):
    rental_id = rented(svc, fake_gpu)
    assert svc.stop(rental_id).status == "stopped"
    assert fake_gpu.instances["fake-1"].actual_status == "stopped"
    with pytest.raises(GPUServiceError, match="stopped"):
        svc.stop(rental_id)
    assert svc.destroy(rental_id).status == "destroyed"


def test_stop_unknown_is_reconciled_not_resent(env, svc, fake_gpu):
    rental_id = rented(svc, fake_gpu)
    fake_gpu.fail["stop_instance"] = "timeout_before_send"
    assert svc.stop(rental_id).status == "provisioning"
    assert env.store.gpu_operations(rental_id, "stop")[0]["status"] == "unknown"
    with pytest.raises(GPUServiceError, match="chưa rõ"):
        svc.stop(rental_id)
    fake_gpu.instances["fake-1"] = fake_gpu.instances["fake-1"].model_copy(update={"actual_status": "exited"})
    assert svc.reconcile(rental_id).status == "stopped"
    assert env.store.gpu_operations(rental_id, "stop")[0]["status"] == "confirmed"
    assert sum(c[0] == "stop_instance" for c in fake_gpu.calls) == 1


def test_stop_refused_when_provider_cannot_stop(env, monkeypatch):
    monkeypatch.setitem(adapters.REGISTRY, "other", f"{__name__}:OtherProvider")
    client = gpu_web(env, "other")
    client.post("/gpu/instances", data=rent_form(client, "x1"))
    svc = client.app.state.gpu
    with pytest.raises(GPUServiceError, match="không hỗ trợ stop"):
        svc.stop(svc.rentals()[0].rental_id)
    assert "/stop" not in client.get(f"/gpu/instances/{svc.rentals()[0].rental_id}").text


def test_g09_web_stop_destroy_need_csrf_and_confirmation(env):
    client = gpu_web(env)
    provider = client.app.state.gpu.provider
    location = client.post("/gpu/instances", data=rent_form(client, "1002")).headers["location"]
    rental_id = location.split("/")[-1].split("?")[0]
    url = f"/gpu/instances/{rental_id}"
    detail = client.get(url).text
    assert f'action="{url}/stop"' in detail and f'action="{url}/destroy"' in detail
    csrf = re.search(r'name="csrf" value="([^"]+)"', detail).group(1)
    assert client.post(f"{url}/destroy", data={"csrf": "sai", "confirm": "on"}).status_code == 403
    assert "err=" in client.post(f"{url}/destroy", data={"csrf": csrf}).headers["location"]
    assert "err=" in client.post(f"{url}/stop", data={"csrf": csrf}).headers["location"]
    assert client.post(f"{url}/explode", data={"csrf": csrf, "confirm": "on"}).status_code == 404
    assert provider.destroyed() == 0 and not any(c[0] == "stop_instance" for c in provider.calls)
    assert "stopped" in client.post(f"{url}/stop", data={"csrf": csrf, "confirm": "on"}).headers["location"]
    provider.fail["destroy_instance"] = "timeout_before_send"
    assert "destroy_unknown" in client.post(f"{url}/destroy", data={"csrf": csrf, "confirm": "on"}).headers["location"]
    page = client.get(url).text
    assert "không gửi destroy lần nữa" in page and f'action="{url}/destroy"' not in page
    assert f'action="{url}/reconcile"' in page
    again = client.post(f"{url}/destroy", data={"csrf": csrf, "confirm": "on"})
    assert "err=" in again.headers["location"] and provider.destroyed() == 1
    del provider.instances["fake-1"]
    assert "destroyed" in client.post(f"{url}/reconcile", data={"csrf": csrf}).headers["location"]


# ------------------------------------------------------------ G10: khởi động lại, orphan theo label


def test_g10_orphan_with_app_label_is_reported_not_deleted(env, svc, fake_gpu):
    rental_id = rented(svc, fake_gpu)
    fake_gpu.instances["fake-90"] = Instance(provider_instance_id="fake-90", label="igniterra:r_0123456789ab",
                                             actual_status="running", dph_total=0.4)
    fake_gpu.instances["fake-91"] = Instance(provider_instance_id="fake-91", label="someone-else",
                                             actual_status="running")
    orphans = svc.find_orphans()
    assert [o.provider_instance_id for o in orphans] == ["fake-90"]
    assert "không có rental" in orphans[0].reason
    assert {"fake-1", "fake-90", "fake-91"} <= set(fake_gpu.instances)
    assert fake_gpu.destroyed() == 0 and not any(c[0] == "stop_instance" for c in fake_gpu.calls)
    assert svc.rental(rental_id).status == "provisioning"


def test_g10_instance_left_after_rental_destroyed_is_orphan(env, svc, fake_gpu):
    rental_id = rented(svc, fake_gpu)
    svc.destroy(rental_id)
    fake_gpu.instances["fake-5"] = Instance(provider_instance_id="fake-5", label=f"igniterra:{rental_id}",
                                            actual_status="running")
    [orphan] = svc.find_orphans()
    assert orphan.provider_instance_id == "fake-5" and "destroyed" in orphan.reason
    assert "fake-5" in fake_gpu.instances


def test_g10_restart_recovers_unknown_states(env, svc, fake_gpu, clock):
    destroying = rented(svc, fake_gpu)
    fake_gpu.fail["destroy_instance"] = "timeout_before_send"
    assert svc.destroy(destroying).status == "destroy_unknown"
    creating = unknown_rental(svc, fake_gpu)
    # Trong lúc worker tắt: provider hủy xong instance cũ, instance của create mất response hiện ra.
    del fake_gpu.instances["fake-1"]
    fake_gpu.instances["fake-20"] = Instance(provider_instance_id="fake-20", label=f"igniterra:{creating}",
                                             actual_status="loading")
    restarted = GPUService(env.store, fake_gpu, image=IMAGE, clock=clock)   # process mới, cùng SQLite
    assert restarted.recover() == []
    assert restarted.rental(destroying).status == "destroyed"
    assert restarted.rental(creating).status == "provisioning"
    assert fake_gpu.created() == 2 and fake_gpu.destroyed() == 1


class SeededFake(FakeGPUProvider):
    """Provider có sẵn một instance mang label của app nhưng SQLite không có rental (worker chết sau create)."""

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.instances["fake-77"] = Instance(provider_instance_id="fake-77", label="igniterra:r_feedfacecafe",
                                             actual_status="running", dph_total=0.29)


class DownFake(FakeGPUProvider):
    def list_owned_instances(self) -> list[Instance]:
        raise GPUError("provider down")


def test_g10_app_startup_surfaces_orphan_on_gpu_page(env, monkeypatch, caplog):
    monkeypatch.setitem(adapters.REGISTRY, "seeded", f"{__name__}:SeededFake")
    with caplog.at_level("WARNING", logger="studio.gpu"):
        client = gpu_web(env, "seeded")
    provider = client.app.state.gpu.provider
    assert ("list_owned_instances",) in provider.calls
    assert any("fake-77" in r.getMessage() and "không tự xóa" in r.getMessage() for r in caplog.records)
    page = client.get("/gpu").text
    assert "fake-77" in page and "igniterra:r_feedfacecafe" in page and "không tự xóa" in page
    assert "fake-77" in provider.instances and provider.destroyed() == 0


def test_g10_orphan_check_failure_does_not_block_startup(env, monkeypatch):
    monkeypatch.setitem(adapters.REGISTRY, "down", f"{__name__}:DownFake")
    client = gpu_web(env, "down")
    assert "Không kiểm được: provider down" in client.get("/gpu").text


# ------------------------------------------------------------ chi phí ước tính


def test_cost_estimate_accrues_by_state(env, svc, fake_gpu, clock):
    rental_id = rented(svc, fake_gpu)                               # 0,35 USD/giờ, disk 60 GB × 0,10/GB-tháng
    disk_per_h = 0.10 * 60 / (30 * 24)
    cost = svc.cost(rental_id)
    assert cost["compute_per_h"] == 0.35 and cost["disk_per_h"] == pytest.approx(disk_per_h)
    assert cost["label"] == "ước tính, không phải hóa đơn" and cost["accrued_est"] == 0
    clock.now += timedelta(hours=2)
    svc.stop(rental_id)
    assert svc.cost(rental_id)["compute_per_h"] == 0                 # stopped: chỉ còn disk
    clock.now += timedelta(hours=3)
    svc.destroy(rental_id)
    clock.now += timedelta(hours=5)                                  # đã destroyed: không cộng thêm
    expected = 2 * (0.35 + disk_per_h) + 3 * disk_per_h
    assert svc.cost(rental_id)["accrued_est"] == pytest.approx(expected, abs=1e-5)
    samples = env.store.cost_samples(rental_id)
    assert {"rental_id", "ts", "compute_per_h", "disk_per_h", "accrued_est"} <= set(samples[0])
    assert samples[-1]["compute_per_h"] == samples[-1]["disk_per_h"] == 0


def test_rental_page_shows_estimate_label(env):
    client = gpu_web(env)
    location = client.post("/gpu/instances", data=rent_form(client, "1002")).headers["location"]
    page = client.get(location.split("?")[0]).text
    assert "Chi phí ước tính" in page and "ước tính, không phải hóa đơn" in page and "60 GB" in page


def test_store_adds_disk_column_to_old_gpu_rentals(tmp_path):
    import sqlite3

    from studio.store import Store

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE gpu_rentals (rental_id TEXT PRIMARY KEY, provider_key TEXT NOT NULL, "
                   "provider_instance_id TEXT, label TEXT NOT NULL UNIQUE, offer_json TEXT NOT NULL, "
                   "status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, idle_deadline TEXT, "
                   "destroy_confirmed_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    Store(path)
    with sqlite3.connect(path) as db:
        assert "disk_gb" in {row[1] for row in db.execute("PRAGMA table_info(gpu_rentals)")}
