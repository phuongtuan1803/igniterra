"""Gate 3: trang review, /files, T03, T07, Series Bible (với bản giả)."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from conftest import last_run_id, steps_run
from studio import pipeline
from studio.providers import fake
from studio.series import SeriesActions, run_series_job
from studio.web import create_app


@pytest.fixture
def web(env):
    return TestClient(create_app(env.config), follow_redirects=False)


def to_final(env, task_id):
    assert env.run(task_id) == "waiting"
    env.actions.decide(task_id, "approve")
    assert env.run(task_id) == "waiting" and env.task(task_id)["wait_reason"] == "gate:final"


def test_pages_render(env, web):
    task_id = env.new_task()
    to_final(env, task_id)
    for url in ["/", f"/tasks/{task_id}", f"/tasks/{task_id}?scene=s03", f"/tasks/{task_id}/detail",
                f"/tasks/{task_id}/status", "/series", "/settings"]:
        response = web.get(url)
        assert response.status_code == 200, url
    board = web.get(f"/tasks/{task_id}").text
    assert "final_9x16.mp4" in board and "C09" in board and "img_s01_a1" in board
    assert web.get(f"/files/tasks/{task_id}/renders/r_001/final_9x16.mp4").status_code == 200


def test_files_stay_inside_data(env, web, tmp_path):
    (tmp_path / "secret.txt").write_text("x")
    data = tmp_path / "data"
    os.symlink(tmp_path / "secret.txt", data / "link.txt")
    assert web.get("/files/../secret.txt").status_code == 404
    assert web.get("/files/%2e%2e/secret.txt").status_code == 404
    assert web.get("/files/link.txt").status_code == 404
    assert web.get("/files/studio.db").status_code == 200


def test_create_task_from_brief_file(env, web):
    response = web.post("/tasks", data={"brief_file": "brief-03.json"})
    assert response.status_code == 303 and response.headers["location"].startswith("/tasks/t_0001")
    assert env.task("t_0001")["brief"]["id"] == "brief-03"
    bad = web.post("/tasks", data={"brief_file": "../pyproject.toml"})
    assert "err=" in bad.headers["location"]


def test_web_refuses_edits_while_running(env, web):
    task_id = env.new_task()
    to_final(env, task_id)
    assert env.store.set_status(task_id, "running")
    response = web.post(f"/tasks/{task_id}/scenes/s01/select", data={"candidate": "img_s01_a1"})
    assert "err=" in response.headers["location"] and "%C4%91ang+ch%E1%BA%A1y" in response.headers["location"]
    assert "disabled" in web.get(f"/tasks/{task_id}").text


def test_review_and_approve_via_web(env, web):
    task_id = env.new_task()
    to_final(env, task_id)
    form = {f"C0{i}_result": "pass" for i in range(1, 10)} | {"active_minutes": "7.5", "reviewer": "Tuan", "C03_ts": "00:02"}
    assert "msg=" in web.post(f"/tasks/{task_id}/review", data=form).headers["location"]
    review = env.store.reviews(task_id)[-1]
    assert review["accepted"] == 1 and review["active_minutes"] == 7.5 and review["checklist"]["C03"]["timestamp"] == "00:02"
    web.post(f"/tasks/{task_id}/gate", data={"decision": "approve"})
    assert env.task(task_id)["status"] == "completed"


def test_edit_scene_via_web_creates_revision(env, web):
    task_id = env.new_task()
    to_final(env, task_id)
    web.post(f"/tasks/{task_id}/scenes/s02/edit", data={"visual": "Robot scene s02, hands out of frame", "note": "test"})
    assert env.task(task_id)["plan_rev"] == 2 and env.task(task_id)["status"] == "pending"
    before = last_run_id(env.store, task_id)
    assert env.run(task_id) == "waiting" and env.task(task_id)["wait_reason"] == "gate:keyframes"
    assert {(s, sc) for s, sc, st in steps_run(env.store, task_id, before) if st == "done"} == {("keyframes", "s02")}


def test_upload_image_reruns_only_animate_and_render(env, web, tmp_path):
    """T03: ứng viên provider user, được chọn; chỉ animate scene đó và render chạy lại."""
    task_id = env.new_task()
    to_final(env, task_id)
    image = tmp_path / "mine.png"
    fake._ffmpeg(["-f", "lavfi", "-i", "color=c=red:s=1080x1920", "-frames:v", "1", str(image)])
    with image.open("rb") as handle:
        response = web.post(f"/tasks/{task_id}/scenes/s04/upload", data={"kind": "image", "source": "ảnh tự vẽ", "rights": "owned"},
                            files={"file": ("mine.png", handle, "image/png")})
    assert "msg=" in response.headers["location"]
    selected = env.td(task_id).selected("s04", "image")
    assert selected.id == "up_s04_1" and selected.provider == "user" and selected.rights == "owned" and not selected.ai_generated
    env.store.set_status(task_id, "waiting", "gate:keyframes")
    env.actions.decide(task_id, "approve")
    before = last_run_id(env.store, task_id)
    images = len(fake.CALLS["image"])
    assert env.run(task_id) == "waiting"
    assert len(fake.CALLS["image"]) == images
    assert {(s, sc) for s, sc, st in steps_run(env.store, task_id, before) if st == "done"} == {("animate", "s04"), ("render", "_")}


def test_upload_clip_reruns_only_render(env, web, tmp_path):
    task_id = env.new_task()
    to_final(env, task_id)
    clip = tmp_path / "mine.mp4"
    fake._ffmpeg(["-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=12", "-pix_fmt", "yuv420p", str(clip)])
    env.actions.upload(task_id, "s02", "clip", clip, "quay tay", "owned", False)
    before = last_run_id(env.store, task_id)
    assert env.run(task_id) == "waiting" and env.task(task_id)["wait_reason"] == "gate:final"
    assert {(s, sc) for s, sc, st in steps_run(env.store, task_id, before) if st == "done"} == {("render", "_")}


def test_switch_tts_adapter_by_settings_only(env, web):
    """T07: đổi adapter TTS qua trang Settings; không sửa code core, chỉ giọng và phần sau chạy lại."""
    task_id = env.new_task()
    to_final(env, task_id)
    response = web.post("/settings", data={"adapter_llm": "fake", "adapter_image": "fake", "adapter_tts": "fake_alt",
                                           "adapter_subtitles": "fake", "adapter_video": "none", "gate_keyframes": "on"})
    assert "msg=" in response.headers["location"]
    from studio import config as studio_config

    env.config = studio_config.load(root=env.config["root"], path=env.toml)
    assert env.config["adapters"]["tts"] == "fake_alt"
    env.store.set_status(task_id, "pending")
    before = last_run_id(env.store, task_id)
    assert env.run(task_id) == "waiting"
    done = {s for s, sc, st in steps_run(env.store, task_id, before) if st == "done"}
    assert done == {"voice", "subtitles", "animate", "render"}
    assert env.td(task_id).selected("s01", "voice").provider.startswith("fake_alt:")
    bad = web.post("/settings", data={"adapter_tts": "khong-co"})
    assert "err=" in bad.headers["location"]


def test_series_refs_sheet_and_keyframes(env, web):
    task_id = env.new_task()
    to_final(env, task_id)
    web.post("/series", data={"name": "robot-garden", "from_task": task_id})
    sa = SeriesActions(env.store, env.config)
    bible = sa.dir("robot-garden").bible()
    assert "robot" in bible.characters
    for view in ("front", "side", "three_quarter"):
        web.post("/series/robot-garden/characters/robot/ref", data={"view": view})
    for job in env.store.series_jobs("robot-garden"):
        claimed = env.store.claim_series_job()
        assert run_series_job(env.store, env.config, claimed) == "done"
    candidates = [env.store.series_jobs("robot-garden")[i]["output_path"] for i in range(3)]
    statuses = [e.status for e in __import__("studio.costs", fromlist=["x"]).read(sa.dir("robot-garden").costs_path)]
    assert statuses == ["pending", "ok"] * 3
    response = web.post("/series/robot-garden/characters/robot/approve", data={"candidate": candidates})
    assert "msg=" in response.headers["location"], response.headers["location"]
    character = sa.dir("robot-garden").bible().characters["robot"]
    assert len(character.refs) == 3 and character.sheet.startswith("refs/robot_sheet_v")
    from studio.media import image_size

    width, height = image_size(sa.dir("robot-garden").root / character.sheet)
    assert height == 1536 and width > 1536
    assert web.get("/series/robot-garden").status_code == 200
    # Sau khi có ảnh duyệt, góc nhìn mới dùng ảnh duyệt đầu tiên làm tham chiếu.
    anchors = []
    original_generate = fake.FakeImage.generate
    fake.FakeImage.generate = lambda self, prompt, refs, size, out: (anchors.append(refs), original_generate(self, prompt, refs, size, out))[1]
    try:
        sa.request_ref("robot-garden", "robot", "side")
        assert run_series_job(env.store, env.config, env.store.claim_series_job()) == "done"
    finally:
        fake.FakeImage.generate = original_generate
    assert anchors[0][0].name == character.refs[0].split("/")[-1]
    assert "Same character as the reference image" in fake.CALLS["image"][-1]
    # Task mới thuộc series: keyframes gửi character sheet làm ảnh tham chiếu.
    sent = []
    original = fake.FakeImage.generate
    fake.FakeImage.generate = lambda self, prompt, refs, size, out: (sent.append(refs), original(self, prompt, refs, size, out))[1]
    try:
        series_task = env.actions.create_task(env.brief(), "robot-garden", {})
        assert env.run(series_task) == "waiting"
    finally:
        fake.FakeImage.generate = original
    assert sent and all(refs and refs[0].name == character.sheet.split("/")[-1] for refs in sent)
    assert "small rusty maintenance robot" in fake.CALLS["image"][-1]
