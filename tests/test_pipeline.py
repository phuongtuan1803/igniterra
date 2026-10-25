"""Test core với bản giả (05 · Kiểm thử; 08 · T01 phần kỹ thuật, T02, T04, T05, T06, T11, T12)."""

from __future__ import annotations

import pytest

from conftest import fake_plan_data, last_run_id, read_manifest, steps_run
from studio import costs, media
from studio.gates import ActionError
from studio.providers import fake
from studio.providers.base import Retryable, Unconfirmed
from studio.steps import subtitles


def run_to_final(env, task_id: str) -> None:
    assert env.run(task_id) == "waiting"
    assert env.task(task_id)["wait_reason"] == "gate:keyframes"
    env.actions.decide(task_id, "approve")
    assert env.run(task_id) == "waiting"
    assert env.task(task_id)["wait_reason"] == "gate:final", env.task(task_id)["wait_detail"]


def test_end_to_end_with_fakes(env):
    task_id = env.new_task()
    run_to_final(env, task_id)
    td = env.td(task_id)
    assert td.render_revisions() == ["r_001"]
    manifest = read_manifest(td, "r_001")
    final = td.root / "renders" / "r_001" / "final_9x16.mp4"
    info = media.check_render(final, 1080, 1920)
    assert info["duration"] <= 60 and manifest["duration_s"] == pytest.approx(info["duration"], abs=0.01)
    # Câu cuối của scene cuối nằm trong scene cuối và kết thúc trước khi video hết (T01).
    cues = subtitles.load(td.root / manifest["subtitles"])
    last_scene = manifest["scenes"][-1]
    ass = (td.root / "renders" / "r_001" / "full.ass").read_text(encoding="utf-8")
    last_line = cues[last_scene["id"]]["cues"][-1]
    assert last_line["text"] in ass.replace(r"\N", " ")
    assert last_scene["start_s"] + last_line["end"] <= info["duration"] + 0.05
    assert last_scene["start_s"] + last_line["start"] >= last_scene["start_s"]
    # Gate final cần phiếu duyệt trước khi duyệt (FR-18).
    with pytest.raises(ActionError):
        env.actions.decide(task_id, "approve")
    env.review_all_pass(task_id)
    env.actions.decide(task_id, "approve")
    assert env.task(task_id)["status"] == "completed"
    review = env.store.reviews(task_id)[0]
    assert review["accepted"] == 1 and review["render_rev"] == "r_001"
    # Mọi ứng viên ghi provider và model (NFR-05).
    for scene, kinds in td.assets().items():
        for slot in kinds.values():
            for candidate in slot["candidates"]:
                assert candidate["provider"] and "model" in candidate


def test_resume_skips_done(env):
    task_id = env.new_task()
    run_to_final(env, task_id)
    calls = {k: len(v) for k, v in fake.CALLS.items()}
    env.store.set_status(task_id, "pending")
    assert env.run(task_id) == "waiting"
    assert {k: len(v) for k, v in fake.CALLS.items()} == calls
    assert env.td(task_id).render_revisions() == ["r_001"]


def test_orphan_pending_becomes_unknown_without_resend(env):
    """T05: worker chết ngay sau khi gửi lượt gọi trả phí."""
    task_id = env.new_task()
    fake.SCRIPT["plan"] = fake_plan_data()
    assert env.worker_subprocess(task_id, {"STUDIO_FAKE_EXIT": "image:3"}) == 9
    td = env.td(task_id)
    orphans = costs.orphans(td.costs_path)
    assert [o.request_id for o in orphans] == [f"{task_id}:s03:keyframes:1"]
    env.store.requeue_running()
    fake.CALLS.clear()
    assert env.run(task_id) == "waiting"
    task = env.task(task_id)
    assert task["wait_reason"] == "unknown_call"
    statuses = [e.status for e in costs.read(td.costs_path) if e.request_id.endswith("s03:keyframes:1")]
    assert statuses == ["pending", "unknown"]
    # Không gửi lại s03; các scene còn lại vẫn chạy.
    assert len(fake.CALLS.get("image", [])) == 3
    assert td.selected("s03", "image") is None
    # Người chọn sinh lại: đúng một lượt gọi mới cho s03 (attempt 2).
    env.actions.unknown(task_id, "s03", "retry")
    fake.CALLS.clear()
    assert env.run(task_id) == "waiting" and env.task(task_id)["wait_reason"] == "gate:keyframes"
    assert len(fake.CALLS["image"]) == 1
    assert td.selected("s03", "image").id == "img_s03_a2"


def test_retryable_backoff_then_success(env):
    """T04 với bản giả: TTS lỗi tạm 2 lần, lượt 3 thành công."""
    def flaky(count, seg_id, text):
        if seg_id == "s02" and sum(1 for s, _ in fake.CALLS["tts"] if s == "s02") <= 2:
            raise Retryable("timeout giả")
    task_id = env.new_task({"gates": {"keyframes": False}})
    fake.SCRIPT["tts"] = flaky
    assert env.run(task_id) == "waiting"
    assert env.task(task_id)["wait_reason"] == "gate:final"
    runs = [(r["attempt"], r["status"]) for r in env.store.runs(task_id, "voice", "s02")]
    assert runs == [(1, "failed"), (2, "failed"), (3, "done")]
    assert env.sleeps[:2] == [0.0, 0.0]


def test_retries_exhausted_waits(env):
    def always(count, seg_id, text):
        if seg_id == "s04":
            raise Retryable("429 giả")
    task_id = env.new_task({"gates": {"keyframes": False}})
    fake.SCRIPT["tts"] = always
    assert env.run(task_id) == "waiting"
    assert env.task(task_id)["wait_reason"] == "retries_exhausted"


def test_unconfirmed_is_not_retried(env):
    def boom(count, prompt):
        if "Robot scene s04" in prompt:
            raise Unconfirmed("đọc timeout sau khi gửi")
    task_id = env.new_task()
    fake.SCRIPT["image"] = boom
    assert env.run(task_id) == "waiting"
    assert env.task(task_id)["wait_reason"] == "unknown_call"
    td = env.td(task_id)
    s04 = [e.status for e in costs.read(td.costs_path) if ":s04:keyframes:" in e.request_id]
    assert s04 == ["pending", "unknown"]
    assert sum("Robot scene s04" in p for p in fake.CALLS["image"]) == 1


def test_quality_fail_retries_only_that_scene(env):
    sizes = iter([(640, 640)])
    original = fake.FakeImage.generate

    def generate(self, prompt, refs, size, out):
        if "Robot scene s01" in prompt:
            fake.SCRIPT["image_size"] = next(sizes, (1080, 1920))
        try:
            return original(self, prompt, refs, size, out)
        finally:
            fake.SCRIPT.pop("image_size", None)

    fake.FakeImage.generate = generate
    try:
        task_id = env.new_task()
        assert env.run(task_id) == "waiting"
    finally:
        fake.FakeImage.generate = original
    prompts = [p for p in fake.CALLS["image"] if "Robot scene s01" in p]
    assert len(prompts) == 2 and "Correction:" not in prompts[1]   # lý do kỹ thuật không vào prompt ảnh
    assert env.td(task_id).selected("s01", "image").id == "img_s01_a2"
    # Lượt trượt chất lượng vẫn có dòng ok trong sổ chi phí (đã bị tính tiền).
    statuses = [e.status for e in costs.read(env.td(task_id).costs_path) if e.request_id.endswith("s01:keyframes:1")]
    assert statuses == ["pending", "ok"]


def test_plan_rejects_must_show_too_early(env):
    """T12: mầm cây ở scene 1 của brief-02 bị từ chối, plan gọi lại kèm lý do."""
    fake.SCRIPT["plan"] = lambda count, prompt, schema: fake_plan_data(early=(count == 1))
    task_id = env.new_task()
    assert env.run(task_id) == "waiting"
    assert len(fake.CALLS["llm"]) == 2
    assert "mầm cây" in fake.CALLS["llm"][1] and "sớm hơn scene 4" in fake.CALLS["llm"][1]
    plan = env.td(task_id).plan(1)
    first = min(i for i, s in enumerate(plan.scenes, 1) if "mầm cây" in s.shows)
    assert first >= 4


def test_plan_word_budget_enforced(env):
    short = {f"s0{i}": "Robot tưới cây mỗi ngày." for i in range(1, 7)}
    fake.SCRIPT["plan"] = lambda count, prompt, schema: fake_plan_data(words=short if count == 1 else None)
    task_id = env.new_task()
    assert env.run(task_id) == "waiting"
    assert "ngoài khoảng 45–58 giây" in fake.CALLS["llm"][1]


def test_voice_over_cap_waits_without_render(env):
    """T11: tổng giọng vượt max_s → waiting duration_exceeded, không render."""
    fake.SCRIPT["seconds_per_word"] = 0.5        # 168 từ × 0,5 = 84 giây
    task_id = env.new_task({"gates": {"keyframes": False}})
    assert env.run(task_id) == "waiting"
    task = env.task(task_id)
    assert task["wait_reason"] == "duration_exceeded" and "> max_s 60s" in task["wait_detail"]
    assert env.td(task_id).render_revisions() == []
    assert not env.store.runs(task_id, "render")


def test_edit_narration_reruns_only_dependents(env):
    """T02 (phần core): sửa narration s05 → voice s05, subtitles, animate s05, render; ảnh không sinh lại."""
    task_id = env.new_task()
    run_to_final(env, task_id)
    env.review_all_pass(task_id)
    env.actions.decide(task_id, "approve")
    before = last_run_id(env.store, task_id)
    images = len(fake.CALLS["image"])
    text = fake_plan_data()["scenes"][4]["narration"].replace("ôm chặt", "ghì chặt")
    assert env.actions.edit_scene(task_id, "s05", narration=text) == 2
    assert env.run(task_id) == "waiting" and env.task(task_id)["wait_reason"] == "gate:final"
    assert len(fake.CALLS["image"]) == images
    rerun = sorted({(step, scene) for step, scene, status in steps_run(env.store, task_id, before) if status == "done"})
    assert rerun == [("animate", "s05"), ("render", "_"), ("subtitles", "_"), ("voice", "s05")]
    td = env.td(task_id)
    assert td.render_revisions() == ["r_001", "r_002"]
    manifest = read_manifest(td, "r_002")
    assert sorted(manifest["stages_built"]) == ["assembly", "export", "s05:audio", "s05:scene", "s05:timing"]
    assert (td.root / "renders" / "r_001" / "final_9x16.mp4").is_file()      # bản đã duyệt còn nguyên
    assert td.plan(2).parent == 1


def test_select_other_image_reanimates_one_scene(env):
    task_id = env.new_task()
    run_to_final(env, task_id)
    env.actions.rerun(task_id, "s02", "keyframes", "hands out of frame")
    assert env.run(task_id) == "waiting" and env.task(task_id)["wait_reason"] == "gate:keyframes"
    assert "Correction: hands out of frame." in fake.CALLS["image"][-1]
    assert env.td(task_id).selected("s02", "image").id == "img_s02_a2"
    env.actions.decide(task_id, "approve")
    before = last_run_id(env.store, task_id)
    assert env.run(task_id) == "waiting" and env.task(task_id)["wait_reason"] == "gate:final"
    done = {(step, scene) for step, scene, s in steps_run(env.store, task_id, before) if s == "done"}
    assert done == {("animate", "s02"), ("render", "_")}
    assert env.td(task_id).render_revisions() == ["r_001", "r_002"]
    # Chọn lại ảnh cũ: gate keyframes đã duyệt với lựa chọn đó, clip và render cũ được dùng lại;
    # gate final trỏ đúng r_001 chứ không phải thư mục mới nhất.
    env.actions.select(task_id, "s02", "img_s02_a1")
    before = last_run_id(env.store, task_id)
    assert env.run(task_id) == "waiting" and env.task(task_id)["wait_reason"] == "gate:final"
    assert steps_run(env.store, task_id, before) == []
    assert "r_001" in env.task(task_id)["wait_detail"]
    env.review_all_pass(task_id)
    assert env.store.reviews(task_id)[-1]["render_rev"] == "r_001"


def test_human_actions_refused_while_running(env):
    task_id = env.new_task()
    assert env.store.claim_one(task_id)
    with pytest.raises(ActionError, match="đang chạy"):
        env.actions.select(task_id, "s01", "img_s01_a1")
    assert "dừng" in env.actions.pause(task_id)
    assert env.store.pause_requested(task_id)


def test_pause_stops_at_item_boundary(env):
    task_id = env.new_task()
    assert env.store.claim_one(task_id)
    env.store.request_pause(task_id)
    from studio import pipeline

    assert pipeline.run_task(env.store, env.config, task_id) == "waiting"
    assert env.task(task_id)["wait_reason"] == "paused"
    env.actions.resume(task_id)
    assert env.task(task_id)["status"] == "pending"


def test_motion_resume_polls_old_job(env):
    """Poll sau resume (T06 với FakeVideo): worker chết khi đang poll → chạy lại không submit mới."""
    plan = fake_plan_data()
    fake.SCRIPT["plan"] = plan
    fake.SCRIPT["video_polls_before_ready"] = 2
    task_id = env.new_task({"adapters": {"video": "fake"}, "gates": {"keyframes": False}})
    # Dừng ở ảnh s01 để kịp sửa scene thành motion trước khi các bước sau chạy.
    assert env.worker_subprocess(task_id, {"STUDIO_FAKE_EXIT": "image:1"}) == 9
    env.store.requeue_running()
    env.store.set_status(task_id, "pending")
    assert env.run(task_id) == "waiting"           # image s01 mồ côi → unknown_call
    env.actions.unknown(task_id, "s01", "retry")
    env.actions.edit_scene(task_id, "s01", mode="motion")
    assert env.worker_subprocess(task_id, {"STUDIO_FAKE_EXIT": "video_poll:1"}) == 9
    runs = env.store.runs(task_id, "motion", "s01")
    assert len(runs) == 1 and runs[0]["job_id"] == "job-1"
    env.store.requeue_running()
    fake.CALLS.clear()
    assert env.run(task_id) == "waiting", env.task(task_id)["wait_detail"]
    assert "video_submit" not in fake.CALLS and fake.CALLS["video_poll"] == ["job-1", "job-1"]
    td = env.td(task_id)
    assert td.selected("s01", "clip").id == "mot_s01_a1"
    statuses = [e.status for e in costs.read(td.costs_path) if ":s01:motion:" in e.request_id]
    assert statuses == ["pending", "ok"]


def test_assets_concurrent_writers(env, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from studio.contracts import AssetCandidate
    from studio.taskdir import TaskDir

    td = TaskDir(tmp_path / "d", "t_9999")

    def add(i):
        td.add_candidate("s01", AssetCandidate(id=f"img_s01_a{i}", kind="image", path=f"k/{i}.png", provider="fake"), select=False)

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(add, range(1, 41)))
    assert len(td.candidates("s01", "image")) == 40
    assert td.selected("s01", "image").id is not None


def test_content_refusal_retries_then_waits(env):
    from studio.providers.base import ContentRefused

    def refuse(count, prompt):
        if "Robot scene s03" in prompt:
            raise ContentRefused("provider chặn nội dung (moderation)")
    task_id = env.new_task()
    fake.SCRIPT["image"] = refuse
    assert env.run(task_id) == "waiting"
    assert env.task(task_id)["wait_reason"] == "retries_exhausted"
    statuses = [e.status for e in costs.read(env.td(task_id).costs_path) if ":s03:keyframes:" in e.request_id]
    assert statuses == ["pending", "failed"] * 3
    assert "safe-for-all-audiences" in [p for p in fake.CALLS["image"] if "Robot scene s03" in p][-1]
