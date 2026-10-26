"""Lát E: guardrail qua service, SQLite và worker; provider giả, 0 USD."""

from datetime import UTC, datetime, timedelta, timezone
import logging
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import approve_gpu_manifest
from studio import pipeline
from studio.comfyui.contracts import RUN_UNRESOLVED
from studio.comfyui.service import ComfyService
from studio.gpu import service
from studio.gpu.adapters.base import GPUError
from studio.gpu.adapters.fake import FakeGPUProvider
from studio.gpu.contracts import Instance, OfferFilters, Quote
from studio.gpu.limits import Limits
from studio.web import create_app

IMAGE = "example/comfyui@sha256:" + "0" * 64


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 10, 26, 8, tzinfo=UTC))
    monkeypatch.setattr("studio.store.utc_now", lambda: clock.now.isoformat(timespec="seconds"))
    return clock


@pytest.fixture
def gpu(env, clock):
    approve_gpu_manifest(env.store)
    provider = FakeGPUProvider(clock=lambda: clock.now)
    return service.GPUService(env.store, provider, image=IMAGE, clock=lambda: clock.now,
                              probe_factory=lambda *_: SimpleNamespace(comfy=object()))


def rent(gpu, status="ready"):
    offer = gpu.search_offers(OfferFilters())[0]
    rental = gpu.rent(gpu.new_rental_id(), Quote(offer_id=offer.offer_id, dph_total=offer.dph_total,
                                               quoted_at=offer.quoted_at, confirmed=True), OfferFilters())
    if status != "provisioning":
        assert gpu._move(rental, "ready")
        if status == "busy":
            assert gpu.claim_for_run(rental.rental_id, "run-test")
        elif status == "stopped":
            gpu.stop(rental.rental_id)
    return rental.rental_id


def unresolved(gpu, rental_id, status="output_failed"):
    workflow = ComfyService(gpu.store, gpu).import_workflow("smoke", b'{"1":{"class_type":"EmptyImage","inputs":{}}}')
    run_id = "run-unverified"
    gpu.store.add_comfy_run(run_id, workflow.workflow_id, workflow.revision, workflow.api_sha256, "0" * 64, rental_id)
    with gpu.store.connect() as db:
        db.execute("UPDATE comfy_runs SET status=? WHERE run_id=?", (status, run_id))
    return run_id


def test_idle_ttl_exact_boundary_and_no_duplicate_destroy(gpu, clock):
    rid = rent(gpu)
    clock.now += timedelta(minutes=30, seconds=-1)
    assert gpu.enforce_limits() == []
    clock.now += timedelta(seconds=1)
    result, = gpu.enforce_limits()
    assert result.outcome == "destroy_sent" and result.status == "destroyed"
    assert [b.kind for b in result.breaches] == ["idle_ttl"]
    assert gpu.enforce_limits() == [] and gpu.provider.destroyed() == 1
    assert any("guardrail [idle_ttl]" in (e["detail"] or "") for e in gpu.store.gpu_events(rid))


def test_runtime_activity_and_release_reset_idle_deadline(gpu, clock):
    rid = rent(gpu)
    clock.now += timedelta(minutes=29)
    gpu.comfy_runtime(rid)
    clock.now += timedelta(minutes=2)
    assert gpu.enforce_limits() == []
    assert gpu.claim_for_run(rid, "run-test")
    clock.now += timedelta(minutes=31)
    assert gpu.enforce_limits() == []  # busy không tính idle
    assert gpu.release_after_run(rid, "run-test")
    assert gpu.rental(rid).idle_deadline == clock.now + timedelta(minutes=30)
    assert gpu.enforce_limits() == []


@pytest.mark.parametrize("activity", ["touch", "claim"])
def test_activity_between_limit_check_and_destroy_cas_wins(gpu, clock, monkeypatch, activity):
    rid = rent(gpu)
    clock.now += timedelta(minutes=31)
    original = gpu.store.begin_destroy

    def race(*args, **kwargs):
        if activity == "touch":
            gpu.comfy_runtime(rid)
        else:
            unresolved(gpu, rid, "checking")
            assert gpu.claim_for_run(rid, "run-unverified")
        return original(*args, **kwargs)

    monkeypatch.setattr(gpu.store, "begin_destroy", race)
    result, = gpu.enforce_limits()
    assert result.outcome == "cleared" and gpu.provider.destroyed() == 0
    assert gpu.rental(rid).status == ("ready" if activity == "touch" else "busy")


def test_idle_deadline_uses_instants_with_different_timezone_offsets(gpu, clock):
    rid = rent(gpu)
    clock.now += timedelta(minutes=31)
    deadline = clock.now + timedelta(minutes=1)
    assert gpu.store.touch_rental_idle(rid, deadline.astimezone(timezone(timedelta(hours=-7))).isoformat())
    assert gpu.enforce_limits() == []
    clock.now += timedelta(minutes=1)
    assert gpu.enforce_limits()[0].status == "destroyed"


def test_legacy_rental_without_idle_deadline_is_reclaimed(gpu, clock):
    rid = rent(gpu)
    with gpu.store.connect() as db:
        db.execute("UPDATE gpu_rentals SET idle_deadline=NULL WHERE rental_id=?", (rid,))
    clock.now += timedelta(minutes=30)
    assert gpu.enforce_limits()[0].status == "destroyed"


@pytest.mark.parametrize("status", ["ready", "busy", "provisioning"])
@pytest.mark.parametrize("limit", ["runtime", "budget", "hourly"])
def test_non_idle_limits_reclaim_billable_rentals(gpu, clock, status, limit):
    rid = rent(gpu, status)
    if limit == "runtime":
        gpu.limits = Limits(idle_ttl_minutes=None, max_runtime_minutes=2)
        clock.now += timedelta(minutes=2)
        kind = "max_runtime"
    elif limit == "budget":
        gpu.limits = Limits(idle_ttl_minutes=None, max_total_usd=0.001)
        clock.now += timedelta(minutes=1)
        kind = "budget"
    else:
        gpu.limits = Limits(max_hourly_usd=0.1)
        kind = "hourly_rate"
    result, = gpu.enforce_limits()
    assert [b.kind for b in result.breaches] == [kind]
    assert gpu.rental(rid).status == "destroyed" and gpu.provider.destroyed() == 1


@pytest.mark.parametrize("run_status", RUN_UNRESOLVED)
def test_g11_output_blocks_every_automatic_destroy(gpu, clock, caplog, run_status):
    rid = rent(gpu)
    run_id = unresolved(gpu, rid, run_status)
    clock.now += timedelta(minutes=241)
    with caplog.at_level(logging.ERROR):
        for _ in range(2):
            result, = gpu.enforce_limits()
            assert result.outcome == "blocked_unverified_output" and result.runs == [run_id]
    assert gpu.provider.destroyed() == 0 and gpu.store.comfy_run(run_id)["status"] == run_status
    assert "máy vẫn tính phí" in caplog.text
    assert len([e for e in gpu.store.gpu_events(rid) if e["transition"] == "limit_blocked"]) == 1
    gpu.destroy(rid, accept_unverified_output=True)
    assert gpu.rental(rid).status == "destroyed" and gpu.store.comfy_run(run_id)["status"] == "abandoned"


@pytest.mark.parametrize("failure", ["timeout_after_send", "timeout_before_send", "reject", "linger"])
def test_g09_tick_reconciles_destroy_without_resending(gpu, clock, failure):
    rid = rent(gpu)
    clock.now += timedelta(minutes=31)
    gpu.provider.fail["destroy_instance"] = failure
    gpu.tick()
    gpu.tick()
    if failure in ("reject", "timeout_before_send"):
        assert gpu.rental(rid).status == "destroy_unknown"
        assert gpu.cost(rid)["compute_per_h"] > 0
        gpu.provider.instances.clear()  # Người vận hành đã hủy bên provider
    elif failure == "linger":
        assert gpu.rental(rid).status == "destroying"
        gpu.provider.finish_destroying()
    gpu.tick()
    assert gpu.rental(rid).status == "destroyed" and gpu.provider.destroyed() == 1
    assert gpu.cost(rid)["compute_per_h"] == 0


def test_g10_restart_reclaims_only_owned_rentals_and_reports_orphans(gpu, clock):
    rid = rent(gpu)
    gpu.provider.instances["foreign"] = Instance(provider_instance_id="foreign", label="someone-else", actual_status="running")
    gpu.provider.instances["orphan"] = Instance(provider_instance_id="orphan", label="igniterra:r_deadbeef0000", actual_status="running")
    clock.now += timedelta(minutes=31)
    restarted = service.GPUService(gpu.store, gpu.provider, clock=lambda: clock.now)
    assert [o.provider_instance_id for o in restarted.recover()] == ["orphan"]
    restarted.tick()
    assert restarted.rental(rid).status == "destroyed"
    assert set(gpu.provider.instances) == {"foreign", "orphan"}


@pytest.mark.parametrize("mismatch", ["provider", "rental_label", "instance_label"])
def test_guard_never_destroys_an_instance_with_wrong_ownership(gpu, clock, mismatch):
    rid = rent(gpu)
    if mismatch == "instance_label":
        iid = gpu.rental(rid).provider_instance_id
        gpu.provider.instances[iid] = gpu.provider.instances[iid].model_copy(update={"label": "foreign"})
    else:
        column, value = ("provider_key", "other") if mismatch == "provider" else ("label", "foreign")
        with gpu.store.connect() as db:
            db.execute(f"UPDATE gpu_rentals SET {column}=? WHERE rental_id=?", (value, rid))
    clock.now += timedelta(minutes=31)
    gpu.enforce_limits()
    assert gpu.provider.destroyed() == 0 and gpu.rental(rid).status == "ready"


def test_stopped_rental_keeps_disk_and_is_not_auto_destroyed(gpu, clock):
    rid = rent(gpu, "stopped")
    clock.now += timedelta(days=1)
    assert gpu.enforce_limits() == [] and gpu.provider.destroyed() == 0
    assert gpu.cost(rid)["disk_per_h"] > 0 and gpu.cost(rid)["compute_per_h"] == 0


def test_hourly_cap_includes_disk_and_refuses_before_create(gpu):
    gpu.limits = Limits(max_hourly_usd=0.355)  # compute .35 + disk .00833 > .355
    with pytest.raises(service.GPUServiceError, match="max_hourly_usd"):
        rent(gpu)
    assert gpu.provider.created() == 0 and gpu.store.rentals() == []
    assert Limits().budget_usd() == 8
    assert Limits(max_runtime_minutes=None).budget_usd() is None
    assert Limits(max_total_usd=2).budget_usd() == 2


@pytest.mark.parametrize("values", [{"max_total_usd": 0}, {"idle_ttl_minutes": -1}, {"max_hourly_usd": float("inf")},
                                   {"max_runtime_minutes": float("nan")}, {"idle_ttl_minute": 5}])
def test_invalid_limits_fail_before_worker_starts(env, values):
    with pytest.raises(service.GPUServiceError, match="gpu.limits"):
        service.from_config(env.store, {"gpu": {"provider": "fake", "limits": values}})


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), "invalid"])
def test_invalid_guard_interval_is_rejected(env, interval):
    with pytest.raises(service.GPUServiceError, match="guard_interval_s"):
        service.start_guard(env.store, {"gpu": {"provider": "fake", "guard_interval_s": interval}})


def test_background_guard_recovers_survives_error_and_stops(gpu, monkeypatch):
    stop, completed = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(service, "from_config", lambda *_: gpu)

    def recover():
        calls.append("recover")
        raise GPUError("startup unavailable")

    def tick():
        calls.append("tick")
        if calls.count("tick") == 1:
            raise GPUError("temporary failure")
        stop.set()
        completed.set()

    monkeypatch.setattr(gpu, "recover", recover)
    monkeypatch.setattr(gpu, "tick", tick)
    thread = service.start_guard(gpu.store, {"gpu": {"guard_interval_s": 0.01}}, stop=stop)
    try:
        assert completed.wait(3)
    finally:
        stop.set()
        thread.join(timeout=3)
    assert calls == ["recover", "tick", "tick"] and not thread.is_alive()


def test_worker_stops_guard_on_exit(env, monkeypatch):
    started = []
    monkeypatch.setattr(service, "start_guard", lambda *args, **kwargs: started.append(kwargs["stop"]))

    def interrupt(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        pipeline.worker(env.store, env.config)
    assert started[0].is_set()
    started.clear()
    pipeline.worker(env.store, env.config, once=True)
    assert started == []


def test_web_lists_limits_breaches_and_output_action(env, gpu, clock, monkeypatch):
    rid = rent(gpu)
    run_id = unresolved(gpu, rid)
    clock.now += timedelta(minutes=31)
    monkeypatch.setattr(service, "from_config", lambda *_: gpu)
    client = TestClient(create_app(env.config))
    deadline = gpu.rental(rid).idle_deadline
    for path in ("/gpu", f"/gpu/instances/{rid}"):
        response = client.get(path)
        assert response.status_code == 200
        assert "Rental vượt giới hạn" in response.text and "Không tự destroy" in response.text
        assert "worker" in response.text and "Giới hạn GPU" in response.text
    detail = client.get(f"/gpu/instances/{rid}").text
    assert f'/comfyui/runs/{run_id}' in detail and "Hạn idle" in detail
    assert gpu.rental(rid).idle_deadline == deadline and gpu.provider.destroyed() == 0


def test_cli_enforce_limits_uses_same_guard(env, gpu, clock, monkeypatch, capsys):
    from studio.cli import main

    rid = rent(gpu)
    clock.now += timedelta(minutes=31)
    monkeypatch.setattr(service, "from_config", lambda *_: gpu)
    monkeypatch.setattr("studio.cli.studio_config.load", lambda: env.config)
    main(["gpu", "enforce-limits"])
    assert rid in capsys.readouterr().out and gpu.rental(rid).status == "destroyed"


def test_guard_error_redacts_tokens_and_password_created_after_startup(gpu, clock, monkeypatch, caplog):
    # Worker tạo service trước khi web tạo rental, nên cache bí mật ban đầu còn rỗng.
    worker = service.GPUService(gpu.store, gpu.provider, clock=lambda: clock.now)
    rid = rent(gpu)
    password = gpu._password(rid)
    key, hf = "vast-test-secret", "hf-test-secret"
    monkeypatch.setenv("VAST_API_KEY", key)
    monkeypatch.setenv("HF_TOKEN", hf)
    clock.now += timedelta(minutes=31)

    def fail(*_):
        raise GPUError(f"Bearer {password} {key} {hf}")

    monkeypatch.setattr(gpu.provider, "get_instance", fail)
    with caplog.at_level(logging.WARNING):
        worker.enforce_limits()
    events = str(gpu.store.gpu_events(rid))
    for secret in (password, key, hf):
        assert secret not in caplog.text and secret not in events
    assert "<WEB_PASSWORD>" in events and "<HF_TOKEN>" in caplog.text


def test_one_provider_failure_does_not_skip_other_rentals(gpu, clock, monkeypatch):
    first, second = rent(gpu), rent(gpu)
    broken = gpu.rental(first).provider_instance_id
    original = gpu.provider.get_instance

    def fail_one(iid):
        if iid == broken:
            raise GPUError("temporary failure")
        return original(iid)

    monkeypatch.setattr(gpu.provider, "get_instance", fail_one)
    clock.now += timedelta(minutes=31)
    gpu.enforce_limits()
    assert gpu.rental(first).status == "ready" and gpu.rental(second).status == "destroyed"


def test_tick_continues_reconciling_late_create_without_creating_again(gpu, clock):
    gpu.provider.fail["create_instance"] = "timeout_before_send"
    rid = rent(gpu, "provisioning")
    assert gpu.rental(rid).status == "create_unknown"
    spec_label = gpu.rental(rid).label
    gpu.provider.instances["late"] = Instance(provider_instance_id="late", label=spec_label, actual_status="loading")
    clock.now += timedelta(minutes=241)
    gpu.tick()
    assert gpu.rental(rid).status == "destroyed"
    assert gpu.provider.created() == 1 and gpu.provider.destroyed() == 1
