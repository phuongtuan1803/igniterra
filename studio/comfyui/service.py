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
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from studio import config as studio_config
from studio.comfyui import inputs, workflows
from studio.comfyui import judge as judges
from studio.comfyui import manifest as manifests
from studio.comfyui import runs as outputs
from studio.comfyui import runtime as comfy
from studio.comfyui.contracts import (
    GROUP_OPEN,
    RUN_EXECUTING,
    RUN_TERMINAL,
    RUN_TRANSITIONS,
    RUN_UNRESOLVED,
    AttemptGroup,
    ComfyManifestRecord,
    Run,
    RunEvent,
    RunOutput,
    WorkflowRevision,
)
from studio.comfyui.manifest import WorkflowManifest
from studio.gpu.service import GPUService, GPUServiceError
from studio.paths import contained
from studio.store import Store

WORKFLOW_ID = re.compile(r"wf_[0-9a-f]{10}")
STAGE_ID = re.compile(r"st_[0-9a-f]{12}")
GROUP_ID = re.compile(r"cg_[0-9a-f]{12}")
STAGE_TTL_S = 24 * 3600
DEFAULT_RUN_S = 60         # ước tính thời gian một lượt khi workflow chưa có lượt nào xong
MAX_ATTEMPTS = 5
MAX_CAP_USD = 5.0
MAX_DOWNLOAD_ATTEMPTS = 3
SUBMIT_GRACE_S = 120       # run kẹt ở checking/submitting lâu hơn thế: tiến trình đã dừng giữa chừng
LOST_AFTER_MISSES = 2      # số lượt tra liên tiếp không thấy prompt ở /queue lẫn /history trước khi coi là mất
API_GAP = ("API JSON không có metadata model/custom node: không xác định được dependency; "
           "khai manifest rồi duyệt trước khi chạy")
log = logging.getLogger("studio.comfyui")

# Lớp web chỉ đi qua module này (09 · s2.b3): các hằng và hàm thuần về mapping, manifest, file được nhắc lại ở đây.
KINDS, FIELD_KINDS, MAX_FIELDS, SEED_MAX = inputs.KINDS, inputs.FIELD_KINDS, inputs.MAX_FIELDS, inputs.SEED_MAX
InputError = inputs.InputError
candidates, field_limits, limit_text, parse_limit = inputs.candidates, inputs.limits, inputs.limit_text, inputs.parse_limit
seed_field = inputs.seed_field
license_warnings, file_sha256 = manifests.license_warnings, outputs.file_sha256


def parse_manifest(raw: bytes | str) -> WorkflowManifest:
    return WorkflowManifest.model_validate_json(raw)


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
                 lost_after_misses: int = LOST_AFTER_MISSES, judge=None):
        self.store = store
        self.gpu = gpu
        # Chấm lượt tự kiểm: callable(ảnh, ảnh tham chiếu | None) → verdict (studio.comfyui.judge); None: chỉ tự duyệt.
        self.judge = judge
        # data/comfyui/runs/<run_id>/: output đã tải và kiểm (phục vụ qua /files của web).
        self.data_dir = Path(data_dir) if data_dir else Path(store.path).parent / "comfyui"
        self.max_download_attempts = max_download_attempts
        self.submit_grace_s = submit_grace_s
        self.lost_after_misses = lost_after_misses

    # ------------------------------------------------------------ thư viện workflow (G06)

    def new_workflow_id(self) -> str:
        return f"wf_{secrets.token_hex(5)}"

    def import_workflow(self, name: str, raw: bytes, *, api_raw: bytes | None = None, workflow_id: str | None = None,
                        note: str | None = None, kind: str | None = None) -> WorkflowRevision:
        """Nhập một file workflow thành revision mới (bất biến) của ``workflow_id`` (mới nếu không truyền).

        GUI JSON: trích dependency; đủ thông tin để khai manifest (thực tế chỉ khi workflow toàn node lõi, không
        model) thì gắn manifest ``extracted`` chờ duyệt, còn thiếu gì thì ``needs_review`` kèm danh sách ``gaps``.
        API JSON: luôn ``needs_review``. Cùng nội dung với revision mới nhất thì trả revision đó, không tạo thêm.
        ``kind``: loại người dùng xác nhận ở S15; không truyền thì đoán từ ``class_type``.
        """
        if kind is not None and kind not in inputs.KINDS:
            raise ComfyServiceError(f"loại workflow lạ: {kind!r}")
        if workflow_id is not None:
            if not WORKFLOW_ID.fullmatch(workflow_id):
                raise ComfyServiceError(f"workflow_id lạ: {workflow_id!r}")
            latest = self.revision(workflow_id)
        else:
            workflow_id, latest = self.new_workflow_id(), None
        parsed = self._parse(raw, api_raw)
        fmt, draft, class_types = parsed["format"], parsed["draft"], parsed["class_types"]
        source_text, api_text = raw.decode("utf-8"), api_raw.decode("utf-8") if api_raw is not None else None
        source_sha, api_sha = parsed["source_sha256"], parsed["api_sha256"]
        if latest is not None and (latest.source_sha256, latest.api_sha256) == (source_sha, api_sha):
            return latest
        revision = self.store.add_workflow_revision(
            workflow_id, (name or "").strip()[:200] or workflow_id, fmt, source_sha, source_text, api_sha,
            source_text if fmt == "api" else api_text, json.dumps(class_types), json.dumps(draft, ensure_ascii=False),
            note, kind=kind or inputs.guess_kind(class_types))
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

    def _parse(self, raw: bytes, api_raw: bytes | None) -> dict:
        """Đọc và kiểm file nhập (chưa ghi gì): dạng, bản trích dependency, class_type, SHA-256."""
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
            raw.decode("utf-8")
            if api_raw is not None:
                api_raw.decode("utf-8")
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
        return {"format": fmt, "draft": draft, "class_types": class_types, "has_api": prompt is not None,
                "source_sha256": workflows.sha256_bytes(raw),
                "api_sha256": workflows.sha256_bytes(raw if fmt == "api" else api_raw) if prompt is not None else None}

    # ------------------------------------------------------------ nhập hai bước (S15) và cảnh báo trùng (F21)

    def stage_import(self, name: str, raw: bytes, *, api_raw: bytes | None = None, workflow_id: str | None = None,
                     origin: str | None = None) -> str:
        """Bước 1: kiểm file rồi giữ tạm dưới ``data/comfyui/staging/<stage_id>/``; chưa tạo revision nào."""
        if workflow_id is not None:
            self.revision(workflow_id)
        self._parse(raw, api_raw)
        self._sweep_stages()
        stage_id = f"st_{secrets.token_hex(6)}"
        folder = self.data_dir / "staging" / stage_id
        folder.mkdir(parents=True)
        (folder / "source.json").write_bytes(raw)
        if api_raw is not None:
            (folder / "api.json").write_bytes(api_raw)
        (folder / "meta.json").write_text(json.dumps({"name": (name or "").strip()[:200], "workflow_id": workflow_id,
                                                      "origin": origin, "created_at": _stamp()}, ensure_ascii=False))
        return stage_id

    def stage_from_machine(self, rental_id: str, path: str, *, workflow_id: str | None = None) -> str:
        """Workflow đã Save trên máy (S13) đi qua cùng bước Kiểm tra & Lưu (S15)."""
        try:
            raw = self._gpu().comfy_runtime(rental_id).read_saved_workflow(path)
        except comfy.ComfyUIError as exc:
            raise ComfyServiceError(f"không đọc được {path} trên máy: {self._clean(str(exc))[:300]}") from None
        title = self.revision(workflow_id).name if workflow_id else path.rsplit("/", 1)[-1].removesuffix(".json")
        return self.stage_import(title, raw, workflow_id=workflow_id, origin=f"lưu từ máy {rental_id}: {path}")

    def _stage_dir(self, stage_id: str) -> Path:
        folder = self.data_dir / "staging" / stage_id
        if not STAGE_ID.fullmatch(stage_id) or not (folder / "meta.json").is_file():
            raise ComfyServiceError("bản nhập tạm không còn (đã lưu, đã hủy hoặc quá 24 giờ); chọn file lại")
        return folder

    def staged(self, stage_id: str) -> dict:
        """Bước 2: kết quả kiểm, loại đoán được và mọi revision cùng nội dung trong cả thư viện."""
        folder = self._stage_dir(stage_id)
        meta = json.loads((folder / "meta.json").read_text())
        raw = (folder / "source.json").read_bytes()
        api_raw = (folder / "api.json").read_bytes() if (folder / "api.json").is_file() else None
        parsed = self._parse(raw, api_raw)
        draft = parsed["draft"]
        return {**parsed, **meta, "stage_id": stage_id, "file_bytes": len(raw), "kind": inputs.guess_kind(parsed["class_types"]),
                "kind_from": _kind_hint(parsed["class_types"]), "models": len(draft["models"]),
                "custom_nodes": len(draft["custom_nodes"]), "gaps": draft["gaps"],
                "duplicates": self.duplicates(parsed["source_sha256"], parsed["api_sha256"]),
                "target": self.revision(meta["workflow_id"]) if meta.get("workflow_id") else None}

    def duplicates(self, source_sha256: str, api_sha256: str | None) -> list[WorkflowRevision]:
        """Revision cùng file gốc hoặc cùng bản API ở bất kỳ workflow nào (F21); chỉ cảnh báo, không gộp, không xóa."""
        return [_revision(row) for row in self.store.same_content_revisions(source_sha256, api_sha256)]

    def commit_stage(self, stage_id: str, *, name: str | None = None, kind: str | None = None,
                     note: str | None = None, keep_duplicate: bool = False) -> WorkflowRevision:
        """Lưu bản nhập tạm thành revision bất biến; có bản trùng thì cần ``keep_duplicate`` (nút Vẫn lưu)."""
        view = self.staged(stage_id)
        if view["duplicates"] and not keep_duplicate:
            first = view["duplicates"][0]
            raise ComfyServiceError(f"đã có {first.name} rev {first.revision} cùng nội dung; mở bản đang có hoặc "
                                    "chọn Vẫn lưu thành bản riêng")
        folder = self._stage_dir(stage_id)
        api_path = folder / "api.json"
        parts = [p for p in (view.get("origin"), (note or "").strip()[:500]) if p]
        record = self.import_workflow(name or view["name"] or "workflow", (folder / "source.json").read_bytes(),
                                      api_raw=api_path.read_bytes() if api_path.is_file() else None,
                                      workflow_id=view.get("workflow_id"), note="\n".join(parts) or None, kind=kind)
        self.discard_stage(stage_id)
        return record

    def discard_stage(self, stage_id: str) -> None:
        import shutil

        if STAGE_ID.fullmatch(stage_id):
            shutil.rmtree(self.data_dir / "staging" / stage_id, ignore_errors=True)

    def _sweep_stages(self) -> None:
        import shutil
        import time

        root = self.data_dir / "staging"
        for folder in root.glob("st_*") if root.is_dir() else ():
            if time.time() - folder.stat().st_mtime > STAGE_TTL_S:
                shutil.rmtree(folder, ignore_errors=True)

    # ------------------------------------------------------------ input mapping (S17) và duyệt trên web (F04)

    def api_prompt(self, record: WorkflowRevision) -> dict:
        """Prompt API đã lưu, kiểm SHA-256 (không cần đã duyệt): để khai mapping và kiểm máy."""
        if not record.runnable:
            raise ComfyServiceError(f"{record.workflow_id} rev {record.revision} chỉ có GUI JSON; nhập kèm bản "
                                    "Export (API) để chạy")
        row = self.store.workflow_revision(record.workflow_id, record.revision)
        api_bytes = (row["api_json"] or "").encode("utf-8")
        if workflows.sha256_bytes(api_bytes) != record.api_sha256:
            raise ComfyServiceError(f"{record.workflow_id} rev {record.revision}: file API không khớp SHA-256 đã lưu")
        prompt = workflows.load(api_bytes)
        problems = workflows.check_api(prompt)
        if problems:
            raise ComfyServiceError("workflow API lỗi: " + "; ".join(problems[:10]))
        return prompt

    def save_inputs(self, workflow_id: str, revision: int, fields: list[dict]) -> WorkflowRevision:
        """Mapping mới là revision mới (cùng file, cùng manifest) và phải duyệt lại; revision cũ và run cũ giữ nguyên."""
        record = self.revision(workflow_id, revision)
        prompt = self.api_prompt(record)
        try:
            clean = inputs.validate_mapping(prompt, fields)
        except inputs.InputError as exc:
            raise ComfyServiceError(str(exc)) from None
        if not clean:
            raise ComfyServiceError("khai ít nhất một ô")
        if clean == record.inputs:
            raise ComfyServiceError("mapping không đổi so với revision này")
        row = self.store.workflow_revision(workflow_id, revision)
        number = self.store.add_workflow_revision(
            workflow_id, record.name, record.format, record.source_sha256, row["source_json"], record.api_sha256,
            row["api_json"], row["class_types"], row["draft_json"], f"khai input từ rev {revision}",
            kind=record.kind, inputs_json=json.dumps(clean, ensure_ascii=False))
        new = self.revision(workflow_id, number)
        manifest = self.manifest_view(record)
        if manifest is not None:
            self._attach(new, manifest, record.manifest_source or "operator", f"dùng lại manifest của rev {revision}")
        return self.revision(workflow_id, number)

    def approve_revision(self, workflow_id: str, revision: int, *, accept_licenses: bool = False) -> WorkflowRevision:
        """Nút Duyệt trên web: duyệt manifest đang chờ (kiểm lại SHA-256 và license) và input mapping của revision."""
        record = self.revision(workflow_id, revision)
        if not record.manifest_sha256:
            raise ComfyServiceError("chưa có manifest dependency: khai manifest trước khi duyệt")
        if record.manifest_status == "revoked":
            raise ComfyServiceError(f"manifest {record.manifest_sha256[:12]} đã bị thu hồi; nhập revision mới")
        if record.manifest_status == "pending_review":
            self.approve_manifest(record.manifest_sha256, accept_licenses=accept_licenses)
        else:
            self._load_manifest(self.manifest(record.manifest_sha256))
        if record.inputs_pending:
            try:
                inputs.validate_mapping(self.api_prompt(record), record.inputs)
            except inputs.InputError as exc:
                raise ComfyServiceError(f"mapping không còn khớp workflow: {exc}") from None
            self.store.approve_workflow_inputs(workflow_id, revision)
        return self.revision(workflow_id, revision)

    def machine_workflows(self, rental_id: str) -> list[dict]:
        """Workflow đã Save trong ComfyUI gốc trên máy ``ready``/``busy`` (S13); chỉ GET, gia hạn idle như mọi lần
        studio dùng máy."""
        try:
            return self._gpu().comfy_runtime(rental_id).saved_workflows()
        except comfy.ComfyUIError as exc:
            raise ComfyServiceError(f"không đọc được workflow trên máy: {self._clean(str(exc))[:300]}") from None

    def import_from_machine(self, rental_id: str, path: str, *, name: str | None = None,
                            workflow_id: str | None = None) -> WorkflowRevision:
        """Lưu một workflow từ máy về thư viện: workflow mới, hoặc revision mới của ``workflow_id`` (không bao giờ ghi
        đè revision cũ). Đi đúng đường ``import_workflow``: trích dependency, ``needs_review`` hoặc chờ duyệt."""
        try:
            raw = self._gpu().comfy_runtime(rental_id).read_saved_workflow(path)
        except comfy.ComfyUIError as exc:
            raise ComfyServiceError(f"không đọc được {path} trên máy: {self._clean(str(exc))[:300]}") from None
        title = name or (self.revision(workflow_id).name if workflow_id else path.rsplit("/", 1)[-1].removesuffix(".json"))
        return self.import_workflow(title, raw, workflow_id=workflow_id, note=f"lưu từ máy {rental_id}: {path}")

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

    def library(self, kind: str | None = None) -> list[dict]:
        """Mỗi workflow một dòng (S14): revision mới nhất, số revision, lần chạy gần nhất và trạng thái chạy được."""
        latest: dict[str, WorkflowRevision] = {}
        counts: dict[str, int] = {}
        for row in self.store.workflow_revisions():
            record = _revision(row)
            counts[record.workflow_id] = counts.get(record.workflow_id, 0) + 1
            if record.workflow_id not in latest or record.revision > latest[record.workflow_id].revision:
                latest[record.workflow_id] = record
        last_runs: dict[str, Run] = {}
        for row in self.store.comfy_runs():
            last_runs.setdefault(row["workflow_id"], Run(**row))
        items = []
        for key, record in latest.items():
            if kind and record.kind != kind:
                continue
            last = last_runs.get(key)
            items.append({"latest": record, "revisions": counts[key], "last_run": last,
                          "readiness": _readiness(record, last)})
        return items

    def _runnable(self, record: WorkflowRevision) -> dict:
        """Prompt API của revision đã duyệt; kiểm lại manifest và SHA-256 file API đã lưu ngay lúc dùng."""
        if not record.runnable:
            raise ComfyServiceError(f"{record.workflow_id} rev {record.revision} chỉ có GUI JSON; nhập kèm bản "
                                    "Export (API) để chạy")
        if record.status != "approved":
            what = "input mapping chưa duyệt" if record.inputs_pending else f"đang {record.status}"
            raise ComfyServiceError(f"{record.workflow_id} rev {record.revision} {what}: chỉ chạy "
                                    "revision có manifest dependency (và input mapping) đã duyệt")
        self._load_manifest(self.manifest(record.manifest_sha256))
        return self.api_prompt(record)

    # ------------------------------------------------------------ chạy (G07 nửa run, G08)

    def new_run_id(self) -> str:
        """Sinh trước khi hiện form chạy; dùng làm nonce chống bấm hai lần."""
        return f"cr_{secrets.token_hex(6)}"

    def ready_rentals(self) -> list:
        return [r for r in self.gpu.machines() if r.status == "ready"] if self.gpu else []

    def start_run(self, run_id: str, workflow_id: str, revision: int, rental_id: str, values: dict | None = None, *,
                  group_id: str | None = None, attempt: int | None = None) -> Run:
        """Chạy revision đã duyệt trên một rental ``ready``, với giá trị cho các ô đã khai (S16).

        Thứ tự: kiểm (manifest, file API, mapping, giá trị input, rental) → ghi run ``checking`` → giành máy
        ``ready→busy`` → kiểm ``/object_info`` (thiếu ``class_type`` hay model thì ``blocked``, ghi nguyên nhân, không
        submit) → upload ảnh input (tên theo SHA-256) → ghi ``prompt_id`` (``submitting``) → ``POST /prompt``.
        """
        gpu = self._gpu()
        if not outputs.RUN_ID.fullmatch(run_id):
            raise ComfyServiceError(f"run_id lạ: {run_id!r}")
        record = self.revision(workflow_id, revision)
        prompt = self._runnable(record)
        if values and not record.inputs:
            raise ComfyServiceError(f"{workflow_id} rev {revision} chưa khai input; không nhận giá trị nhập")
        prepared = self.prepare_inputs(record, values or {}) if record.inputs else {}
        images = self._input_images(record, prepared)
        sent = inputs.apply(prompt, record.inputs, {k: images[k][1] if k in images else v for k, v in prepared.items()})
        try:
            rental = gpu.rental(rental_id)
        except KeyError:
            raise ComfyServiceError(f"không có rental {rental_id}") from None
        if rental.status != "ready":
            raise ComfyServiceError(f"rental {rental_id} đang {rental.status}; chỉ chạy trên rental ready")
        try:
            self.store.add_comfy_run(run_id, workflow_id, record.revision, workflows.prompt_sha256(sent),
                                     record.manifest_sha256, rental_id,
                                     inputs_json=json.dumps(prepared, ensure_ascii=False) if record.inputs else None,
                                     group_id=group_id, attempt=attempt)
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
        skip = {(f["node"], f["input"]) for f in record.inputs if f["key"] in images}
        missing, invalid = workflows.check_object_info(sent, info, skip)
        if missing or invalid:
            if missing:
                code, detail = "missing_class_type", ("runtime thiếu class_type: " + ", ".join(missing)
                                                      + " (custom node chưa cài trên rental này); không submit")
            else:
                code, detail = "invalid_input", "giá trị không có trên runtime (thiếu model?): " + "; ".join(invalid[:10])
            self._move(self.run(run_id), "blocked", error_code=code, detail=detail)
            return self.run(run_id)
        for path, name in images.values():
            try:
                uploaded = runtime.upload_image(path)
            except comfy.ComfyUIError as exc:
                self._move(self.run(run_id), "blocked", error_code="input_upload_failed",
                           detail=f"upload ảnh input lỗi: {self._clean(str(exc))[:300]}; chưa submit")
                return self.run(run_id)
            if uploaded != name:
                self._move(self.run(run_id), "blocked", error_code="input_upload_failed",
                           detail=f"máy lưu ảnh input dưới tên khác {uploaded!r}; chưa submit")
                return self.run(run_id)

        prompt_id = str(uuid.uuid4())
        if not self._move(self.run(run_id), "submitting", prompt_id=prompt_id, submitted_at=_stamp()):
            return self.run(run_id)
        run = self.run(run_id)
        try:
            runtime.submit(sent, prompt_id, client_id=f"igniterra:{run_id}")
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

    # ------------------------------------------------------------ input, ước tính, kiểm máy (S16)

    @property
    def data_root(self) -> Path:
        """Thư mục data của studio (``data/``); output và ảnh input nằm dưới đây, phục vụ qua ``/files``."""
        return self.data_dir.parent

    def prepare_inputs(self, record: WorkflowRevision, values: dict) -> dict:
        """Giá trị form → giá trị đã kiểm theo mapping; ảnh và nhân vật thành ``{path, sha256}`` (gọi lại an toàn)."""
        try:
            resolved = inputs.resolve(record.inputs, values, self.api_prompt(record))
        except inputs.InputError as exc:
            raise ComfyServiceError(str(exc)) from None
        for field in record.inputs:
            key = field["key"]
            if key in resolved and field["kind"] == "image":
                resolved[key] = self._image_value(resolved[key], field["label"])
            elif key in resolved and field["kind"] == "character":
                resolved[key] = self._character_value(resolved[key], field["label"])
        return resolved

    def save_input_image(self, raw: bytes) -> str:
        """Ảnh người dùng tải lên cho ô Ảnh: lưu ``comfyui/inputs/<sha256>.<đuôi>``; trả đường dẫn dưới data."""
        if len(raw) > comfy.REF_MAX_BYTES:
            raise ComfyServiceError("ảnh lớn hơn 64 MiB")
        try:
            name, _ = comfy.ref_image_name(raw)
        except comfy.ComfyUIRejected as exc:
            raise ComfyServiceError(str(exc)) from None
        target = self.data_dir / "inputs" / name.removeprefix("igniterra_ref_")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            target.write_bytes(raw)
        return os.path.relpath(target, self.data_root)

    def _image_value(self, raw, label: str) -> dict:
        rel = raw.get("path") if isinstance(raw, dict) else str(raw)
        path = contained(self.data_dir / "inputs", Path(str(rel)).name) if rel else None
        if path is None or not path.is_file() or os.path.relpath(path, self.data_root) != rel:
            raise ComfyServiceError(f"{label}: chưa có ảnh tải lên hợp lệ")
        digest = outputs.file_sha256(path)
        if not path.name.startswith(digest):
            raise ComfyServiceError(f"{label}: ảnh đã bị sửa sau khi tải lên")
        return {"path": rel, "sha256": digest}

    def characters(self) -> list[dict]:
        """Nhân vật có ảnh tham chiếu đã duyệt (character sheet) trong mọi Series, cho ô Nhân vật."""
        from studio.series import SeriesDir, SeriesError, list_series

        found = []
        for name in list_series(self.data_root):
            try:
                bible = SeriesDir(self.data_root, name).bible()
            except (SeriesError, OSError, ValueError):
                continue
            for key, character in bible.characters.items():
                if character.sheet:
                    found.append({"ref": f"{name}/{key}", "label": f"{key} · Series {name} · {len(character.refs)} ảnh đã duyệt"})
        return found

    def _character_value(self, raw, label: str) -> dict:
        from studio.series import SeriesDir, SeriesError

        ref = raw.get("ref") if isinstance(raw, dict) else str(raw)
        series, _, key = str(ref or "").partition("/")
        sd, character = None, None
        try:
            sd = SeriesDir(self.data_root, series)
            character = sd.bible().characters.get(key)
        except (SeriesError, OSError, ValueError):
            pass
        path = contained(sd.root, character.sheet) if sd and character and character.sheet else None
        if path is None or not path.is_file():
            raise ComfyServiceError(f"{label}: nhân vật {ref!r} chưa có ảnh tham chiếu đã duyệt")
        digest = outputs.file_sha256(path)
        if isinstance(raw, dict) and raw.get("sha256") and raw["sha256"] != digest:
            raise ComfyServiceError(f"{label}: ảnh tham chiếu của {ref} đã đổi so với lượt đầu")
        return {"ref": ref, "path": os.path.relpath(path, self.data_root), "sha256": digest}

    def _input_images(self, record: WorkflowRevision, prepared: dict) -> dict[str, tuple[Path, str]]:
        """Ô ảnh/nhân vật → (file trên đĩa, tên sẽ có trên máy). Tên theo SHA-256 nên prompt tính được trước upload."""
        found = {}
        for field in record.inputs:
            value = prepared.get(field["key"])
            if field["kind"] in ("image", "character") and isinstance(value, dict):
                path = self.data_root / value["path"]
                found[field["key"]] = (path, comfy.ref_image_name(path.read_bytes())[0])
        return found

    def estimate(self, workflow_id: str, rental) -> dict:
        """Ước tính một lượt (không phải hóa đơn): trung vị thời gian 5 lượt xong gần nhất × giá giờ của máy."""
        import statistics

        seconds = [r.gpu_seconds for r in self.runs(workflow_id=workflow_id)
                   if r.status == "succeeded" and r.gpu_seconds is not None][:5]
        if seconds:
            span, basis = float(statistics.median(seconds)), f"trung vị {len(seconds)} lượt gần nhất"
        else:
            span, basis = float(DEFAULT_RUN_S), f"chưa có lượt nào xong, giả định {DEFAULT_RUN_S} giây"
        return {"seconds": round(span), "basis": basis, "usd": rental.offer.dph_total * span / 3600,
                "budgeted": rental.provider_key != "fixed"}

    def machine_options(self, workflow_id: str) -> list[dict]:
        return [{"rental": r, "estimate": self.estimate(workflow_id, r)} for r in self.ready_rentals()]

    def check_machine(self, workflow_id: str, revision: int, rental_id: str) -> tuple[list[str], list[str]]:
        """Cổng 4 trước khi chạy: đọc ``/object_info`` của máy (GET, không tốn lượt chạy)."""
        record = self.revision(workflow_id, revision)
        prompt = self.api_prompt(record)
        try:
            info = self._gpu().comfy_runtime(rental_id).object_info()
        except (GPUServiceError, comfy.ComfyUIError) as exc:
            raise ComfyServiceError(f"không đọc được /object_info: {self._clean(str(exc))[:300]}") from None
        skip = {(f["node"], f["input"]) for f in record.inputs if f["kind"] in ("image", "character")}
        return workflows.check_object_info(prompt, info, skip)

    def verified_output(self, run_id: str, media: str) -> RunOutput | None:
        """Output đầu tiên đã kiểm SHA-256 của loại ``image``/``audio``/``video``; chưa kiểm thì không có."""
        return next((RunOutput(**o) for o in self.store.run_outputs(run_id)
                     if o["status"] == "verified" and o["path"] and (o["media_type"] or "").startswith(media + "/")), None)

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
        """Theo dõi mọi run chưa xong (worker hoặc CLI gọi định kỳ), rồi một bước cho mỗi nhóm tự kiểm đang chạy.
        ``output_failed`` chờ người vận hành tải lại."""
        results = []
        for row in self.store.comfy_runs(statuses=tuple(s for s in RUN_UNRESOLVED if s != "output_failed")):
            try:                           # một run lỗi không làm bỏ qua các run khác trong lượt
                results.append(self.refresh(row["run_id"]))
            except (ComfyServiceError, GPUServiceError) as exc:
                log.warning("poll %s: %s", row["run_id"], self._clean(str(exc)))
                self.store.add_comfy_event(row["run_id"], "poll", "poll_error", self._clean(str(exc)))
                results.append(self.run(row["run_id"]))
        self.advance_groups()
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

    # ------------------------------------------------------------ tự kiểm và chạy lại (S18)

    @property
    def judge_enabled(self) -> bool:
        return self.judge is not None and bool(getattr(self.judge, "enabled", True))

    @property
    def judge_cost_usd(self) -> float:
        return float(getattr(self.judge, "cost_usd", 0.0)) if self.judge is not None else 0.0

    def start_group(self, run_id: str, workflow_id: str, revision: int, rental_id: str, values: dict, *,
                    judge: str, max_attempts: int, cap_usd: float) -> AttemptGroup:
        """Lượt 1 của nhóm tự kiểm. Mỗi lượt là một run thường; lượt sau chỉ đổi seed, cùng các input khác."""
        if judge not in ("vision", "manual"):
            raise ComfyServiceError(f"cách kiểm lạ: {judge!r}")
        if judge == "vision" and not self.judge_enabled:
            raise ComfyServiceError("chưa chọn model vision ([comfyui.judge] model); chọn Tôi tự duyệt từng lượt")
        if not 1 <= max_attempts <= MAX_ATTEMPTS:
            raise ComfyServiceError(f"số lượt tối đa trong 1–{MAX_ATTEMPTS}")
        if not 0 < cap_usd <= MAX_CAP_USD:
            raise ComfyServiceError(f"trần chi phí trong (0, {MAX_CAP_USD:g}] USD")
        if not outputs.RUN_ID.fullmatch(run_id):
            raise ComfyServiceError(f"run_id lạ: {run_id!r}")
        record = self.revision(workflow_id, revision)
        self._runnable(record)
        if inputs.seed_field(record.inputs) is None:
            raise ComfyServiceError("tự kiểm cần một ô Seed trong input mapping để mỗi lượt ra kết quả khác")
        try:
            rental = self._gpu().rental(rental_id)
        except KeyError:
            raise ComfyServiceError(f"không có rental {rental_id}") from None
        per_attempt = self.estimate(workflow_id, rental)["usd"] + (self.judge_cost_usd if judge == "vision" else 0.0)
        if per_attempt > cap_usd:
            raise ComfyServiceError(f"ước tính một lượt ~${per_attempt:.3f} lớn hơn trần ${cap_usd:.2f}")
        prepared = self.prepare_inputs(record, values)
        group_id = "cg_" + run_id.removeprefix("cr_")
        try:
            self.store.add_comfy_group(group_id, workflow_id, record.revision, rental_id,
                                       json.dumps(prepared, ensure_ascii=False), judge, max_attempts, cap_usd, per_attempt)
        except sqlite3.IntegrityError:
            raise ComfyServiceError(f"{run_id} đã được dùng; không chạy lại (bấm hai lần?)") from None
        return self._next_attempt(self.group(group_id), run_id=run_id)

    def group(self, group_id: str) -> AttemptGroup:
        try:
            return AttemptGroup(**self.store.comfy_group(group_id))
        except KeyError as exc:
            raise ComfyServiceError(str(exc).strip("'\"")) from None

    def group_runs(self, group_id: str) -> list[Run]:
        return sorted((Run(**row) for row in self.store.comfy_runs(group_id=group_id)), key=lambda r: r.attempt or 0)

    def groups(self, *, workflow_id: str | None = None) -> list[AttemptGroup]:
        return [AttemptGroup(**row) for row in self.store.comfy_groups(workflow_id=workflow_id)]

    def _next_attempt(self, group: AttemptGroup, *, run_id: str | None = None) -> AttemptGroup:
        values = json.loads(group.inputs_json)
        number = group.attempts + 1
        if number > 1:
            record = self.revision(group.workflow_id, group.revision)
            seed = inputs.seed_field(record.inputs)
            used = {r.inputs.get(seed["key"]) for r in self.group_runs(group.group_id)}
            values[seed["key"]] = next(v for v in iter(lambda: secrets.randbelow(inputs.SEED_MAX + 1), None)
                                       if v not in used)
        if not self.store.update_comfy_group(group.group_id, group.version, attempts=number, status="running"):
            return self.group(group.group_id)              # bên khác đã mở lượt này
        try:
            self.start_run(run_id or self.new_run_id(), group.workflow_id, group.revision, group.rental_id, values,
                           group_id=group.group_id, attempt=number)
        except (ComfyServiceError, GPUServiceError) as exc:
            current = self.group(group.group_id)
            self.store.update_comfy_group(current.group_id, current.version, status="stopped",
                                          stop_reason=f"Không mở được lượt {number}: {self._clean(str(exc))[:300]}")
        return self.group(group.group_id)

    def _spent(self, group: AttemptGroup, runs: list[Run]) -> float:
        dph = self._gpu().rental(group.rental_id).offer.dph_total if self.gpu is not None else 0.0
        total = 0.0
        for run in runs:
            total += dph * (run.gpu_seconds or 0.0) / 3600
            if run.verdict and run.verdict.get("by") == "vision":
                total += self.judge_cost_usd
        return round(total, 6)

    def _passed_unknown(self, run: Run) -> bool:
        return any(e["transition"].endswith("submit_unknown") for e in self.store.comfy_events(run.run_id))

    def advance(self, group_id: str) -> AttemptGroup:
        """Một bước của vòng tự kiểm, gọi lại an toàn (worker và nút trên web): chấm lượt vừa xong, rồi dừng hoặc mở
        lượt sau. Lượt không ``succeeded`` thì dừng, không chạy lại; lượt từng ``submit_unknown`` không mở lượt mới."""
        group = self.group(group_id)
        if group.status != "running":
            return group
        runs = self.group_runs(group_id)
        last = runs[-1] if runs else None
        if last is None or last.status in (*RUN_EXECUTING, "downloading", "output_failed"):
            return group
        spent = self._spent(group, runs)
        number = last.attempt or len(runs)
        if last.status != "succeeded":
            reason = (f"Lượt {number}: không rõ ComfyUI đã nhận prompt (submit_unknown); đã đối soát, không gửi lại"
                      if self._passed_unknown(last) else
                      f"Lượt {number} {last.status}{' · ' + last.error_code if last.error_code else ''}; không chạy lại")
            self.store.update_comfy_group(group_id, group.version, status="stopped", stop_reason=reason, spent_usd=spent)
            return self.group(group_id)
        verdict = last.verdict
        if verdict is None:
            if group.judge == "manual":
                self.store.update_comfy_group(group_id, group.version, status="awaiting_review", spent_usd=spent,
                                              stop_reason=f"Lượt {number} xong; chờ bạn duyệt")
                return self.group(group_id)
            try:
                verdict = self._judge(last)
            except judges.JudgeError as exc:
                self.store.update_comfy_group(group_id, group.version, status="awaiting_review", spent_usd=spent,
                                              stop_reason=f"Chấm tự động lỗi ({self._clean(str(exc))[:200]}); bạn tự duyệt lượt {number}")
                return self.group(group_id)
            if not self.store.set_run_verdict(last.run_id, json.dumps(verdict, ensure_ascii=False)):
                return self.group(group_id)                # bên khác đã chấm
            runs = self.group_runs(group_id)
            spent = self._spent(group, runs)
        return self._decide(group, number, verdict, spent, self._passed_unknown(last))

    def _decide(self, group: AttemptGroup, number: int, verdict: dict, spent: float, unknown: bool) -> AttemptGroup:
        group_id = group.group_id
        if verdict.get("passed"):
            self.store.update_comfy_group(group_id, group.version, status="passed", spent_usd=spent,
                                          stop_reason=f"Lượt {number} đạt" + (f"; còn {group.max_attempts - number} lượt chưa dùng"
                                                                             if group.max_attempts > number else ""))
        elif unknown:
            self.store.update_comfy_group(group_id, group.version, status="stopped", spent_usd=spent,
                                          stop_reason=f"Lượt {number} từng ở submit_unknown; không tự mở lượt mới")
        elif number >= group.max_attempts:
            self.store.update_comfy_group(group_id, group.version, status="exhausted", spent_usd=spent,
                                          stop_reason=f"Hết {group.max_attempts} lượt, chưa lượt nào đạt")
        elif spent + group.est_attempt_usd > group.cap_usd + 1e-9:
            self.store.update_comfy_group(group_id, group.version, status="capped", spent_usd=spent,
                                          stop_reason=f"Dừng trước lượt {number + 1}: đã dùng ~${spent:.3f}, thêm một lượt "
                                                      f"~${group.est_attempt_usd:.3f} sẽ vượt trần ${group.cap_usd:.2f}")
        else:
            if not self.store.update_comfy_group(group_id, group.version, spent_usd=spent):
                return self.group(group_id)
            return self._next_attempt(self.group(group_id))
        return self.group(group_id)

    def _judge(self, run: Run) -> dict:
        image = self.verified_output(run.run_id, "image")
        if image is None:
            raise judges.JudgeError("lượt này không có ảnh đã kiểm để chấm")
        record = self.revision(run.workflow_id, run.revision)
        reference = next((self.data_root / run.inputs[f["key"]]["path"] for f in record.inputs
                          if f["kind"] == "character" and isinstance(run.inputs.get(f["key"]), dict)), None)
        return self.judge(self.data_root / image.path, reference)

    def review(self, group_id: str, run_id: str, ok: bool, note: str = "") -> AttemptGroup:
        """Người dùng tự duyệt lượt mới nhất (cách kiểm thủ công, hoặc khi chấm tự động lỗi)."""
        group = self.group(group_id)
        runs = self.group_runs(group_id)
        if group.status != "awaiting_review" or not runs or runs[-1].run_id != run_id:
            raise ComfyServiceError("chỉ duyệt được lượt mới nhất khi nhóm đang chờ duyệt")
        if not self.store.set_run_verdict(run_id, json.dumps(judges.manual(ok, note), ensure_ascii=False)):
            raise ComfyServiceError(f"{run_id} đã được chấm hoặc chưa succeeded")
        last = self.run(run_id)
        return self._decide(group, last.attempt or len(runs), last.verdict, self._spent(group, self.group_runs(group_id)),
                            self._passed_unknown(last))

    def choose(self, group_id: str, run_id: str) -> AttemptGroup:
        """Người dùng chọn lượt dùng làm kết quả; chỉ lượt ``succeeded`` (mọi output đã kiểm SHA-256)."""
        group = self.group(group_id)
        run = next((r for r in self.group_runs(group_id) if r.run_id == run_id), None)
        if run is None or run.status != "succeeded":
            raise ComfyServiceError("chỉ chọn được lượt đã xong và đã kiểm output")
        fields = {"chosen_run_id": run_id}
        if group.status in GROUP_OPEN:
            fields.update(status="passed", stop_reason=f"Bạn chọn lượt {run.attempt}")
        if not self.store.update_comfy_group(group_id, group.version, **fields):
            raise ComfyServiceError("nhóm vừa đổi ở nơi khác; tải lại trang")
        return self.group(group_id)

    def stop_group(self, group_id: str) -> AttemptGroup:
        """Dừng nhóm: không mở lượt mới; lượt đang chạy vẫn chạy xong và được tải, kiểm như thường."""
        group = self.group(group_id)
        if group.status in GROUP_OPEN:
            self.store.update_comfy_group(group_id, group.version, status="stopped", stop_reason="Bạn dừng nhóm")
        return self.group(group_id)

    def advance_groups(self) -> list[AttemptGroup]:
        results = []
        for row in self.store.comfy_groups(statuses=("running",)):
            try:
                results.append(self.advance(row["group_id"]))
            except (ComfyServiceError, GPUServiceError) as exc:
                log.warning("advance %s: %s", row["group_id"], self._clean(str(exc)))
        return results

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
    data = {k: v for k, v in row.items() if k in WorkflowRevision.model_fields and k != "inputs"}
    return WorkflowRevision(**{**data, "class_types": json.loads(row["class_types"]), "draft": json.loads(row["draft_json"]),
                               "inputs": json.loads(row["inputs_json"]) if row.get("inputs_json") else []})


def _readiness(record: WorkflowRevision, last: Run | None) -> tuple[str, str]:
    """(nhãn, mức chip) cho cột Chạy được? ở S14."""
    if not record.runnable:
        return "Thiếu bản API", "bad"
    if record.status == "needs_review":
        return "Chờ khai dependency", "warn"
    if record.status != "approved":
        return "Chờ duyệt", "warn"
    if last is not None and last.revision == record.revision and last.error_code in ("missing_class_type", "invalid_input"):
        return "Thiếu node hoặc model trên máy", "warn"
    return "Sẵn sàng", "ok"


def _kind_hint(class_types: list[str]) -> str | None:
    """Node làm căn cứ cho loại đoán được (hiện ở S15 để người dùng biết vì sao)."""
    guess = inputs.guess_kind(class_types)
    needles = {"tts": ("tts", "vieneu"), "i2v": ("video", "webm"), "fix": ("detailer",), "upscale": ("upscale",),
               "i2i": ("loadimage",)}.get(guess, ())
    return next((c for c in class_types if any(n in c.lower() for n in needles)), None)


def from_config(store: Store, config: dict, gpu: GPUService | None) -> ComfyService:
    """Thư viện workflow luôn bật (nhập, duyệt không cần GPU); chạy cần ``gpu`` khác None."""
    comfyui = config.get("comfyui", {})
    return ComfyService(store, gpu, data_dir=Path(config["data_dir"]) / "comfyui" if config.get("data_dir") else None,
                        max_download_attempts=int(comfyui.get("max_download_attempts", MAX_DOWNLOAD_ATTEMPTS)),
                        submit_grace_s=float(comfyui.get("submit_grace_s", SUBMIT_GRACE_S)),
                        lost_after_misses=int(comfyui.get("lost_after_misses", LOST_AFTER_MISSES)),
                        judge=judges.OmniRouteJudge(config))


WATCH_INTERVAL_S = 10.0


def start_watch(store: Store, config: dict, gpu: GPUService | None, *,
                stop: threading.Event | None = None) -> threading.Thread | None:
    """Luồng nền trong worker: ``poll()`` mỗi ``[comfyui] watch_interval_s`` giây, để run chạy từ web tự tải và kiểm
    output, và nhóm tự kiểm tự sang lượt sau mà không cần bấm Kiểm tiến độ. None khi chưa bật GPU."""
    if gpu is None:
        return None
    comfy = from_config(store, config, gpu)
    interval = float(config.get("comfyui", {}).get("watch_interval_s", WATCH_INTERVAL_S))
    if not interval > 0:
        raise ComfyServiceError("[comfyui] watch_interval_s phải > 0")
    stop = stop or threading.Event()

    def loop() -> None:
        while not stop.wait(interval):
            try:
                comfy.poll()
            except Exception as exc:  # noqa: BLE001 — một lượt lỗi không dừng luồng theo dõi
                log.error("theo dõi ComfyUI: %s", comfy._clean(str(exc)))

    thread = threading.Thread(target=loop, name="comfy-watch", daemon=True)
    thread.start()
    return thread
