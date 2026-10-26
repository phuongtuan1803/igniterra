from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from studio import config as studio_config
from studio import pipeline
from studio.contracts import Brief
from studio.gates import Actions
from studio.providers import fake
from studio.store import Store
from studio.taskdir import TaskDir

ROOT = Path(__file__).resolve().parent.parent

FAKE_TOML = """
data_dir = "{data}"
mpt_home = "{data}/mpt"
[adapters]
llm = "fake"
image = "fake"
tts = "fake"
subtitles = "fake"
video = "none"
renderer = "video_project"
[concurrency]
image = 1
tts = 2
animate = 2
[retry]
max_attempts = 3
backoff_s = 0
[gates]
keyframes = true
[render]
width = 1080
height = 1920
fps = 30
"""

NARRATION = {   # 168 từ ≈ 57,9 giây ở 2,9 từ/giây (sát trần trên của khoảng mục tiêu)
    "s01": "Con người đã biến mất từ lâu, nhưng mỗi sáng con robot nhỏ vẫn đi qua thành phố đổ nát để tìm nước cho chậu đất của nó.",
    "s02": "Nó băng qua những con phố vắng, leo lên những tòa nhà sụp đổ, gom từng giọt sương đọng trên tấm kính vỡ để mang về chậu.",
    "s03": "Nhiều năm trôi qua, chậu đất vẫn im lìm, không một chồi non, nhưng robot vẫn tưới đều đặn như thể ai đó đang chờ nó trở về.",
    "s04": "Một đêm lạnh buốt, đèn báo pin chuyển sang màu đỏ, và robot biết nếu không quay về trạm sạc thì nó sẽ không còn tỉnh lại nữa.",
    "s05": "Nó ở lại, ôm chặt chậu đất bằng đôi tay kim loại, truyền chút hơi ấm cuối cùng vào lòng đất lạnh trong khi bóng tối dần buông xuống.",
    "s06": "Khi bình minh tới, đôi mắt máy đã tắt hẳn, còn giữa chậu đất ẩm, một mầm xanh nhỏ xíu vừa nhú lên đón ánh nắng đầu tiên.",
}


def fake_plan_data(early: bool = False, words: dict | None = None) -> dict:
    texts = words or NARRATION
    shows = {"s01": ["đô thị bỏ hoang"], "s02": [], "s03": ["tưới chậu đất"], "s04": [], "s05": [], "s06": ["mầm cây"]}
    if early:
        shows["s01"] = ["đô thị bỏ hoang", "mầm cây"]
    moves = ["push_in", "pan_left", "static", "pull_out", "push_in", "pan_right"]
    return {
        "title": "Người làm vườn cuối cùng",
        "cast": [{"key": "robot", "description": "small rusty maintenance robot with one round lens eye"}],
        "scenes": [
            {"id": sid, "narration": texts[sid], "visual": f"Robot scene {sid}", "characters": ["robot"],
             "shot": {"size": "wide", "angle": "eye_level", "movement": moves[i % len(moves)]}, "shows": shows[sid]}
            for i, sid in enumerate(texts)
        ],
    }


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.toml = tmp / "studio.toml"
        self.toml.write_text(FAKE_TOML.format(data=tmp / "data"), encoding="utf-8")
        self.config = studio_config.load(root=ROOT, path=self.toml)
        self.store = Store(Path(self.config["data_dir"]) / "studio.db")
        self.actions = Actions(self.store, self.config)
        self.sleeps: list[float] = []

    def brief(self, name: str = "brief-02") -> Brief:
        return Brief.model_validate_json((ROOT / "briefs" / f"{name}.json").read_text(encoding="utf-8"))

    def new_task(self, overrides: dict | None = None, name: str = "brief-02") -> str:
        return self.actions.create_task(self.brief(name), None, overrides or {})

    def run(self, task_id: str) -> str:
        assert self.store.claim_one(task_id), self.store.task(task_id)["status"]
        return pipeline.run_task(self.store, self.config, task_id, sleep=self.sleeps.append)

    def task(self, task_id: str) -> dict:
        return self.store.task(task_id)

    def td(self, task_id: str) -> TaskDir:
        return TaskDir(Path(self.config["data_dir"]), task_id)

    def worker_subprocess(self, task_id: str, extra_env: dict) -> int:
        env = {**os.environ, "STUDIO_CONFIG": str(self.toml), "STUDIO_ROOT": str(ROOT), **extra_env}
        script = (
            "import json,sys; from studio.providers import fake; "
            f"fake.SCRIPT.update(plan=json.loads({json.dumps(json.dumps(fake.SCRIPT.get('plan')))}), "
            f"video_polls_before_ready={fake.SCRIPT.get('video_polls_before_ready', 2)}); "
            f"from studio.cli import main; sys.exit(main(['worker','--once','--skip-preflight','--task','{task_id}']))"
        )
        return subprocess.run([sys.executable, "-c", script], env=env, cwd=ROOT, capture_output=True, text=True).returncode

    def review_all_pass(self, task_id: str, minutes: float = 5.0) -> None:
        from studio.contracts import Review

        review = Review(checklist={f"C0{i}": {"result": "pass"} for i in range(1, 10)}, active_minutes=minutes, reviewer="test")
        self.actions.review(task_id, review)


@pytest.fixture
def env(tmp_path):
    fake.reset()
    fake.SCRIPT["plan"] = fake_plan_data()
    yield Env(tmp_path)
    fake.reset()


def steps_run(store: Store, task_id: str, since_id: int = 0) -> list[tuple[str, str, str]]:
    return [(r["step"], r["scene"], r["status"]) for r in store.runs(task_id) if r["id"] > since_id]


def last_run_id(store: Store, task_id: str) -> int:
    runs = store.runs(task_id)
    return runs[-1]["id"] if runs else 0


def read_manifest(td: TaskDir, rev: str) -> dict:
    return json.loads((td.root / "renders" / rev / "manifest.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------ GPU (Gate 4): manifest provisioning đã duyệt

COMFYUI_REF = "0123456789abcdef0123456789abcdef01234567"     # commit giả, đủ 40 hex


def gpu_manifest(**kwargs):
    from studio.gpu.provisioning import build_manifest

    return build_manifest(kwargs.pop("comfyui_ref", COMFYUI_REF), **kwargs)


def approve_gpu_manifest(store: Store, manifest=None) -> str:
    """Manifest provisioning đã duyệt trong SQLite (lát C: không có manifest duyệt thì không thuê được)."""
    manifest = manifest or gpu_manifest()
    sha = manifest.sha256()
    store.add_manifest(sha, manifest.canonical_json())
    store.set_manifest_status(sha, "approved", expect="pending_review")
    return sha
