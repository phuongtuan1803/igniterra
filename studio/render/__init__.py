"""Adapter renderer gọi vendor/video_project.py (bản copy MPT 82bb4a7, không sửa)."""

from __future__ import annotations

from pathlib import Path

from studio import media
from studio.providers.base import QualityFail
from studio.vendor import video_project


class VideoProjectRenderer:
    name = "video_project"
    version = video_project.RENDERER_VERSION

    def __init__(self, config: dict):
        render = config["render"]
        self.settings = {"width": render["width"], "height": render["height"], "fps": render["fps"]}

    def render(self, project_dir: Path, project_id: str, scenes: list[dict]) -> dict:
        """scenes: [{id, footage, audio, narration}] với đường dẫn tuyệt đối. Trả export và thời lượng từng scene."""
        project = video_project.VideoProject(project_dir)
        stages: list[tuple[str, bool]] = []
        try:
            spec = project.create_revision({
                "project_id": project_id,
                "scenes": [{"id": s["id"], "footage": s["footage"], "audio": s["audio"], "narration": s["narration"]}
                           for s in scenes],
                "settings": self.settings,
            }, relative_to="/")
            status = project.render(spec["revision_id"], on_stage=lambda stage, reused: stages.append((stage.key, reused)))
        except video_project.ProjectError as exc:
            raise QualityFail(f"video_project: {exc}") from None
        export = Path(status["export"]["path"])
        durations = {}
        for scene in scenes:
            stage = status["stages"][f"{scene['id']}:scene"]
            clip = project.directory / "artifacts" / stage["fingerprint"] / f"output-{stage['sha256']}.mp4"
            durations[scene["id"]] = media.probe(clip)["duration"]
        return {
            "revision_id": spec["revision_id"],
            "export": export,
            "scene_durations": durations,
            "reused": [key for key, reused in stages if reused],
            "built": [key for key, reused in stages if not reused],
        }
