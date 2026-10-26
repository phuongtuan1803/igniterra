"""Manifest dependency của một revision workflow (09 · s6 "Duyệt").

Liệt kê model (URL Hugging Face ghim commit, đích dưới ``models/``, dung lượng, SHA-256, license) và custom node
(repo GitHub, commit đầy đủ, dòng ``requirements.txt`` đã đọc). Cùng quy tắc ghim nguồn với manifest provisioning
(``studio.gpu.provisioning``) và cùng cách lưu: khóa là SHA-256 của JSON chuẩn hóa, duyệt một lần, kiểm lại nội
dung và SHA-256 mỗi lần dùng (không phải cờ "đã tin" một lần).

Manifest này **không** tự sinh hay gộp vào manifest provisioning của GPU, và không cài gì lên rental: nó là bản
khai đã duyệt (audit, license) của thứ workflow cần. Một rental ``ready`` có chạy được workflow hay không do
``GET /object_info`` của chính runtime đó quyết định ngay trước khi submit (G07, nửa run).
"""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from studio.gpu.provisioning import GITHUB_REPO, HF_RESOLVE, SHA1, SHA256, dest_ok

# Dòng requirements được nhận: tên gói (extras) + ràng buộc phiên bản + marker. Không URL, không ``-e``/``--index-url``.
REQUIREMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?"
                         r"(?:\s*(?:===|==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]+(?:\s*,\s*(?:===|==|>=|<=|~=|!=|<|>)"
                         r"\s*[A-Za-z0-9.*+!_-]+)*)?(?:\s*;[^#]*)?")
# License dùng thương mại được không cần xác nhận thêm; license khác cần ``accept_licenses`` khi duyệt.
COMMERCIAL_OK = ("apache-2.0", "mit", "bsd-2-clause", "bsd-3-clause", "cc0-1.0", "cc-by-4.0")


class ModelDep(BaseModel):
    url: str                            # https://huggingface.co/<org>/<repo>/resolve/<commit>/<file>
    dest: str                           # <thư mục>/<file> dưới ComfyUI/models/
    size_bytes: int = Field(gt=0)
    sha256: str
    license: str = Field(min_length=1)


class NodeDep(BaseModel):
    repo: str                           # https://github.com/<owner>/<repo>
    commit: str                         # full commit SHA
    dest: str                           # thư mục dưới custom_nodes/
    requirements: list[str] = Field(default_factory=list)   # dòng requirements.txt ở commit đó (rỗng: không có)


class WorkflowManifest(BaseModel):
    models: list[ModelDep] = Field(default_factory=list)
    custom_nodes: list[NodeDep] = Field(default_factory=list)

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


def validate(manifest: WorkflowManifest) -> list[str]:
    """Lý do manifest không được nhận; rỗng là đạt."""
    problems = []
    for node in manifest.custom_nodes:
        if not GITHUB_REPO.fullmatch(node.repo):
            problems.append(f"repo không phải https://github.com/<owner>/<repo>: {node.repo}")
        if not SHA1.fullmatch(node.commit):
            problems.append(f"{node.repo}: commit phải là full SHA (40 hex), không phải tag/branch: {node.commit!r}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", node.dest) or node.dest in (".", ".."):
            problems.append(f"tên thư mục custom node lạ: {node.dest!r}")
        for line in node.requirements:
            if not REQUIREMENT.fullmatch(line.strip()) or "://" in line:
                problems.append(f"{node.repo}: dòng requirements không nhận (chỉ tên gói + phiên bản): {line!r}")
    names = [n.dest for n in manifest.custom_nodes]
    if len(set(names)) != len(names):
        problems.append("hai custom node cùng thư mục")
    for model in manifest.models:
        parsed = urlparse(model.url)
        if not HF_RESOLVE.fullmatch(model.url) or parsed.query or parsed.fragment:
            problems.append(f"model phải tải từ huggingface.co/<org>/<repo>/resolve/<commit 40 hex>/<file>: {model.url}")
        if not dest_ok(model.dest) or "/" not in model.dest:
            problems.append(f"đích model phải là <thư mục>/<file> dưới models/: {model.dest!r}")
        if not SHA256.fullmatch(model.sha256):
            problems.append(f"{model.dest}: thiếu SHA-256 (64 hex)")
    dests = [m.dest for m in manifest.models]
    if len(set(dests)) != len(dests):
        problems.append("hai model cùng đích")
    return problems


def license_warnings(manifest: WorkflowManifest) -> list[str]:
    """Model có license ngoài danh sách dùng thương mại được: duyệt cần xác nhận riêng (``accept_licenses``)."""
    return [f"{m.dest}: license {m.license!r} chưa chắc dùng thương mại được"
            for m in manifest.models if m.license.strip().lower() not in COMMERCIAL_OK]
