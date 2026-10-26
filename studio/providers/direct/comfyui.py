"""ImageProvider qua workflow ComfyUI đã duyệt trên rental ready; không tự thuê/cài model.

Mỗi attempt có run bền vững để web theo dõi. Submit/đối soát/tải/kiểm output dùng ComfyService như UI;
timeout sau submit là Unconfirmed, không tạo prompt thay thế. Chi phí GPU vẫn ở rental, không phân bổ giả
vào costs.jsonl của ảnh. Một adapter tuần tự hóa các scene vì rental chỉ nhận một run mỗi lúc.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studio import costs
from studio.comfyui import runtime
from studio.comfyui import service as comfy_service
from studio.comfyui.runs import file_sha256
from studio.contracts import AssetCandidate
from studio.gpu import service as gpu_service
from studio.gpu.adapters.base import GPUError
from studio.providers.base import Fatal, QualityFail, Retryable, Unconfirmed
from studio.store import Store


class ImageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    rental_id: str = Field(pattern=r"^r_[0-9a-f]{12}$")
    workflow_id: str = Field(pattern=r"^wf_[0-9a-f]{10}$")
    revision: int = Field(default=1, ge=1)
    reference_workflow_id: str = ""
    reference_revision: int = Field(default=1, ge=1)
    model: str = "FLUX.2-klein-4b-fp8"
    timeout_s: float = Field(default=600, gt=0)
    poll_s: float = Field(default=2, gt=0)


class ComfyImage:
    name = "comfyui"
    paid = True
    max_refs = 1

    def __init__(self, config: dict):
        try:
            self.settings = ImageSettings(**config.get("comfyui", {}).get("image", {}))
        except ValidationError as exc:
            error = exc.errors()[0]
            raise Fatal(f"[comfyui.image] {error['loc']}: {error['msg']}") from None
        if self.settings.reference_workflow_id and not re.fullmatch(r"wf_[0-9a-f]{10}", self.settings.reference_workflow_id):
            raise Fatal("reference_workflow_id không hợp lệ")
        self.model = self.settings.model
        self.input_signature = self.settings.model_dump(exclude={"rental_id", "timeout_s", "poll_s"})
        self.input_signature["adapter_version"] = "comfy-image-v1"
        self.config = config
        self._lock = threading.Lock()

    def _services(self):
        store = Store(Path(self.config["data_dir"]) / "studio.db")
        try:
            gpu = gpu_service.from_config(store, self.config)
        except (GPUError, gpu_service.GPUServiceError) as exc:
            raise Fatal(str(exc)) from None
        if gpu is None:
            raise Fatal("ComfyImage cần bật GPU provider và chọn rental ready")
        return store, gpu, comfy_service.from_config(store, self.config, gpu)

    def generate(self, prompt: str, refs: list[Path], size: str, out: Path) -> AssetCandidate:
        with self._lock:
            return self._generate(prompt, refs, size, out)

    def _generate(self, prompt: str, refs: list[Path], size: str, out: Path) -> AssetCandidate:
        if len(refs) > self.max_refs:
            raise Fatal("ComfyImage nhận một character sheet; ghép các ảnh tham chiếu trước khi chạy")
        if not re.fullmatch(r"[0-9]{2,4}x[0-9]{2,4}", size):
            raise Fatal("size phải dạng WIDTHxHEIGHT")
        width, height = (math.ceil(int(v) / 16) * 16 for v in size.split("x"))
        if not 16 <= width <= 4096 or not 16 <= height <= 4096:
            raise Fatal("kích thước ngoài khoảng 16–4096")
        store, gpu, comfy = self._services()
        request_id = costs.current_request_id() or f"direct:{uuid.uuid4().hex}"
        try:
            fingerprint = hashlib.sha256(json.dumps({"prompt": prompt, "refs": [file_sha256(Path(p)) for p in refs],
                                                     "size": [width, height], "profile": self.input_signature},
                                                    sort_keys=True).encode()).hexdigest()
        except OSError as exc:
            raise Fatal(f"không đọc được ảnh tham chiếu: {exc}") from None
        # Cùng attempt trong cùng task không submit hai lần, kể cả khi adapter được dựng lại.
        run_id = "cr_" + hashlib.sha256(request_id.encode()).hexdigest()[:12]
        try:
            run = comfy.run(run_id)
        except comfy_service.ComfyServiceError:
            run = None
        started = time.monotonic()
        if run is None:
            try:
                rental = gpu.rental(self.settings.rental_id)
            except KeyError as exc:
                raise Fatal(str(exc)) from None
            if rental.status == "busy":
                raise Retryable(f"rental {rental.rental_id} đang busy; chưa gửi prompt")
            if rental.status != "ready":
                raise Fatal(f"rental {rental.rental_id} đang {rental.status}, cần ready")
            workflow_id, revision = self.settings.workflow_id, self.settings.revision
            if refs:
                workflow_id, revision = self.settings.reference_workflow_id, self.settings.reference_revision
                if not workflow_id:
                    raise Fatal("có ảnh tham chiếu nhưng chưa chọn reference_workflow_id")
            try:
                graph = comfy.image_template(workflow_id, revision)
                image_nodes = [n for n in graph.values() if n["class_type"] == "LoadImage"]
                if refs and len(image_nodes) != 1:
                    raise Fatal("workflow tham chiếu cần đúng một LoadImage cho character sheet")
                if not refs and image_nodes:
                    raise Fatal("workflow text-to-image có LoadImage nhưng chưa cung cấp ảnh tham chiếu")
                # Kiểm dependency đã duyệt trước khi upload bất kỳ file nào.
                bindings = {}
                reference = None
                if refs:
                    reference = gpu.comfy_runtime(rental.rental_id).upload_image(Path(refs[0]))
                    prompt = ("The reference is a character sheet of one subject from several angles. "
                              "Render exactly one subject in the requested scene, preserving its design. "
                              "Do not reproduce the sheet layout, panels or text. Scene: " + prompt)
                for node_id, node in graph.items():
                    kind = node["class_type"]
                    if kind == "CLIPTextEncode":
                        bindings[node_id] = {"text": prompt}
                    elif kind in ("EmptyFlux2LatentImage", "Flux2Scheduler"):
                        bindings[node_id] = {"width": width, "height": height}
                    elif kind == "RandomNoise":
                        bindings[node_id] = {"noise_seed": int(hashlib.sha256(request_id.encode()).hexdigest()[:16], 16)}
                    elif kind == "SaveImage":
                        bindings[node_id] = {"filename_prefix": f"igniterra_{run_id}"}
                    elif kind == "LoadImage" and reference:
                        bindings[node_id] = {"image": reference}
                bound = comfy.bind_image_inputs(workflow_id, revision, bindings, request_id, fingerprint)
                run = comfy.start_run(run_id, bound.workflow_id, bound.revision, rental.rental_id)
            except runtime.ComfyUIUnavailable as exc:
                raise Retryable(gpu.redact(str(exc))) from None
            except (runtime.ComfyUIError, gpu_service.GPUServiceError, comfy_service.ComfyServiceError, OSError) as exc:
                # start_run có thể đã ghi run trước khi lỗi tới đây; không retry mù.
                if any(r["run_id"] == run_id for r in store.comfy_runs()):
                    raise Unconfirmed(f"{run_id}: {gpu.redact(str(exc))}; theo dõi /comfyui/runs/{run_id}") from None
                raise Fatal(gpu.redact(str(exc))) from None
        elif run.rental_id != self.settings.rental_id:
            raise Fatal("attempt đã gắn với rental khác; kiểm run trước khi đổi cấu hình")
        elif f"input_sha256:{fingerprint}" not in (comfy.revision(run.workflow_id, run.revision).note or ""):
            raise Fatal("attempt đã ghi input khác; cần tạo attempt mới thay vì dùng lại run cũ")

        while run.status not in ("succeeded", "failed", "blocked", "abandoned", "output_failed"):
            if time.monotonic() - started >= self.settings.timeout_s:
                raise Unconfirmed(f"{run_id} chưa xong; theo dõi /comfyui/runs/{run_id}; không submit lại")
            try:
                run = comfy.refresh(run_id)
            except (runtime.ComfyUIError, gpu_service.GPUServiceError, comfy_service.ComfyServiceError) as exc:
                raise Unconfirmed(f"{run_id}: {gpu.redact(str(exc))}; theo dõi /comfyui/runs/{run_id}") from None
            if run.status not in ("succeeded", "failed", "blocked", "abandoned", "output_failed"):
                time.sleep(self.settings.poll_s)
        if run.status in ("output_failed", "abandoned"):
            raise Unconfirmed(f"{run_id} {run.status}; xử lý output tại /comfyui/runs/{run_id}, không tự sinh lại")
        if run.status == "failed":
            raise QualityFail(f"ComfyUI run {run_id}: {gpu.redact(run.detail or run.error_code or 'execution error')}")
        if run.status == "blocked":
            raise Fatal(f"ComfyUI run {run_id} bị chặn: {gpu.redact(run.detail or run.error_code or '')}")
        images = [o for o in store.run_outputs(run_id) if o["status"] == "verified" and o["media_type"].startswith("image/")]
        if len(images) != 1:
            raise QualityFail(f"{run_id}: cần đúng một ảnh đã kiểm, nhận {len(images)}")
        image = images[0]
        root = Path(self.config["data_dir"]).resolve()
        source = (root / image["path"]).resolve()
        if not source.is_relative_to(root):
            raise Fatal("output ComfyUI nằm ngoài data_dir")
        target = out.with_suffix(Path(image["filename"]).suffix.lower())
        try:
            if file_sha256(source) != image["sha256"]:
                raise OSError("output đã đổi sau khi kiểm")
            shutil.copyfile(source, target)
            if file_sha256(target) != image["sha256"]:
                raise OSError("bản sao keyframe không khớp SHA-256")
        except OSError as exc:
            target.unlink(missing_ok=True)
            raise Unconfirmed(f"{run_id}: {exc}; output đã sinh, không tự submit lại") from None
        costs.report_usage(units={"images": 1}, model=self.model)
        return AssetCandidate(id="tmp", kind="image", path=str(target), provider=self.name, model=self.model,
                              source=f"comfyui:{run_id}", ai_generated=True,
                              rights="AI-generated; model/dependency theo manifest ComfyUI đã duyệt")
