"""IGN-UX giai đoạn 3: chạy có kiểm bằng form (S14–S19). Input mapping là revision mới cần duyệt lại, form chỉ thay
ô đã khai, nhập hai bước có cảnh báo trùng cả thư viện, duyệt trên web, giọng đọc, vòng tự kiểm có trần chi phí.

ComfyUI giả của ``test_comfyui`` trên 127.0.0.1; model chấm là hàm giả. 0 USD, không gọi mạng ngoài.
"""

from __future__ import annotations

import io
import json
import re
import wave

import pytest
from fastapi.testclient import TestClient
from test_comfyui import (  # noqa: F401
    CKPT,
    IMAGE,
    MODEL,
    OUTPUT,
    PNG,
    PORTAL,
    approved,
    fake_gpu,
    fc,
    gpu,
    gui_workflow,
    ready_rental,
    server,
    svc,
)

from studio.comfyui import inputs
from studio.comfyui import judge as judges
from studio.comfyui.manifest import WorkflowManifest
from studio.comfyui.service import ComfyService, ComfyServiceError
from studio.web import create_app

PROMPT = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "một con mèo", "clip": ["1", 1]}},
    "3": {"class_type": "KSampler", "inputs": {"seed": 7, "steps": 4, "cfg": 1.0, "model": ["1", 0],
                                               "positive": ["2", 0]}},
    "4": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
    "5": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "igniterra"}},
}
OBJECT_INFO = {
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [[CKPT], {}]}}},
    "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {}]}}},
    "KSampler": {"input": {"required": {"seed": ["INT", {}]}}},
    "LoadImage": {"input": {"required": {"image": [["example.png"], {}]}}},
    "SaveImage": {"input": {"required": {"filename_prefix": ["STRING", {}]}}},
}
FIELDS = [{"label": "Mô tả cảnh", "kind": "text", "node": "2", "input": "text", "required": True, "max_length": "200"},
          {"label": "Seed", "kind": "seed", "node": "3", "input": "seed"}]


@pytest.fixture(autouse=True)
def runtime_nodes(fc):
    fc.object_info = json.loads(json.dumps(OBJECT_INFO))


def mapped(svc, fields=None, prompt=None) -> tuple[str, int]:
    """Workflow đã duyệt, rồi khai input (revision 2) và duyệt mapping."""
    workflow_id, _ = approved(svc, prompt or PROMPT)
    record = svc.save_inputs(workflow_id, 1, fields or FIELDS)
    svc.approve_revision(workflow_id, record.revision)
    return workflow_id, record.revision


def sent(fc, index=-1) -> dict:
    return fc.posts[index]["prompt"]


# ------------------------------------------------------------ input mapping (S17)


def test_mapping_is_a_new_revision_that_needs_approval_again(svc):
    workflow_id, _ = approved(svc, PROMPT)
    record = svc.save_inputs(workflow_id, 1, FIELDS)
    assert record.revision == 2 and record.manifest_status == "approved" and record.status == "pending_review"
    assert [f["key"] for f in record.inputs] == ["in1", "in2"] and record.inputs[0]["max_length"] == 200
    assert svc.revision(workflow_id, 1).inputs == [] and svc.revision(workflow_id, 1).status == "approved"
    with pytest.raises(ComfyServiceError, match="input mapping chưa duyệt"):
        svc.image_template(workflow_id, 2)
    assert svc.approve_revision(workflow_id, 2).status == "approved"
    with pytest.raises(ComfyServiceError, match="không đổi"):
        svc.save_inputs(workflow_id, 2, FIELDS)


@pytest.mark.parametrize("field, reason", [
    ({"label": "Ảnh", "kind": "text", "node": "3", "input": "model"}, "liên kết"),
    ({"label": "Seed", "kind": "text", "node": "3", "input": "seed"}, "không hợp"),
    ({"label": "X", "kind": "text", "node": "9", "input": "text"}, "không có trong workflow"),
    ({"label": "", "kind": "text", "node": "2", "input": "text"}, "tên ô"),
])
def test_mapping_rejects_links_wrong_kinds_and_unknown_nodes(svc, field, reason):
    workflow_id, _ = approved(svc, PROMPT)
    with pytest.raises(ComfyServiceError, match=reason):
        svc.save_inputs(workflow_id, 1, [field])


def test_mapping_candidates_are_literal_inputs_only():
    found = {(c["node"], c["input"]): c for c in inputs.candidates(PROMPT)}
    assert ("3", "model") not in found and ("2", "clip") not in found
    assert found[("3", "seed")]["suggest"] == "seed" and found[("4", "image")]["suggest"] == "image"
    assert inputs.parse_limit("number", "1–4") == {"min": "1", "max": "4"}
    assert inputs.limit_text({"kind": "number", "min": 1, "max": 4}) == "1–4"


def test_kind_is_guessed_from_nodes_and_can_be_overridden(svc):
    tts = {"1": {"class_type": "VieNeuTTS", "inputs": {"text": "xin chào", "voice": "Nữ miền Bắc"}},
           "2": {"class_type": "SaveAudio", "inputs": {"audio": ["1", 0]}}}
    assert svc.import_workflow("tts", json.dumps(tts).encode()).kind == "tts"
    assert svc.import_workflow("flux", json.dumps(PROMPT).encode()).kind == "i2i"
    assert svc.import_workflow("nv", json.dumps(PROMPT).encode() + b" ", kind="character").kind == "character"
    assert inputs.guess_kind(["CheckpointLoaderSimple", "FaceDetailer", "SaveImage"]) == "fix"
    assert inputs.guess_kind(["CheckpointLoaderSimple", "KSampler", "SaveImage"]) == "t2i"


# ------------------------------------------------------------ chạy với input (S16, F20)


def test_run_replaces_only_mapped_inputs_and_records_them(env, svc, gpu, fake_gpu, fc, server):
    fc.object_info = OBJECT_INFO
    workflow_id, revision = mapped(svc)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    run = svc.start_run(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "cô gái áo dài", "in2": "42"})
    assert run.status == "queued" and run.inputs == {"in1": "cô gái áo dài", "in2": 42}
    prompt = sent(fc)
    assert prompt["2"]["inputs"]["text"] == "cô gái áo dài" and prompt["3"]["inputs"]["seed"] == 42
    assert {k: v for k, v in prompt.items() if k not in ("2", "3")} == {k: v for k, v in PROMPT.items() if k not in ("2", "3")}
    assert prompt["2"]["inputs"]["clip"] == ["1", 1]                      # liên kết giữ nguyên
    fc.finish(fc.posts[-1]["prompt_id"])
    assert svc.refresh(run.run_id).status == "succeeded"

    other = svc.start_run(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "con chó"})
    assert 0 <= other.inputs["in2"] <= inputs.SEED_MAX and other.workflow_sha256 != run.workflow_sha256


@pytest.mark.parametrize("values, reason", [({"in2": "1"}, "bắt buộc"), ({"in1": "x" * 201}, "dài quá"),
                                            ({"in1": "a", "in2": "-1"}, "ngoài khoảng"), ({"in1": "a", "in2": "x"}, "phải là số")])
def test_invalid_values_are_refused_before_anything_is_written(svc, gpu, fake_gpu, fc, server, values, reason):
    workflow_id, revision = mapped(svc)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    with pytest.raises(ComfyServiceError, match=reason):
        svc.start_run(svc.new_run_id(), workflow_id, revision, rental_id, values)
    assert svc.runs() == [] and fc.posts == [] and gpu.rental(rental_id).status == "ready"


def test_values_for_a_revision_without_mapping_are_refused(svc, gpu, fake_gpu, fc, server):
    workflow_id, revision = approved(svc, PROMPT)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    with pytest.raises(ComfyServiceError, match="chưa khai input"):
        svc.start_run(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "x"})


def test_image_input_is_uploaded_by_sha_name_and_skips_the_combo_check(env, svc, gpu, fake_gpu, fc, server):
    fc.object_info = OBJECT_INFO
    fields = [*FIELDS, {"label": "Ảnh gốc", "kind": "image", "node": "4", "input": "image", "required": True}]
    workflow_id, revision = mapped(svc, fields)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    rel = svc.save_input_image(PNG)
    assert re.fullmatch(r"comfyui/inputs/[0-9a-f]{64}\.png", rel)
    run = svc.start_run(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a", "in3": rel})
    [(name, data)] = fc.uploads
    assert run.status == "queued" and data == PNG and sent(fc)["4"]["inputs"]["image"] == name
    assert name == f"igniterra_ref_{rel.split('/')[-1]}" and run.inputs["in3"]["path"] == rel

    (env.store.path.parent / rel).write_bytes(PNG + b"sua")              # sửa sau khi tải lên
    with pytest.raises(ComfyServiceError, match="bị sửa"):
        svc.start_run(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a", "in3": rel})
    with pytest.raises(ComfyServiceError, match="ảnh tải lên hợp lệ"):
        svc.start_run(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a", "in3": "../studio.db"})
    with pytest.raises(ComfyServiceError, match="PNG, JPEG hoặc WebP"):
        svc.save_input_image(b"GIF89a" + b"\x00" * 32)


def test_check_machine_reports_missing_nodes_without_running(svc, gpu, fake_gpu, fc, server):
    fc.object_info = {k: v for k, v in OBJECT_INFO.items() if k != "KSampler"}
    workflow_id, revision = mapped(svc)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    missing, invalid = svc.check_machine(workflow_id, revision, rental_id)
    assert missing == ["KSampler"] and invalid == [] and fc.posts == [] and svc.runs() == []


def test_estimate_uses_finished_runs_or_says_it_is_an_assumption(svc, gpu, fake_gpu, fc, server):
    workflow_id, revision = mapped(svc)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    rental = gpu.rental(rental_id)
    first = svc.estimate(workflow_id, rental)
    assert first["seconds"] == 60 and "giả định" in first["basis"] and first["budgeted"]
    assert first["usd"] == pytest.approx(rental.offer.dph_total * 60 / 3600)


# ------------------------------------------------------------ nhập hai bước và trùng (S15, F21)


def test_staged_import_warns_about_any_same_content_revision_and_never_merges(svc):
    raw = json.dumps(PROMPT).encode()
    first = svc.import_workflow("bản đầu", raw)
    svc.import_workflow("bản đầu", json.dumps({**PROMPT, "9": PROMPT["5"]}).encode(), workflow_id=first.workflow_id)
    stage = svc.stage_import("bản sao", raw)                              # trùng rev 1, không phải rev mới nhất
    view = svc.staged(stage)
    assert [(d.workflow_id, d.revision) for d in view["duplicates"]] == [(first.workflow_id, 1)]
    assert view["has_api"] and view["kind"] == "i2i" and view["kind_from"] == "LoadImage" and len(svc.library()) == 1
    with pytest.raises(ComfyServiceError, match="Vẫn lưu thành bản riêng"):
        svc.commit_stage(stage)
    record = svc.commit_stage(stage, name="bản riêng", kind="t2i", keep_duplicate=True)
    assert record.workflow_id != first.workflow_id and record.kind == "t2i" and len(svc.library()) == 2
    with pytest.raises(ComfyServiceError, match="không còn"):
        svc.staged(stage)
    with pytest.raises(ComfyServiceError, match="không nhập được"):
        svc.stage_import("hỏng", b"{")


# ------------------------------------------------------------ duyệt trên web (F04)


def test_approve_revision_checks_licenses_and_needs_a_manifest(svc):
    record = svc.import_workflow("flux", json.dumps(PROMPT).encode())
    with pytest.raises(ComfyServiceError, match="chưa có manifest"):
        svc.approve_revision(record.workflow_id, 1)
    odd = WorkflowManifest(models=[{**MODEL, "license": "flux-1-dev-non-commercial-license"}])
    svc.attach_manifest(record.workflow_id, 1, odd)
    with pytest.raises(ComfyServiceError, match="license"):
        svc.approve_revision(record.workflow_id, 1)
    assert svc.approve_revision(record.workflow_id, 1, accept_licenses=True).status == "approved"


# ------------------------------------------------------------ tự kiểm và chạy lại (S18)


class Judge:
    """Model chấm giả: trả lần lượt các kết quả đã đặt; ``JudgeError`` nếu đặt một Exception."""

    enabled = True
    cost_usd = 0.002

    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def __call__(self, image, reference=None):
        self.calls.append((image, reference))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return judges.normalize(result, by="vision", model="fake/vision")


BAD = {"face_ok": True, "hands_ok": False, "character_match": None, "artifacts": [], "summary": "Tay trái 6 ngón"}
GOOD = {"face_ok": True, "hands_ok": True, "character_match": None, "artifacts": [], "summary": "Đạt"}


@pytest.fixture
def judged(env, gpu):
    def make(*results):
        return ComfyService(env.store, gpu, judge=Judge(*results))
    return make


def finish_last(fc):
    fc.finish(fc.posts[-1]["prompt_id"])


def tick(svc):
    svc.poll()


def test_failed_check_runs_again_with_a_new_seed_then_stops_on_pass(judged, gpu, fake_gpu, fc, server):
    svc = judged(BAD, GOOD)
    workflow_id, revision = mapped(svc)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    group = svc.start_group(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "bé Na", "in2": "421337"},
                            judge="vision", max_attempts=3, cap_usd=0.10)
    assert group.attempts == 1 and group.status == "running" and len(fc.posts) == 1
    finish_last(fc)
    tick(svc)                                         # lượt 1 xong → chấm lỗi → mở lượt 2
    group = svc.group(group.group_id)
    assert group.attempts == 2 and len(fc.posts) == 2
    first, second = sent(fc, 0), sent(fc, 1)
    assert first["3"]["inputs"]["seed"] == 421337 != second["3"]["inputs"]["seed"]
    assert first["2"]["inputs"]["text"] == second["2"]["inputs"]["text"] == "bé Na"
    finish_last(fc)
    tick(svc)
    group = svc.group(group.group_id)
    runs = svc.group_runs(group.group_id)
    assert group.status == "passed" and group.stop_reason.startswith("Lượt 2 đạt") and len(fc.posts) == 2
    assert [r.verdict["passed"] for r in runs] == [False, True] and runs[0].verdict["summary"] == "Tay trái 6 ngón"
    assert group.chosen_run_id is None                # người dùng chọn kết quả cuối
    assert svc.choose(group.group_id, runs[0].run_id).chosen_run_id == runs[0].run_id
    assert group.spent_usd >= 2 * 0.002


def test_attempts_run_out(judged, gpu, fake_gpu, fc, server):
    svc = judged(BAD, BAD)
    workflow_id, revision = mapped(svc)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    group = svc.start_group(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a"}, judge="vision",
                            max_attempts=2, cap_usd=1)
    for _ in range(2):
        finish_last(fc)
        tick(svc)
    group = svc.group(group.group_id)
    assert group.status == "exhausted" and len(fc.posts) == 2 and "Hết 2 lượt" in group.stop_reason


def test_cost_cap_stops_before_the_attempt_that_would_exceed_it(judged, gpu, fake_gpu, fc, server):
    svc = judged(BAD, BAD, BAD)
    workflow_id, revision = mapped(svc)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    per_attempt = svc.estimate(workflow_id, gpu.rental(rental_id))["usd"] + 0.002
    with pytest.raises(ComfyServiceError, match="lớn hơn trần"):
        svc.start_group(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a"}, judge="vision",
                        max_attempts=3, cap_usd=per_attempt / 2)
    group = svc.start_group(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a"}, judge="vision",
                            max_attempts=3, cap_usd=per_attempt * 1.01)
    finish_last(fc)
    tick(svc)
    group = svc.group(group.group_id)
    assert group.status == "capped" and len(fc.posts) == 1 and "vượt trần" in group.stop_reason
    assert group.spent_usd <= group.cap_usd


def test_failed_or_blocked_attempt_stops_without_retry(judged, gpu, fake_gpu, fc, server):
    svc = judged()
    workflow_id, revision = mapped(svc)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    group = svc.start_group(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a"}, judge="vision",
                            max_attempts=3, cap_usd=1)
    fc.finish(fc.posts[-1]["prompt_id"], ok=False)
    tick(svc)
    group = svc.group(group.group_id)
    assert group.status == "stopped" and "failed" in group.stop_reason and len(fc.posts) == 1
    assert svc.judge.calls == []


def test_submit_unknown_is_reconciled_and_never_resubmitted(judged, gpu, fake_gpu, fc, server):
    svc = judged(BAD)
    workflow_id, revision = mapped(svc)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    fc.prompt_mode = "lost"                           # request mất: ComfyUI không bao giờ thấy prompt
    group = svc.start_group(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a"}, judge="vision",
                            max_attempts=3, cap_usd=1)
    [run] = svc.group_runs(group.group_id)
    assert run.status == "submit_unknown"
    for _ in range(3):
        tick(svc)
    group = svc.group(group.group_id)
    assert svc.run(run.run_id).status == "failed" and group.status == "stopped" and "submit_unknown" in group.stop_reason
    assert len(fc.posts) == 1 and len(svc.group_runs(group.group_id)) == 1


def test_attempt_that_passed_through_submit_unknown_does_not_open_another(judged, gpu, fake_gpu, fc, server):
    svc = judged(BAD)
    workflow_id, revision = mapped(svc)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    fc.prompt_mode = "slow_ok"                        # ComfyUI nhận prompt nhưng response mất
    group = svc.start_group(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a"}, judge="vision",
                            max_attempts=3, cap_usd=1)
    finish_last(fc)
    tick(svc)
    group = svc.group(group.group_id)
    [run] = svc.group_runs(group.group_id)
    assert run.status == "succeeded" and run.verdict["passed"] is False
    assert group.status == "stopped" and "submit_unknown" in group.stop_reason and len(fc.posts) == 1


def test_manual_review_and_judge_error_fall_back_to_the_user(judged, gpu, fake_gpu, fc, server):
    svc = judged(judges.JudgeError("model không trả JSON"), judges.JudgeError("model không trả JSON"))
    workflow_id, revision = mapped(svc)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    group = svc.start_group(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a"}, judge="vision",
                            max_attempts=2, cap_usd=1)
    finish_last(fc)
    tick(svc)
    group = svc.group(group.group_id)
    [run] = svc.group_runs(group.group_id)
    assert group.status == "awaiting_review" and "tự duyệt" in group.stop_reason and len(fc.posts) == 1
    group = svc.review(group.group_id, run.run_id, False, "mặt méo")
    assert group.attempts == 2 and len(fc.posts) == 2 and svc.run(run.run_id).verdict["by"] == "manual"
    with pytest.raises(ComfyServiceError, match="mới nhất"):
        svc.review(group.group_id, run.run_id, True)
    finish_last(fc)
    tick(svc)
    last = svc.group_runs(group.group_id)[-1]
    assert svc.group(group.group_id).status == "awaiting_review"
    group = svc.review(group.group_id, last.run_id, True)
    assert group.status == "passed" and len(fc.posts) == 2


def test_autofix_needs_a_seed_field_and_vision_needs_a_model(env, svc, gpu, fake_gpu, fc, server):
    workflow_id, revision = mapped(svc, FIELDS[:1])
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    with pytest.raises(ComfyServiceError, match="ô Seed"):
        svc.start_group(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a"}, judge="manual",
                        max_attempts=2, cap_usd=1)
    assert not ComfyService(env.store, gpu, judge=judges.OmniRouteJudge({})).judge_enabled
    with pytest.raises(ComfyServiceError, match="model vision"):
        svc.start_group(svc.new_run_id(), workflow_id, revision, rental_id, {"in1": "a"}, judge="vision",
                        max_attempts=2, cap_usd=1)


def test_judge_rubric_computes_pass_itself():
    assert judges.normalize({**GOOD, "artifacts": ["chữ vô nghĩa"]}, by="vision")["passed"] is False
    assert judges.normalize({**GOOD, "character_match": False}, by="vision")["passed"] is False
    assert judges.normalize(GOOD, by="vision")["passed"] is True
    with pytest.raises(judges.JudgeError):
        judges.normalize({"face_ok": True}, by="vision")


# ------------------------------------------------------------ web (S14–S19)


def wav_bytes(seconds: float = 1.5, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


@pytest.fixture
def web(env, fc, server):
    config = {**env.config, "gpu": {"provider": "fake", "image": IMAGE, "portal": PORTAL}}
    client = TestClient(create_app(config), follow_redirects=False)
    page = client.get("/comfyui/workflows").text
    client.csrf = page.split('name="csrf" value="')[1].split('"')[0]
    return client


def test_library_filters_by_kind_and_shows_readiness(web):
    comfy = web.app.state.comfy
    approved(comfy, PROMPT)
    comfy.import_workflow("chỉ GUI", gui_workflow([{"id": 1, "type": "EmptyImage", "properties": {"cnr_id": "comfy-core"}}]))
    page = web.get("/comfyui/workflows").text
    assert "Sẵn sàng" in page and "Thiếu bản API" in page and "Dùng mẫu" in page
    for label in inputs.KINDS.values():
        assert label in page
    assert "Xóa" not in page
    only = web.get("/comfyui/workflows?kind=t2i").text
    assert "chỉ GUI" in only and "Sẵn sàng" not in only


def test_web_mapping_run_form_and_approval(web, fc, server):
    comfy, gpu = web.app.state.comfy, web.app.state.gpu
    fc.object_info = OBJECT_INFO
    workflow_id, _ = approved(comfy, PROMPT)
    page = web.get(f"/comfyui/workflows/{workflow_id}/inputs?revision=1").text
    assert 'value="2|text"' in page and 'value="3|model"' not in page and "Lưu thành revision 2" in page
    form = {"csrf": web.csrf, "revision": "1", "label_1": "Mô tả cảnh", "kind_1": "text", "target_1": "2|text",
            "required_1": "on", "limit_1": "300", "label_2": "Seed", "kind_2": "seed", "target_2": "3|seed"}
    assert web.post(f"/comfyui/workflows/{workflow_id}/inputs", data={**form, "csrf": "x"}).status_code == 403
    saved = web.post(f"/comfyui/workflows/{workflow_id}/inputs", data=form)
    assert saved.headers["location"].startswith(f"/comfyui/workflows/{workflow_id}?revision=2")

    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    page = web.get(f"/comfyui/workflows/{workflow_id}?revision=2").text
    assert "Chờ duyệt" in page and "<fieldset disabled" in page and "input mapping" in page
    approve = {"csrf": web.csrf, "revision": "2"}
    assert "x%C3%A1c" not in web.post(f"/comfyui/workflows/{workflow_id}/approve", data=approve).headers["location"]
    assert comfy.revision(workflow_id, 2).status == "pending_review"         # chưa đánh dấu đã đọc
    web.post(f"/comfyui/workflows/{workflow_id}/approve", data={**approve, "confirm": "on"})
    assert comfy.revision(workflow_id, 2).status == "approved"

    checked = web.post(f"/comfyui/workflows/{workflow_id}/check", data={"csrf": web.csrf, "revision": "2",
                                                                       "rental_id": rental_id})
    assert "checked=" in checked.headers["location"] and fc.posts == []    # chỉ đọc /object_info
    assert "Đã kiểm" in web.get(checked.headers["location"]).text
    page = web.get(f"/comfyui/workflows/{workflow_id}?revision=2").text
    assert "<fieldset disabled" not in page and 'name="in_in1"' in page and 'name="in_in2"' in page
    assert 'name="in_' + 'in3"' not in page and "ckpt_name" not in page.split("CHẠY CÓ KIỂM")[1].split("DEPENDENCY")[0]
    assert "Tự kiểm và chạy lại khi lỗi" in page and "ước tính" in page.lower()
    run_id = page.split('name="run_id" value="')[1].split('"')[0]
    run_form = {"csrf": web.csrf, "run_id": run_id, "revision": "2", "rental_id": rental_id, "in_in1": "bé Na", "in_in2": "5"}
    assert fc.posts == [] and "x%C3%A1c+nh%E1%BA%ADn" in web.post(f"/comfyui/workflows/{workflow_id}/runs",
                                                                     data=run_form).headers["location"]
    started = web.post(f"/comfyui/workflows/{workflow_id}/runs", data={**run_form, "confirm": "on"})
    assert started.headers["location"].startswith(f"/comfyui/runs/{run_id}")
    assert sent(fc)["2"]["inputs"]["text"] == "bé Na" and sent(fc)["3"]["inputs"]["seed"] == 5
    run_page = web.get(f"/comfyui/runs/{run_id}").text
    assert "Giá trị đã gửi" in run_page and "bé Na" in run_page



def test_web_two_step_import_with_duplicate_warning(web):
    comfy = web.app.state.comfy
    first = comfy.import_workflow("gốc", json.dumps(PROMPT).encode())
    files = {"file": ("ban-sao.json", json.dumps(PROMPT).encode(), "application/json")}
    stage = web.post("/comfyui/workflows", data={"csrf": web.csrf}, files=files).headers["location"]
    page = web.get(stage).text
    assert "Đã có workflow cùng nội dung" in page and f"/comfyui/workflows/{first.workflow_id}?revision=1" in page
    assert "Vẫn lưu thành bản riêng" in page and "Lưu vào thư viện" not in page and "không tốn phí" in page
    refused = web.post(stage, data={"csrf": web.csrf, "action": "save"})
    assert "err=" in refused.headers["location"] and len(comfy.library()) == 1
    web.post(stage, data={"csrf": web.csrf, "action": "keep", "kind": "upscale"})
    assert len(comfy.library()) == 2


def test_web_group_page_and_actions(env, web, fc, server):
    comfy, gpu = web.app.state.comfy, web.app.state.gpu
    workflow_id, revision = mapped(comfy)
    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    page = web.get(f"/comfyui/workflows/{workflow_id}?revision={revision}").text
    assert "Model vision chấm" not in page and "Tôi tự duyệt từng lượt" in page   # chưa chọn model chấm
    run_id = page.split('name="run_id" value="')[1].split('"')[0]
    started = web.post(f"/comfyui/workflows/{workflow_id}/runs", data={
        "csrf": web.csrf, "run_id": run_id, "revision": str(revision), "rental_id": rental_id, "in_in1": "a",
        "confirm": "on", "autofix": "on", "judge": "manual", "max_attempts": "2", "cap_usd": "0.10"})
    group_url = started.headers["location"].split("?")[0]
    assert re.fullmatch(r"/comfyui/groups/cg_[0-9a-f]{12}", group_url)
    finish_last(fc)
    web.post(group_url + "/refresh", data={"csrf": web.csrf})
    page = web.get(group_url).text
    assert "Chờ bạn duyệt" in page and "Lỗi, chạy lượt sau" in page and "Trần $0.10" in page
    assert "/files/comfyui/runs/" in page and "Dùng kết quả này" in page
    web.post(group_url + "/review", data={"csrf": web.csrf, "run_id": run_id, "verdict": "pass"})
    web.post(group_url + "/choose", data={"csrf": web.csrf, "run_id": run_id})
    page = web.get(group_url).text
    assert "Đang dùng kết quả này" in page and "Dừng vì: Lượt 1 đạt" in page and len(fc.posts) == 1


def test_web_audio_result_and_use_for_scene(env, web, fc, server):
    comfy, gpu = web.app.state.comfy, web.app.state.gpu
    tts = {"1": {"class_type": "VieNeuTTS", "inputs": {"text": "xin chào", "voice": "nu_bac"}},
           "2": {"class_type": "SaveAudio", "inputs": {"audio": ["1", 0], "filename_prefix": "tts"}}}
    fc.object_info = {"VieNeuTTS": {"input": {"required": {}}}, "SaveAudio": {"input": {"required": {}}}}
    fields = [{"label": "Lời thoại", "kind": "text", "node": "1", "input": "text", "required": True},
              {"label": "Giọng", "kind": "voice", "node": "1", "input": "voice", "options": "nu_bac, nam_nam"}]
    workflow_id, revision = mapped(comfy, fields, tts)
    rental_id = ready_rental(gpu, gpu.provider, fc, server)
    with pytest.raises(ComfyServiceError, match="không có trong danh sách"):
        comfy.start_run(comfy.new_run_id(), workflow_id, revision, rental_id, {"in1": "a", "in2": "nu_trung"})
    run = comfy.start_run(comfy.new_run_id(), workflow_id, revision, rental_id,
                          {"in1": "Tối hôm ấy, Na cầm chiếc đèn lồng nhỏ.", "in2": "nam_nam"})
    fc.files["tts_00001_.wav"] = (wav_bytes(), "audio/wav")
    fc.finish(fc.posts[-1]["prompt_id"], files=[{"filename": "tts_00001_.wav", "subfolder": "", "type": "output"}])
    assert comfy.refresh(run.run_id).status == "succeeded"

    task_id = env.new_task()
    env.run(task_id)                                   # có storyboard (dừng ở cổng keyframes)
    page = web.get(f"/comfyui/runs/{run.run_id}").text
    assert '<audio controls' in page and "1,5 giây" in page and "WAV" in page and "24 kHz mono" in page
    assert "Tối hôm ấy, Na cầm chiếc đèn lồng nhỏ." in page and "Dùng cho scene" in page and f"{task_id}/s01" in page
    used = web.post(f"/comfyui/runs/{run.run_id}/use", data={"csrf": web.csrf, "target": f"{task_id}/s03"})
    assert used.headers["location"].startswith(f"/tasks/{task_id}?scene=s03")
    [candidate] = [c for c in env.td(task_id).candidates("s03", "voice") if c.provider == "user"]
    assert candidate.source == f"comfyui:{run.run_id}" and candidate.ai_generated

    output = comfy.verified_output(run.run_id, "audio")
    (env.store.path.parent / output.path).write_bytes(wav_bytes(2.0))       # file đổi sau khi kiểm
    refused = web.post(f"/comfyui/runs/{run.run_id}/use", data={"csrf": web.csrf, "target": f"{task_id}/s04"})
    assert "err=" in refused.headers["location"]


# ------------------------------------------------------------ ẩn và khôi phục (workflow, task, máy GPU)


def test_hiding_a_workflow_keeps_data_and_blocks_runs(env, svc, gpu, fake_gpu, fc, server):
    workflow_id, _ = approved(svc, PROMPT)
    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    svc.hide_workflow(workflow_id)
    assert svc.library() == [] and [i["latest"].workflow_id for i in svc.library(hidden=True)] == [workflow_id]
    assert [i["latest"].workflow_id for i in svc.library(hidden=None)] == [workflow_id]
    assert svc.revision(workflow_id).status == "approved"          # dữ liệu còn nguyên
    with pytest.raises(ComfyServiceError, match="đang ẩn"):
        svc.start_run(svc.new_run_id(), workflow_id, 1, rental_id, {})
    svc.restore_workflow(workflow_id)
    assert len(svc.library()) == 1 and svc.library(hidden=True) == []
    svc.start_run(svc.new_run_id(), workflow_id, 1, rental_id, {})


def test_workflow_with_unfinished_run_cannot_be_hidden(env, svc, gpu, fake_gpu, fc, server):
    workflow_id, _ = approved(svc, PROMPT)
    svc.start_run(svc.new_run_id(), workflow_id, 1, ready_rental(gpu, fake_gpu, fc, server), {})
    with pytest.raises(ComfyServiceError, match="chưa xong"):
        svc.hide_workflow(workflow_id)
    assert len(svc.library()) == 1


def test_web_hide_and_restore_workflow_requires_csrf(web):
    workflow_id, _ = approved(web.app.state.comfy, PROMPT)
    assert web.post(f"/comfyui/workflows/{workflow_id}/hide", data={}).status_code == 403
    response = web.post(f"/comfyui/workflows/{workflow_id}/hide", data={"csrf": web.csrf})
    assert response.status_code == 303 and "msg=" in response.headers["location"]
    assert "Không có workflow nào" not in web.get("/comfyui/workflows?hidden=1").text
    assert workflow_id in web.get("/comfyui/workflows?hidden=1").text
    assert workflow_id not in web.get("/comfyui/workflows").text
    assert "Khôi phục workflow" in web.get(f"/comfyui/workflows/{workflow_id}").text
    web.post(f"/comfyui/workflows/{workflow_id}/restore", data={"csrf": web.csrf})
    assert workflow_id in web.get("/comfyui/workflows").text


def test_web_hides_finished_tasks_but_not_running_ones(env, web):
    done, busy = env.new_task(), env.new_task()
    assert env.store.claim_one(busy) and env.store.set_status(done, "completed")
    refused = web.post(f"/tasks/{busy}/hide", data={"csrf": web.csrf})
    assert "err=" in refused.headers["location"] and busy in web.get("/").text
    assert web.post(f"/tasks/{done}/hide", data={}).status_code == 403
    assert "msg=" in web.post(f"/tasks/{done}/hide", data={"csrf": web.csrf}).headers["location"]
    assert f"/tasks/{done}\"" not in web.get("/").text and f"/tasks/{done}\"" in web.get("/?hidden=1").text
    web.post(f"/tasks/{done}/restore", data={"csrf": web.csrf})
    assert f"/tasks/{done}\"" in web.get("/").text
    assert web.post("/tasks/t_9999/hide", data={"csrf": web.csrf}).status_code == 404


def test_only_destroyed_or_failed_machines_can_be_hidden(env, gpu, fake_gpu, fc, server):
    from studio.gpu.service import GPUServiceError

    rental_id = ready_rental(gpu, fake_gpu, fc, server)
    with pytest.raises(GPUServiceError, match="chỉ ẩn được"):
        gpu.hide(rental_id)
    rental = gpu.rental(rental_id)
    assert env.store.transition_rental(rental_id, rental.version, "destroyed", expect="ready")
    gpu.hide(rental_id)
    assert rental_id in gpu.hidden_ids() and gpu.rental(rental_id).status == "destroyed"   # sổ chi phí còn
    gpu.restore(rental_id)
    assert rental_id not in gpu.hidden_ids()
