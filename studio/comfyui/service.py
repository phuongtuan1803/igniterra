"""Thư viện workflow và lượt chạy ComfyUI (09 · ComfyUI workflow và lượt chạy). Web và CLI chỉ gọi lớp này.

- **Nhập** (G06): mỗi file là một revision bất biến; dependency trích từ GUI JSON khi có, API JSON không có metadata
  thì ``needs_review``: không đoán, không cài, không chạy được tới khi người vận hành gắn manifest và duyệt.
- **Duyệt**: manifest dependency ở ``comfy_manifests`` (khóa SHA-256 nội dung), duyệt bằng CLI như manifest
  provisioning; mỗi lần dùng kiểm lại SHA-256 của manifest và của file API đã lưu.
- **Không tự cài lên rental**: manifest workflow không sinh hay gộp vào manifest provisioning của GPU. Rental ``ready``
  có chạy được workflow hay không do ``GET /object_info`` quyết định ngay trước khi submit (G07, nửa run).
- **Submit** (G08): ghi ``prompt_id`` (UUID do studio sinh) vào ``comfy_runs`` rồi mới ``POST /prompt``. Kết quả
  không rõ → ``submit_unknown``, tra ``/queue`` rồi ``/history/{prompt_id}``; không bao giờ submit lại.
- **Output** (G11): tải ``/view`` ra ``data/comfyui/runs/<run_id>/``, kiểm SHA-256 và loại media; chỉ khi mọi output
  đã kiểm mới ``succeeded``. Rental còn run chưa kiểm output thì destroy cần xác nhận riêng (``GPUService.destroy``).

Mọi đổi trạng thái run là CAS theo (status, version) ở SQL (``Store.transition_comfy_run``): worker poll và nút trên
web chạy cùng lúc thì chỉ một bên ghi; bên thua đọc lại. Rental ``ready→busy`` khi run giành máy, ``busy→ready``
khi run rời nhóm đang chạy (``RUN_EXECUTING``), chỉ do bên thắng CAS đó gọi.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from studio import config as studio_config
from studio.comfyui import manifest as manifests
from studio.comfyui import runs as outputs
from studio.comfyui import runtime as comfy
from studio.comfyui import workflows
from studio.comfyui.contracts import (
    RUN_EXECUTING,
    RUN_TERMINAL,
    RUN_TRANSITIONS,
    RUN_UNRESOLVED,
    ComfyManifestRecord,
    Run,
    RunEvent,
    RunOutput,
    WorkflowRevision,
)
from studio.comfyui.manifest import WorkflowManifest
from studio.gpu.service import GPUService, GPUServiceError
from studio.store import Store

WORKFLOW_ID = re.compile(r"wf_[0-9a-f]{10}")
MAX_DOWNLOAD_ATTEMPTS = 3
SUBMIT_GRACE_S = 120       # run kẹt ở checking/submitting lâu hơn thế: tiến trình đã dừng giữa chừng
LOST_AFTER_MISSES = 2      # số lượt tra liên tiếp không thấy prompt ở /queue lẫn /history trước khi coi là mất
API_GAP = ("API JSON không có metadata model/custom node: không xác định được dependency; "
           "khai manifest rồi duyệt trước khi chạy")
log = logging.getLogger("studio.comfyui")


class ComfyServiceError(Exception):
    """Từ chối trước khi đổi gì (workflow chưa duyệt, rental không ready, run_id trùng…)."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _stamp(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class ComfyService:
    def __init__(self, store: Store, gpu: GPUService | None, *, data_dir: Path | None = None,
                 max_download_attempts: int = MAX_DOWNLOAD_ATTEMPTS, submit_grace_s: float = SUBMIT_GRACE_S,
                 lost_after_misses: int = LOST_AFTER_MISSES):
        self.store = store
        self.gpu = gpu
        # data/comfyui/runs/<run_id>/: output đã tải và kiểm (phục vụ qua /files của web).
        self.data_dir = Path(data_dir) if data_dir else Path(store.path).parent / "comfyui"
        self.max_download_attempts = max_download_attempts
        self.submit_grace_s = submit_grace_s
        self.lost_after_misses = lost_after_misses

    # ------------------------------------------------------------ thư viện workflow (G06)

    def new_workflow_id(self) -> str:
        return f"wf_{secrets.token_hex(5)}"

    def import_workflow(self, name: str, raw: bytes, *, api_raw: bytes | None = None, workflow_id: str | None = None,
                        note: str | None = None) -> WorkflowRevision:
        """Nhập một file workflow thành revision mới (bất biến) của ``workflow_id`` (mới nếu không truyền).

        GUI JSON: trích dependency; đủ thông tin để khai manifest (thực tế chỉ khi workflow toàn node lõi, không
        model) thì gắn manifest ``extracted`` chờ duyệt, còn thiếu gì thì ``needs_review`` kèm danh sách ``gaps``.
        API JSON: luôn ``needs_review``. Cùng nội dung với revision mới nhất thì trả revision đó, không tạo thêm.
        """
        if workflow_id is not None:
            if not WORKFLOW_ID.fullmatch(workflow_id):
                raise ComfyServiceError(f"workflow_id lạ: {workflow_id!r}")
            latest = self.revision(workflow_id)
        else:
            workflow_id, latest = self.new_workflow_id(), None
        try:
            data = workflows.load(raw)
            fmt = workflows.detect(data)
            prompt = data if fmt == "api" else None
            if api_raw is not None:
                if fmt == "api":
                    raise workflows.WorkflowError("file chính đã là API JSON; không kèm file API thứ hai")
                prompt = workflows.load(api_raw)
                if workflows.detect(prompt) != "api":
                    raise workflows.WorkflowError("file kèm theo phải là Export (API): {node_id: {class_type, inputs}}")
            source_text, api_text = raw.decode("utf-8"), None
            if api_raw is not None:
                api_text = api_raw.decode("utf-8")
        except (workflows.WorkflowError, UnicodeDecodeError) as exc:
            raise ComfyServiceError(f"workflow không nhập được: {exc}") from None
        if prompt is not None:
            problems = workflows.check_api(prompt)
            if problems:
                raise ComfyServiceError("workflow API lỗi: " + "; ".join(problems[:10]))
        if fmt == "gui":
            draft = workflows.extract_gui(data)
        else:
            draft = {"custom_nodes": [], "models": [], "class_types": workflows.class_types(prompt),
                     "gaps": [API_GAP]}
        if prompt is not None:
            draft["hints"] = workflows.model_hints(prompt)
        class_types = workflows.class_types(prompt) if prompt is not None else draft["class_types"]
        source_sha = workflows.sha256_bytes(raw)
        api_sha = workflows.sha256_bytes(raw if fmt == "api" else api_raw) if prompt is not None else None
        if latest is not None and (latest.source_sha256, latest.api_sha256) == (source_sha, api_sha):
            return latest
        revision = self.store.add_workflow_revision(
            workflow_id, (name or "").strip()[:200] or workflow_id, fmt, source_sha, source_text, api_sha,
            source_text if fmt == "api" else api_text, json.dumps(class_types), json.dumps(draft, ensure_ascii=False),
            note)
        record = self.revision(workflow_id, revision)
        if fmt == "gui" and not draft["gaps"]:
            try:
                manifest = WorkflowManifest(
                    models=[manifests.ModelDep(**m) for m in draft["models"]],
                    custom_nodes=[manifests.NodeDep(**{k: v for k, v in n.items() if k != "class_types"})
                                  for n in draft["custom_nodes"]])
            except ValidationError:
                return record
            if not manifests.validate(manifest):
                self._attach(record, manifest, "extracted", note)
                record = self.revision(workflow_id, revision)
        return record

    def attach_manifest(self, workflow_id: str, revision: int, manifest: WorkflowManifest,
                        note: str | None = None) -> WorkflowRevision:
        """Người vận hành khai manifest cho revision (bắt buộc với ``needs_review``); vào ``pending_review``."""
        problems = manifests.validate(manifest)
        if problems:
            raise ComfyServiceError("manifest không đạt: " + "; ".join(problems))
        record = self.revision(workflow_id, revision)
        if record.status == "approved":
            raise ComfyServiceError(f"{workflow_id} rev {revision} đã có manifest duyệt; đổi dependency thì nhập revision mới")
        self._attach(record, manifest, "operator", note)
        return self.revision(workflow_id, revision)

    def image_template(self, workflow_id: str, revision: int) -> dict:
        """Graph đã kiểm SHA-256 và manifest duyệt, dùng làm template của ImageProvider."""
        return self._runnable(self.revision(workflow_id, revision))

    def import_image_profile(self, folder: Path) -> dict:
        """Nhập hai workflow và manifest đã ghim từ một profile; giữ pending_review, không thuê hay tải model."""
        manifest = WorkflowManifest.model_validate_json((folder / "manifest.json").read_text())
        problems = manifests.validate(manifest)
        if problems:
            raise ComfyServiceError("profile không đạt: " + "; ".join(problems))
        result = {}
        for name in ("text2image", "reference"):
            record = self.import_workflow(f"FLUX.2 Klein 4B · {name}", (folder / f"{name}.json").read_bytes())
            record = self.attach_manifest(record.workflow_id, record.revision, manifest)
            result[name] = {"workflow_id": record.workflow_id, "revision": record.revision}
        result["manifest_sha256"] = manifest.sha256()
        return result

    def bind_image_inputs(self, workflow_id: str, revision: int, bindings: dict, request_id: str,
                          input_sha256: str = "") -> WorkflowRevision:
        """Tạo workflow cho một lượt sinh ảnh từ template đã duyệt; dependency và liên kết không được đổi.

        Chỉ cho thay literal text, seed, kích thước và tên ảnh tham chiếu. Manifest dùng lại bản đã duyệt;
        không duyệt dependency mới. Revision gốc bất biến và workflow phát sinh giữ request_id để monitor.
        """
        import copy

        record = self.revision(workflow_id, revision)
        original = self._runnable(record)
        allowed = {"CLIPTextEncode": {"text"}, "RandomNoise": {"noise_seed"},
                   "EmptyFlux2LatentImage": {"width", "height"}, "Flux2Scheduler": {"width", "height"},
                   "SaveImage": {"filename_prefix"}, "LoadImage": {"image"}}
        prompt = copy.deepcopy(original)
        for node_id, values in bindings.items():
            node = prompt.get(node_id)
            if node is None or not isinstance(values, dict) or not set(values) <= allowed.get(node["class_type"], set()):
                raise ComfyServiceError(f"binding không được phép ở node {node_id}")
            for key, value in values.items():
                if key not in node["inputs"] or isinstance(node["inputs"][key], (list, dict)):
                    raise ComfyServiceError(f"không thay liên kết hoặc thêm input {node_id}.{key}")
                if isinstance(value, (list, dict)) or type(value) is not type(node["inputs"][key]):
                    raise ComfyServiceError(f"binding {node_id}.{key} sai kiểu")
                if key in ("width", "height") and (not 16 <= value <= 4096 or value % 16):
                    raise ComfyServiceError("kích thước ComfyUI phải là bội số 16, trong 16–4096")
                if key == "noise_seed" and not 0 <= value < 2**64:
                    raise ComfyServiceError("seed ngoài khoảng uint64")
                if key == "text" and len(value) > 100_000:
                    raise ComfyServiceError("prompt quá dài")
                if key == "image" and not re.fullmatch(r"igniterra_ref_[0-9a-f]{64}\.(png|jpg|webp)", value):
                    raise ComfyServiceError("ảnh tham chiếu không phải tên SHA-256 do studio upload")
                if key == "filename_prefix" and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
                    raise ComfyServiceError("prefix output không hợp lệ")
            node["inputs"].update(values)
        derived = self.import_workflow(f"Keyframe · {request_id}", json.dumps(prompt).encode(),
                                       note=f"image_request:{request_id}\ninput_sha256:{input_sha256}")
        manifest = self.manifest_view(record)
        return self.attach_manifest(derived.workflow_id, derived.revision, manifest,
                                    note=f"template {workflow_id} rev {revision}; dependency đã duyệt")

    def _attach(self, record: WorkflowRevision, manifest: WorkflowManifest, source: str, note: str | None) -> None:
        sha = manifest.sha256()
        self.store.add_comfy_manifest(sha, manifest.canonical_json(), note)
        if not self.store.attach_comfy_manifest(record.workflow_id, record.revision, record.version, sha, source):
            raise ComfyServiceError(f"{record.workflow_id} rev {record.revision} đã đổi ở nơi khác hoặc đã duyệt; "
                                    "tải lại rồi thử lại")

    def approve_manifest(self, manifest_sha256: str, *, accept_licenses: bool = False) -> ComfyManifestRecord:
        """Duyệt manifest đã đọc; kiểm lại nội dung, SHA-256 và license trước khi đổi trạng thái."""
        record = self.manifest(manifest_sha256)
        manifest = self._load_manifest(record)
        warnings = manifests.license_warnings(manifest)
        if warnings and not accept_licenses:
            raise ComfyServiceError("cần xác nhận license (--accept-licenses): " + "; ".join(warnings))
        if record.status == "approved":
            return record
        if not self.store.set_comfy_manifest_status(manifest_sha256, "approved", expect="pending_review"):
            raise ComfyServiceError(f"manifest {manifest_sha256[:12]} đang {record.status}; không duyệt được")
        return self.manifest(manifest_sha256)

    def manifest(self, manifest_sha256: str) -> ComfyManifestRecord:
        try:
            return ComfyManifestRecord(**self.store.comfy_manifest(manifest_sha256))
        except KeyError as exc:
            raise ComfyServiceError(str(exc).strip("'\"")) from None

    def manifest_view(self, record: WorkflowRevision) -> WorkflowManifest | None:
        if not record.manifest_sha256:
            return None
        return WorkflowManifest.model_validate_json(self.manifest(record.manifest_sha256).manifest_json)

    def _load_manifest(self, record: ComfyManifestRecord) -> WorkflowManifest:
        """Manifest từ SQLite; SHA-256 phải khớp nội dung (không ai sửa sau khi duyệt) và vẫn đạt kiểm tra."""
        try:
            manifest = WorkflowManifest.model_validate_json(record.manifest_json)
        except ValidationError as exc:
            raise ComfyServiceError(f"manifest {record.manifest_sha256[:12]} hỏng: {exc.errors()[0]['msg']}") from None
        if manifest.sha256() != record.manifest_sha256:
            raise ComfyServiceError(f"manifest {record.manifest_sha256[:12]} không khớp SHA-256 đã lưu")
        problems = manifests.validate(manifest)
        if problems:
            raise ComfyServiceError("manifest không đạt: " + "; ".join(problems))
        return manifest

    def revision(self, workflow_id: str, revision: int | None = None) -> WorkflowRevision:
        try:
            return _revision(self.store.workflow_revision(workflow_id, revision))
        except KeyError as exc:
            raise ComfyServiceError(str(exc).strip("'\"")) from None

    def revisions(self, workflow_id: str) -> list[WorkflowRevision]:
        return [_revision(row) for row in self.store.workflow_revisions(workflow_id)]

    def library(self) -> list[dict]:
        """Mỗi workflow một dòng: revision mới nhất và số revision."""
        latest: dict[str, WorkflowRevision] = {}
        counts: dict[str, int] = {}
        for row in self.store.workflow_revisions():
            record = _revision(row)
            counts[record.workflow_id] = counts.get(record.workflow_id, 0) + 1
            if record.workflow_id not in latest or record.revision > latest[record.workflow_id].revision:
                latest[record.workflow_id] = record
        return [{"latest": record, "revisions": counts[key]} for key, record in latest.items()]

    def _runnable(self, record: WorkflowRevision) -> dict:
        """Prompt API của revision đã duyệt; kiểm lại manifest và SHA-256 file API đã lưu ngay lúc dùng."""
        if not record.runnable:
            raise ComfyServiceError(f"{record.workflow_id} rev {record.revision} chỉ có GUI JSON; nhập kèm bản "
                                    "Export (API) để chạy")
        if record.status != "approved":
            raise ComfyServiceError(f"{record.workflow_id} rev {record.revision} đang {record.status}: chỉ chạy "
                                    "revision có manifest dependency đã duyệt")
        self._load_manifest(self.manifest(record.manifest_sha256))
        row = self.store.workflow_revision(record.workflow_id, record.revision)
        api_bytes = (row["api_json"] or "").encode("utf-8")
        if workflows.sha256_bytes(api_bytes) != record.api_sha256:
            raise ComfyServiceError(f"{record.workflow_id} rev {record.revision}: file API không khớp SHA-256 đã lưu")
        prompt = workflows.load(api_bytes)
        problems = workflows.check_api(prompt)
        if problems:
            raise ComfyServiceError("workflow API lỗi: " + "; ".join(problems[:10]))
        return prompt

    # ------------------------------------------------------------ chạy (G07 nửa run, G08)

    def new_run_id(self) -> str:
        """Sinh trước khi hiện form chạy; dùng làm nonce chống bấm hai lần."""
        return f"cr_{secrets.token_hex(6)}"

    def ready_rentals(self) -> list:
        return [r for r in self.gpu.machines() if r.status == "ready"] if self.gpu else []

    def start_run(self, run_id: str, workflow_id: str, revision: int, rental_id: str) -> Run:
        """Chạy revision đã duyệt trên một rental ``ready``.

        Thứ tự: kiểm (manifest, file API, rental) → ghi run ``checking`` → giành máy ``ready→busy`` → kiểm
        ``/object_info`` (thiếu ``class_type`` hay model thì ``blocked``, ghi nguyên nhân, không submit) → ghi
        ``prompt_id`` (``submitting``) → ``POST /prompt``.
        """
        gpu = self._gpu()
        if not outputs.RUN_ID.fullmatch(run_id):
            raise ComfyServiceError(f"run_id lạ: {run_id!r}")
        record = self.revision(workflow_id, revision)
        prompt = self._runnable(record)
        try:
            rental = gpu.rental(rental_id)
        except KeyError:
            raise ComfyServiceError(f"không có rental {rental_id}") from None
        if rental.status != "ready":
            raise ComfyServiceError(f"rental {rental_id} đang {rental.status}; chỉ chạy trên rental ready")
        try:
            self.store.add_comfy_run(run_id, workflow_id, record.revision, workflows.prompt_sha256(prompt),
                                     record.manifest_sha256, rental_id)
        except sqlite3.IntegrityError:
            raise ComfyServiceError(f"{run_id} đã được dùng; không chạy lại (bấm hai lần?)") from None

        if not gpu.claim_for_run(rental_id, run_id):
            status = gpu.rental(rental_id).status
            self._move(self.run(run_id), "blocked", error_code="rental_busy",
                       detail=f"rental {rental_id} không còn ready (đang {status}); một run mỗi instance")
            return self.run(run_id)
        try:
            runtime = gpu.comfy_runtime(rental_id)
            info = runtime.object_info()
        except (GPUServiceError, comfy.ComfyUIError) as exc:
            self._move(self.run(run_id), "blocked", error_code="runtime_unavailable",
                       detail=f"không đọc được /object_info: {exc}; chưa submit")
            return self.run(run_id)
        missing, invalid = workflows.check_object_info(prompt, info)
        if missing or invalid:
            if missing:
                code, detail = "missing_class_type", ("runtime thiếu class_type: " + ", ".join(missing)
                                                      + " (custom node chưa cài trên rental này); không submit")
            else:
                code, detail = "invalid_input", "giá trị không có trên runtime (thiếu model?): " + "; ".join(invalid[:10])
            self._move(self.run(run_id), "blocked", error_code=code, detail=detail)
            return self.run(run_id)

        prompt_id = str(uuid.uuid4())
        if not self._move(self.run(run_id), "submitting", prompt_id=prompt_id, submitted_at=_stamp()):
            return self.run(run_id)
        run = self.run(run_id)
        try:
            runtime.submit(prompt, prompt_id, client_id=f"igniterra:{run_id}")
        except comfy.ComfyUIUnknown as exc:
            # G08: có thể đã tới ComfyUI. Không gửi lại; tra /queue và /history theo prompt_id.
            self._move(run, "submit_unknown", error_code="submit_unknown", detail=str(exc))
            return self.refresh(run_id)
        except comfy.ComfyUIUnavailable as exc:
            self._move(run, "failed", error_code="not_sent", detail=f"chưa tới được ComfyUI: {exc}")
        except comfy.ComfyUIError as exc:
            self._move(run, "failed", error_code="submit_rejected", detail=str(exc))
        else:
            self._move(run, "queued", last_event="submitted", last_event_at=_stamp())
        return self.run(run_id)

    def refresh(self, run_id: str) -> Run:
        """Một lượt theo dõi (poll): tra ``/queue`` và ``/history/{prompt_id}``, tải và kiểm output. Gọi lại an toàn."""
        run = self.run(run_id)
        if run.status == "checking":
            return self._interrupted(run)
        if run.status in ("submitting", "submit_unknown", "queued", "running"):
            return self._track(run)
        if run.status == "downloading":
            return self._download(run)
        return run

    def poll(self) -> list[Run]:
        """Theo dõi mọi run chưa xong (worker hoặc CLI gọi định kỳ). ``output_failed`` chờ người vận hành tải lại."""
        results = []
        for row in self.store.comfy_runs(statuses=tuple(s for s in RUN_UNRESOLVED if s != "output_failed")):
            try:                           # một run lỗi không làm bỏ qua các run khác trong lượt
                results.append(self.refresh(row["run_id"]))
            except (ComfyServiceError, GPUServiceError) as exc:
                log.warning("poll %s: %s", row["run_id"], self._clean(str(exc)))
                self.store.add_comfy_event(row["run_id"], "poll", "poll_error", self._clean(str(exc)))
                results.append(self.run(row["run_id"]))
        return results

    def _interrupted(self, run: Run) -> Run:
        """``checking`` quá hạn: tiến trình dừng trước khi ghi ``prompt_id``, chắc chắn chưa gửi gì."""
        if (utc_now() - _aware(run.updated_at)).total_seconds() < self.submit_grace_s:
            return run
        self._move(run, "blocked", error_code="interrupted",
                   detail="tiến trình dừng giữa lúc kiểm, trước khi ghi prompt_id; chưa submit")
        return self.run(run.run_id)

    def _track(self, run: Run) -> Run:
        run_id, prompt_id = run.run_id, run.prompt_id
        if run.status == "submitting":
            # Bên khác có thể đang chờ response của POST /prompt: chưa quá hạn thì không động vào.
            if run.submitted_at and (utc_now() - _aware(run.submitted_at)).total_seconds() < self.submit_grace_s:
                return run
            if not self._move(run, "submit_unknown", error_code="submit_interrupted",
                              detail="tiến trình dừng sau khi ghi prompt_id, chưa rõ đã POST /prompt hay chưa"):
                return self.run(run_id)
            run = self.run(run_id)
        try:
            runtime = self._gpu().comfy_runtime(run.rental_id)
            state = runtime.queue_state(prompt_id)       # hỏi queue trước history: không lọt prompt vừa chạy xong
            entry = runtime.history(prompt_id)
        except (GPUServiceError, comfy.ComfyUIError):
            return run                                   # GET lỗi: thử lại ở lượt sau, không đổi gì
        if entry is not None:
            status = comfy.history_status(entry)
            name, stamp_ms, node = comfy.last_event(entry)
            at = datetime.fromtimestamp(stamp_ms / 1000, UTC) if stamp_ms else utc_now()
            progress = {"queue_state": None, "current_node": node, "last_event": name or "history",
                        "last_event_at": _stamp(at), "lookup_misses": 0}
            if status == "running":
                self._move(run, "running", **progress)
            elif status == "error":
                messages = json.dumps((entry.get("status") or {}).get("messages") or [], ensure_ascii=False)
                self._move(run, "failed", error_code="execution_error", detail=f"prompt {prompt_id}: {messages[:600]}",
                           **progress)
            else:
                files = comfy.output_files(entry)
                if not files:
                    self._move(run, "failed", error_code="no_output",
                               detail=f"prompt {prompt_id} xong nhưng không có file output để kiểm", **progress)
                else:
                    self.store.add_run_outputs(run_id, files)
                    if self._move(run, "downloading", **progress):
                        return self._download(self.run(run_id))
            return self.run(run_id)
        if state is not None:
            target = "running" if state == "running" or run.status == "running" else "queued"
            fields = {"queue_state": state, "lookup_misses": 0}
            if target != run.status or state != run.queue_state:
                fields.update(last_event=f"queue_{state}", last_event_at=_stamp())
            self._move(run, target, **fields)
            return self.run(run_id)
        misses = run.lookup_misses + 1
        if misses >= self.lost_after_misses:
            self._move(run, "failed", error_code="prompt_lost", lookup_misses=misses,
                       detail=f"prompt {prompt_id} không có trong /queue và /history sau {misses} lượt tra; "
                              "không submit lại, tạo run mới nếu cần")
        else:
            self._move(run, run.status, lookup_misses=misses)
            self.store.add_comfy_event(run_id, "lookup", "not_found",
                                       f"prompt {prompt_id} chưa thấy ở /queue và /history (lượt {misses})")
        return self.run(run_id)

    # ------------------------------------------------------------ output (G11)

    def run_dir(self, run_id: str) -> Path:
        if not outputs.RUN_ID.fullmatch(run_id):
            raise ComfyServiceError(f"run_id lạ: {run_id!r}")
        return self.data_dir / "runs" / run_id

    def _download(self, run: Run) -> Run:
        """Tải output chưa kiểm; lỗi mạng tính lượt (tối đa ``max_download_attempts``), output sai thì dừng ngay."""
        run_id = run.run_id
        pending = [o for o in self.store.run_outputs(run_id) if o["status"] != "verified"]
        if pending:
            try:
                runtime = self._gpu().comfy_runtime(run.rental_id)
            except GPUServiceError as exc:
                return self._download_failed(run_id, str(exc))
            run_dir = self.run_dir(run_id)
            root = os.path.realpath(self.data_dir.parent)
            for item in pending:
                target = None
                try:
                    target = outputs.output_target(run_dir, item["subfolder"], item["filename"], item["folder_type"])
                    got = runtime.download(item["filename"], item["subfolder"], item["folder_type"], target)
                    media = outputs.media_type(item["filename"], got.head, got.content_type)
                    on_disk = outputs.file_sha256(target)
                    if on_disk != got.sha256:
                        raise outputs.OutputInvalid("sha256_mismatch", f"{item['filename']}: SHA-256 trên đĩa "
                                                    f"{on_disk[:12]}… ≠ lúc tải {got.sha256[:12]}…")
                except (outputs.OutputInvalid, comfy.ComfyUIRejected) as exc:
                    code = exc.code if isinstance(exc, outputs.OutputInvalid) else "download_rejected"
                    if target is not None and target.is_file():
                        target.unlink()                  # không để file chưa kiểm nằm trong thư mục run
                    self.store.set_run_output(run_id, item["node"], item["subfolder"], item["filename"], "failed",
                                              error=self._clean(str(exc))[:500])
                    self._move(self.run(run_id), "output_failed", error_code=code, detail=str(exc), only_from="downloading")
                    return self.run(run_id)
                except comfy.ComfyUIError as exc:
                    return self._download_failed(run_id, str(exc))
                self.store.set_run_output(run_id, item["node"], item["subfolder"], item["filename"], "verified",
                                          path=os.path.relpath(target, root), bytes=got.size, sha256=got.sha256,
                                          media_type=media)
        items = self.store.run_outputs(run_id)
        if items and all(o["status"] == "verified" for o in items):
            self._move(self.run(run_id), "succeeded", only_from="downloading",
                       detail=f"{len(items)} output đã tải, kiểm SHA-256 và loại media")
        return self.run(run_id)

    def _download_failed(self, run_id: str, reason: str) -> Run:
        attempts = self.store.bump_download_attempts(run_id)
        self.store.add_comfy_event(run_id, "download", "download_retry",
                                   self._clean(f"lượt {attempts}/{self.max_download_attempts}: {reason}")[:600])
        if attempts >= self.max_download_attempts:
            self._move(self.run(run_id), "output_failed", error_code="download_failed", only_from="downloading",
                       detail=f"tải output lỗi {attempts} lượt: {reason}; output chưa kiểm, chưa destroy rental")
        return self.run(run_id)

    def retry_download(self, run_id: str) -> Run:
        """Người vận hành tải lại output của run ``output_failed`` (rental còn ``ready``/``busy``)."""
        run = self.run(run_id)
        if run.status != "output_failed":
            raise ComfyServiceError(f"{run_id} đang {run.status}; chỉ tải lại run output_failed")
        self.store.reset_failed_outputs(run_id)
        if not self._move(run, "downloading", download_attempts=0):
            return self.run(run_id)
        return self._download(self.run(run_id))

    # ------------------------------------------------------------ trạng thái run

    def _move(self, run: Run, status: str, *, error_code: str | None = None, detail: str | None = None,
              only_from: str | None = None, **fields) -> bool:
        """CAS từ bản đã đọc; False khi bên khác đã đổi trước. ``only_from``: bản đọc lại đã rời trạng thái đó thì
        bỏ qua (không phải lỗi). Rời nhóm đang chạy thì nhả rental ``busy→ready``."""
        if only_from is not None and run.status != only_from:
            return False
        if status != run.status and status not in RUN_TRANSITIONS[run.status]:
            raise ComfyServiceError(f"chuyển trạng thái run không hợp lệ: {run.status}→{status}")
        if status in RUN_TERMINAL:
            fields.setdefault("finished_at", _stamp())
        moved = self.store.transition_comfy_run(run.run_id, run.version, status, expect=run.status,
                                                error_code=error_code,
                                                detail=self._clean(detail)[:1000] if detail else None, **fields)
        if moved and run.status in RUN_EXECUTING and status not in RUN_EXECUTING and self.gpu is not None:
            self.gpu.release_after_run(run.rental_id, run.run_id)
        return moved

    def _gpu(self) -> GPUService:
        if self.gpu is None:
            raise ComfyServiceError("GPU provider chưa bật ([gpu] provider = \"none\"); không có runtime để chạy")
        return self.gpu

    def _clean(self, text: str) -> str:
        return self.gpu.redact(text) if self.gpu is not None else studio_config.redact(text)

    def run(self, run_id: str) -> Run:
        try:
            return Run(**self.store.comfy_run(run_id))
        except KeyError as exc:
            raise ComfyServiceError(str(exc).strip("'\"")) from None

    def runs(self, *, workflow_id: str | None = None, rental_id: str | None = None) -> list[Run]:
        return [Run(**row) for row in self.store.comfy_runs(workflow_id=workflow_id, rental_id=rental_id)]

    def run_detail(self, run_id: str) -> dict:
        run = self.run(run_id)
        rental = None
        if self.gpu is not None:
            try:
                rental = self.gpu.rental(run.rental_id)
            except KeyError:
                pass
        return {"run": run, "revision": self.revision(run.workflow_id, run.revision), "rental": rental,
                "outputs": [RunOutput(**row) for row in self.store.run_outputs(run_id)],
                "events": [RunEvent(**{k: row[k] for k in RunEvent.model_fields}) for row in self.store.comfy_events(run_id)]}


def _revision(row: dict) -> WorkflowRevision:
    data = {k: v for k, v in row.items() if k in WorkflowRevision.model_fields}
    return WorkflowRevision(**{**data, "class_types": json.loads(row["class_types"]), "draft": json.loads(row["draft_json"])})


def from_config(store: Store, config: dict, gpu: GPUService | None) -> ComfyService:
    """Thư viện workflow luôn bật (nhập, duyệt không cần GPU); chạy cần ``gpu`` khác None."""
    comfyui = config.get("comfyui", {})
    return ComfyService(store, gpu, data_dir=Path(config["data_dir"]) / "comfyui" if config.get("data_dir") else None,
                        max_download_attempts=int(comfyui.get("max_download_attempts", MAX_DOWNLOAD_ATTEMPTS)),
                        submit_grace_s=float(comfyui.get("submit_grace_s", SUBMIT_GRACE_S)),
                        lost_after_misses=int(comfyui.get("lost_after_misses", LOST_AFTER_MISSES)))
