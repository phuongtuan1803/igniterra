"""Thư mục task: brief, ScenePlan revision, assets.json, đường dẫn ứng viên (04 · Thư mục task).

Mọi file chỉ được thêm. Riêng assets.json được thay nguyên file qua ``write_assets``:
flock theo task, đọc–sửa–ghi file tạm, fsync, đổi tên.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from studio.contracts import AssetCandidate, Brief, ScenePlan

KIND_PREFIX = {"image": "img", "voice": "vo", "clip": "clip", "music": "mus"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def commit_file(tmp: Path, final: Path) -> str:
    """fsync file tạm rồi đổi tên thành tên cuối; không ghi đè file đã có. Trả SHA-256."""
    if final.exists():
        raise FileExistsError(f"không ghi đè {final}")
    with tmp.open("rb") as handle:
        os.fsync(handle.fileno())
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, final)
    fsync_dir(final.parent)
    return sha256(final)


def write_json_new(path: Path, value: object) -> None:
    """Ghi file JSON mới (không ghi đè) qua file tạm."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    commit_file(Path(name), path)


class TaskDir:
    def __init__(self, data_dir: Path, task_id: str):
        self.task_id = task_id
        self.root = Path(data_dir) / "tasks" / task_id
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ đường dẫn

    @property
    def costs_path(self) -> Path:
        return self.root / "costs.jsonl"

    @property
    def assets_path(self) -> Path:
        return self.root / "assets.json"

    @property
    def project_dir(self) -> Path:
        return self.root / "project"

    def rel(self, path: Path) -> str:
        return str(Path(path).resolve().relative_to(self.root.resolve()))

    def abs(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if os.path.commonpath([path, self.root.resolve()]) != str(self.root.resolve()):
            raise ValueError(f"đường dẫn ngoài thư mục task: {rel}")
        return path

    def tmp_path(self, folder: str, suffix: str) -> Path:
        directory = self.root / folder
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".tmp-", suffix=suffix, dir=directory)
        os.close(fd)
        return Path(name)

    # ------------------------------------------------------------ brief, plan

    def write_brief(self, brief: Brief) -> None:
        write_json_new(self.root / "brief.json", brief.model_dump(mode="json"))

    def brief(self) -> Brief:
        return Brief.model_validate_json((self.root / "brief.json").read_text(encoding="utf-8"))

    def plan_path(self, revision: int) -> Path:
        return self.root / "plan" / f"rev_{revision:03d}.json"

    def plan_revisions(self) -> list[int]:
        folder = self.root / "plan"
        if not folder.is_dir():
            return []
        return sorted(int(m.group(1)) for p in folder.iterdir() if (m := re.fullmatch(r"rev_(\d{3})\.json", p.name)))

    def write_plan(self, plan: ScenePlan) -> int:
        revisions = self.plan_revisions()
        plan.revision = (revisions[-1] + 1) if revisions else 1
        write_json_new(self.plan_path(plan.revision), plan.model_dump(mode="json"))
        return plan.revision

    def plan(self, revision: int) -> ScenePlan:
        return ScenePlan.model_validate_json(self.plan_path(revision).read_text(encoding="utf-8"))

    # ------------------------------------------------------------ assets.json

    @contextmanager
    def _assets_lock(self) -> Iterator[None]:
        with (self.root / ".assets.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def assets(self) -> dict:
        if not self.assets_path.is_file():
            return {}
        return json.loads(self.assets_path.read_text(encoding="utf-8"))

    def write_assets(self, change: Callable[[dict], None]) -> dict:
        """Hàm duy nhất ghi assets.json."""
        with self._assets_lock():
            assets = self.assets()
            change(assets)
            fd, name = tempfile.mkstemp(prefix=".assets-", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(assets, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, self.assets_path)
                fsync_dir(self.root)
            finally:
                Path(name).unlink(missing_ok=True)
            return assets

    def add_candidate(self, scene: str, candidate: AssetCandidate, select: bool = True) -> None:
        candidate.created_at = candidate.created_at or datetime.now().isoformat(timespec="seconds")

        def change(assets: dict) -> None:
            slot = assets.setdefault(scene, {}).setdefault(candidate.kind, {"selected": None, "candidates": []})
            if any(c["id"] == candidate.id for c in slot["candidates"]):
                raise ValueError(f"ứng viên {candidate.id} đã có")
            slot["candidates"].append(candidate.model_dump(mode="json", exclude_none=True, exclude_defaults=False))
            if select or slot["selected"] is None:
                slot["selected"] = candidate.id

        self.write_assets(change)

    def select(self, scene: str, candidate_id: str) -> str:
        """Đổi selected; trả loại asset của ứng viên."""
        found: dict = {}

        def change(assets: dict) -> None:
            for kind, slot in assets.get(scene, {}).items():
                if any(c["id"] == candidate_id for c in slot["candidates"]):
                    slot["selected"] = candidate_id
                    found["kind"] = kind
                    return
            raise KeyError(f"{scene} không có ứng viên {candidate_id}")

        self.write_assets(change)
        return found["kind"]

    def selected(self, scene: str, kind: str) -> AssetCandidate | None:
        slot = self.assets().get(scene, {}).get(kind)
        if not slot or not slot["selected"]:
            return None
        for candidate in slot["candidates"]:
            if candidate["id"] == slot["selected"]:
                return AssetCandidate.model_validate(candidate)
        return None

    def candidates(self, scene: str, kind: str) -> list[AssetCandidate]:
        slot = self.assets().get(scene, {}).get(kind) or {"candidates": []}
        return [AssetCandidate.model_validate(c) for c in slot["candidates"]]

    @staticmethod
    def candidate_id(kind: str, scene: str, attempt: int) -> str:
        return f"{KIND_PREFIX[kind]}_{scene}_a{attempt}"

    # ------------------------------------------------------------ renders

    def render_revisions(self) -> list[str]:
        folder = self.root / "renders"
        if not folder.is_dir():
            return []
        return sorted(p.name for p in folder.iterdir() if re.fullmatch(r"r_\d{3}", p.name) and (p / "manifest.json").is_file())

    def new_render_dir(self) -> tuple[str, Path]:
        folder = self.root / "renders"
        folder.mkdir(parents=True, exist_ok=True)
        existing = [int(p.name[2:]) for p in folder.iterdir() if re.fullmatch(r"r_\d{3}", p.name)]
        name = f"r_{(max(existing) + 1) if existing else 1:03d}"
        path = folder / name
        path.mkdir()
        return name, path
