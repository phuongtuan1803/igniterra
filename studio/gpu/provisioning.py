"""Manifest provisioning đã duyệt và báo cáo từ instance (09 · Provisioning trên instance).

Đầu vào khai báo gồm repo ghim commit, model Hugging Face ghim revision + SHA-256, disk và
``on_failure: destroy``. Studio sinh YAML có lệnh cài requirements cố định và service ComfyUI sau Caddy;
không nhận shell tùy ý từ form. Manifest chuẩn hóa được duyệt trong SQLite trước khi thuê, rồi ghi vào
``data/gpu/<rental_id>/provisioning.yaml`` và chuyển lên instance bằng ``onstart``.

Model chỉ được coi là đạt khi SHA-256 khớp manifest. Script cố định chờ ``/.provisioning_complete``, băm các
đích đã kiểm và quote, rồi ghi ``output/igniterra/model_sha256.txt`` bằng file tạm + ``mv``. Worker dùng
``ProvisioningReport.files`` nếu có, thiếu thì đọc file qua ``GET /view`` có TLS + Bearer đã kiểm. Hash thiếu
thì chờ tới ``provision_timeout_s``; hash lệch thì fail và destroy.

Studio không kết nối SSH: dùng lại kênh HTTPS đã xác thực tránh giữ thêm khóa và quản lý known_hosts. Cả hai
kênh đều nhận hash do instance tính, không chứng minh host đáng tin. Chế độ launch của provider vẫn có thể mở
cổng SSH; nếu bổ sung client SSH sau này phải ghim host key và giữ ``StrictHostKeyChecking=yes``. Quyết định,
nguồn schema và giới hạn được ghi ở 09 · s5.b4–s5.b5 và runbook Slice E.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

SHA1 = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
GITHUB_REPO = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(?:\.git)?")
HF_RESOLVE = re.compile(r"https://huggingface\.co/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/resolve/([0-9a-f]{40})/[A-Za-z0-9_./-]+")
COMFYUI_REPOS = ("https://github.com/Comfy-Org/ComfyUI", "https://github.com/comfyanonymous/ComfyUI")
SAFE_DEST = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
WORKSPACE = "/workspace"
COMFYUI_DIR = f"{WORKSPACE}/ComfyUI"
REMOTE_MANIFEST = "/etc/igniterra/provisioning.yaml"
REMOTE_CHECKSUM_SCRIPT = "/etc/igniterra/model_sha256.sh"
PROVISIONING_COMPLETE = "/.provisioning_complete"
CHECKSUM_SUBFOLDER = "igniterra"            # dưới ComfyUI/output/: đọc lại qua GET /view (type=output)
CHECKSUM_FILE = "model_sha256.txt"
CHECKSUM_MAX_BYTES = 256 * 1024
PORTAL_CONFIG = "localhost:1111:11111:/:Instance Portal|localhost:8188:18188:/:ComfyUI"
BOOT_COMMAND = "/opt/instance-tools/bin/entrypoint.sh --no-update-portal --no-update-vast"
MAX_RETRIES_LIMIT = 3
GB = 1_000_000_000


class GitRepo(BaseModel):
    url: str
    ref: str                            # full commit SHA
    dest: str                           # tương đối dưới ``custom_nodes/`` (custom node) hoặc ``ComfyUI``


class Download(BaseModel):
    url: str                            # https://huggingface.co/<org>/<repo>/resolve/<commit>/<file>
    dest: str                           # tương đối dưới ``ComfyUI/models/``
    size_bytes: int = Field(gt=0)
    sha256: str                         # giá trị LFS của Hugging Face
    license: str | None = None


class OnFailure(BaseModel):
    action: Literal["destroy"] = "destroy"
    max_retries: int = Field(default=2, ge=0, le=MAX_RETRIES_LIMIT)


class ProvisioningManifest(BaseModel):
    """Manifest khai báo; ``runtime_disk_gb`` là phần disk cho CUDA venv + ComfyUI ngoài model."""

    comfyui: GitRepo
    custom_nodes: list[GitRepo] = Field(default_factory=list)
    downloads: list[Download] = Field(default_factory=list)
    on_failure: OnFailure = Field(default_factory=OnFailure)
    runtime_disk_gb: float = Field(default=30, gt=0)

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def download_bytes(self) -> int:
        return sum(d.size_bytes for d in self.downloads)

    def required_disk_gb(self) -> float:
        """Disk tối thiểu: runtime + model + 10% dư cho file tạm khi tải."""
        return round(self.runtime_disk_gb + self.download_bytes() * 1.1 / GB, 2)

    def provisioner(self) -> dict:
        """Manifest provisioner với repo/model đã duyệt, lệnh cài cố định và service ComfyUI sau proxy."""
        install = "uv pip install --python /venv/main/bin/python -r requirements.txt"
        repos = [{"url": self.comfyui.url, "ref": self.comfyui.ref, "dest": COMFYUI_DIR,
                  "post_commands": [install]}]
        repos += [{"url": r.url, "ref": r.ref, "dest": f"{COMFYUI_DIR}/custom_nodes/{r.dest}",
                   "post_commands": [f"if test -f requirements.txt; then {install}; fi"]} for r in self.custom_nodes]
        return {
            "version": 1,
            "git_repos": repos,
            "downloads": [{"url": d.url, "dest": f"{COMFYUI_DIR}/models/{d.dest}"} for d in self.downloads],
            "services": [{"name": "comfyui", "portal_search_term": "ComfyUI", "venv": "/venv/main",
                          "workdir": COMFYUI_DIR, "skip_on_serverless": False,
                          "command": "python main.py --listen 127.0.0.1 --port 18188 --disable-auto-launch"}],
            "on_failure": {"action": self.on_failure.action, "max_retries": self.on_failure.max_retries},
        }


def build_manifest(comfyui_ref: str, *, custom_nodes: list[dict] | None = None, downloads: list[dict] | None = None,
                   max_retries: int = 2, runtime_disk_gb: float = 30,
                   comfyui_url: str = COMFYUI_REPOS[0]) -> ProvisioningManifest:
    """Dựng manifest từ commit ComfyUI, custom node và model đã chọn; ``validate`` trước khi lưu."""
    return ProvisioningManifest(comfyui=GitRepo(url=comfyui_url, ref=comfyui_ref, dest="ComfyUI"),
                                custom_nodes=[GitRepo(**n) for n in custom_nodes or []],
                                downloads=[Download(**d) for d in downloads or []],
                                on_failure=OnFailure(max_retries=max_retries), runtime_disk_gb=runtime_disk_gb)


def dest_ok(dest: str) -> bool:
    return bool(SAFE_DEST.fullmatch(dest)) and not any(part in (".", "..") for part in dest.split("/"))


def validate(manifest: ProvisioningManifest) -> list[str]:
    """Lý do manifest không được duyệt; rỗng là đạt. Không nhận gì ngoài repo GitHub ghim commit và HF ghim commit."""
    problems = []
    if manifest.comfyui.url.rstrip("/").removesuffix(".git") not in COMFYUI_REPOS:
        problems.append(f"ComfyUI phải từ {COMFYUI_REPOS[0]}: {manifest.comfyui.url}")
    for repo in [manifest.comfyui, *manifest.custom_nodes]:
        if not GITHUB_REPO.fullmatch(repo.url):
            problems.append(f"repo không phải https://github.com/<owner>/<repo>: {repo.url}")
        if not SHA1.fullmatch(repo.ref):
            problems.append(f"{repo.url}: ref phải là full commit SHA (40 hex), không phải tag/branch: {repo.ref!r}")
    names = [r.dest for r in manifest.custom_nodes]
    for name in names:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in (".", ".."):
            problems.append(f"tên thư mục custom node lạ: {name!r}")
    if len(set(names)) != len(names):
        problems.append("hai custom node cùng thư mục")
    dests = [d.dest for d in manifest.downloads]
    for download in manifest.downloads:
        parsed = urlparse(download.url)
        if not HF_RESOLVE.fullmatch(download.url) or parsed.query or parsed.fragment:
            problems.append(f"model phải tải từ huggingface.co/<org>/<repo>/resolve/<commit 40 hex>/<file>: {download.url}")
        if not dest_ok(download.dest) or "/" not in download.dest:
            problems.append(f"đích model phải là <thư mục>/<file> dưới models/: {download.dest!r}")
        if not SHA256.fullmatch(download.sha256):
            problems.append(f"{download.dest}: thiếu SHA-256 (64 hex) để kiểm sau khi tải")
    if len(set(dests)) != len(dests):
        problems.append("hai model cùng đích")
    if manifest.on_failure.action != "destroy":
        problems.append("on_failure.action phải là destroy")
    return problems


def to_yaml(data, indent: int = 0) -> str:
    """YAML khối cho dict/list/scalar; chuỗi viết dạng JSON (là YAML hợp lệ), không cần PyYAML."""
    pad = "  " * indent
    lines = []
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)) and value:
                lines.append(f"{pad}{key}:")
                lines.append(to_yaml(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(value)}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and item:
                first, *rest = to_yaml(item, indent + 1).split("\n")
                lines.append(f"{pad}- {first.lstrip()}")
                lines.extend(rest)
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:
        lines.append(f"{pad}{_scalar(data)}")
    return "\n".join(lines)


def _scalar(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        return "{}" if isinstance(value, dict) else "[]"
    return json.dumps(str(value), ensure_ascii=False)


def render(manifest: ProvisioningManifest, manifest_sha256: str) -> str:
    """Nội dung ``provisioning.yaml`` gửi lên instance."""
    header = (f"# igniterra provisioning manifest {manifest_sha256}\n"
              "# Sinh từ manifest đã duyệt trong SQLite (gpu_manifests); không sửa tay.\n")
    return header + to_yaml(manifest.provisioner()) + "\n"


def checksum_script(manifest: ProvisioningManifest) -> str | None:
    """Script sh tính SHA-256 các model của manifest sau khi provisioning xong (xem docstring đầu module).

    None khi manifest không có model. Chỉ nhận manifest đã qua ``validate`` (đích theo ``dest_ok``); vẫn
    ``shlex.quote`` từng đường dẫn, không ghép chuỗi thô vào lệnh.
    """
    if not manifest.downloads:
        return None
    for download in manifest.downloads:
        if not dest_ok(download.dest):
            raise ValueError(f"đích model lạ: {download.dest!r}")
    paths = " ".join(shlex.quote(d.dest) for d in manifest.downloads)
    out_dir = f"{COMFYUI_DIR}/output/{CHECKSUM_SUBFOLDER}"
    target = f"{out_dir}/{CHECKSUM_FILE}"
    return ("#!/bin/sh\n"
            "# igniterra: SHA-256 model sau provisioning (provisioner không kiểm checksum; 09 · s5.b2)\n"
            f"while [ ! -f {PROVISIONING_COMPLETE} ]; do sleep 15; done\n"
            f"mkdir -p {out_dir} || exit 1\n"
            f"cd {COMFYUI_DIR}/models || exit 1\n"
            f"sha256sum -- {paths} > {target}.part\n"
            f"mv -f {target}.part {target}\n")


def parse_sha256sum(text: str) -> dict[str, str]:
    """Dòng ``<64 hex>  <đường dẫn>`` (hoặc ``*<đường dẫn>`` chế độ nhị phân) của ``sha256sum`` → {đường dẫn: hash}.

    Dòng lạ bị bỏ qua; đường dẫn không qua ``dest_ok`` cũng bỏ (không khớp được model nào trong manifest).
    """
    files = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line.strip())
        if match and dest_ok(match.group(2)):
            files[match.group(2)] = match.group(1).lower()
    return files


# ------------------------------------------------------------ báo cáo tiến độ từ instance

PROVISION_STEPS = ("runtime_setup", "custom_nodes", "models")   # bước do provisioner chạy trên instance
NON_RETRYABLE = ("disk_insufficient", "dependency_conflict")     # thử lại cũng không qua: dừng sớm, destroy


class StepReport(BaseModel):
    status: Literal["pending", "running", "done", "failed"] = "pending"
    attempt: int = 0
    error: str | None = None            # dòng lỗi/log tail; sẽ được che bí mật trước khi ghi
    error_code: str | None = None
    final: bool = False                 # provisioner đã bỏ cuộc (hết max_retries)
    bytes_done: int | None = None
    bytes_total: int | None = None


class ProvisioningReport(BaseModel):
    """Trạng thái provisioning đọc qua Instance Portal (có xác thực).

    ``complete`` nghĩa là ``/.provisioning_complete`` đã có. ``files`` là SHA-256 instance tính cho model đã tải
    (nếu có); thiếu thì đọc file do ``checksum_script`` ghi qua ComfyUI. Chưa có hash thì chưa ``ready``.
    """

    complete: bool = False
    steps: dict[str, StepReport] = Field(default_factory=dict)
    disk_free_gb: float | None = None
    files: dict[str, str] = Field(default_factory=dict)
    log_ref: str | None = None


ERROR_PATTERNS = (
    ("disk_insufficient", re.compile(r"No space left on device|\[Errno 28\]|ENOSPC|Disk quota exceeded|"
                                     r"not enough (?:free )?disk", re.IGNORECASE)),
    ("dependency_conflict", re.compile(r"ResolutionImpossible|conflicting dependencies|dependency conflict|"
                                       r"has requirement .+ but you have|but you have .+ which is incompatible",
                                       re.IGNORECASE)),
)
STEP_ERROR = {"runtime_setup": "pip_failed", "custom_nodes": "custom_node_failed", "models": "model_download_failed"}


def classify(step_key: str, report: StepReport) -> str:
    """Mã lỗi ổn định cho một bước hỏng: disk, xung đột dependency (G07), còn lại theo bước (G05)."""
    if report.error_code in (*NON_RETRYABLE, *STEP_ERROR.values()):
        return report.error_code
    text = report.error or ""
    for code, pattern in ERROR_PATTERNS:
        if pattern.search(text):
            return code
    return STEP_ERROR.get(step_key, "provision_failed")
