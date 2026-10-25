"""Bước render: clip + giọng selected → video_project → burn-in phụ đề → renders/r_NNN/ (NFR-03)."""

from __future__ import annotations

import json
from pathlib import Path

from studio import media
from studio.steps import subtitles
from studio.steps.base import Ctx, Item, Result, WaitFor
from studio.taskdir import commit_file, write_json_new

FONTS_DIR = Path(__file__).resolve().parent.parent / "vendor" / "fonts"
BURN_VERSION = "burn-v1"


def applies(ctx: Ctx, scene: str) -> bool:
    return True


def _selected(ctx: Ctx) -> dict:
    scenes = {}
    for scene_id in ctx.scene_ids():
        clip = ctx.td.selected(scene_id, "clip")
        voice = ctx.td.selected(scene_id, "voice")
        scenes[scene_id] = {"clip": [clip.id, clip.sha256] if clip else None, "voice": [voice.id, voice.sha256] if voice else None}
    return scenes


def inputs(ctx: Ctx, scene: str, subtitles_sha: str | None = None) -> dict:
    return {
        "scenes": _selected(ctx),
        "subtitles": subtitles_sha,
        "render": ctx.config["render"],
        "renderer": ctx.adapter_id("renderer"),
        "version": BURN_VERSION,
    }


def run(ctx: Ctx, item: Item, subtitles_path: Path) -> Result:
    renderer = ctx.adapter("renderer")
    render_cfg = ctx.config["render"]
    scenes = []
    for scene_id in ctx.scene_ids():
        clip = ctx.td.selected(scene_id, "clip")
        voice = ctx.td.selected(scene_id, "voice")
        scenes.append({"id": scene_id, "footage": str(ctx.td.abs(clip.path)), "audio": str(ctx.td.abs(voice.path)),
                       "narration": ctx.plan.scene(scene_id).narration, "clip_id": clip.id, "voice_id": voice.id})
    out = renderer.render(ctx.td.project_dir, ctx.task_id, scenes)

    offsets, position = {}, 0.0
    for scene in scenes:
        offsets[scene["id"]] = position
        position += out["scene_durations"][scene["id"]]
    ass = subtitles.build_ass(subtitles.load(subtitles_path), offsets, render_cfg)

    render_rev, folder = ctx.td.new_render_dir()
    ass_path = folder / "full.ass"
    tmp_ass = folder / ".full.ass.tmp"
    tmp_ass.write_text(ass, encoding="utf-8")
    commit_file(tmp_ass, ass_path)
    tmp_mp4 = folder / ".final.tmp.mp4"
    try:
        media.run([
            media.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(out["export"]),
            "-vf", f"ass=filename={ass_path.name}:fontsdir={FONTS_DIR}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart", str(tmp_mp4.name),
        ], cwd=folder)
        info = media.check_render(tmp_mp4, render_cfg["width"], render_cfg["height"])
        final = folder / "final_9x16.mp4"
        digest = commit_file(tmp_mp4, final)
    finally:
        tmp_mp4.unlink(missing_ok=True)
    manifest = {
        "render_rev": render_rev,
        "task_id": ctx.task_id,
        "plan_rev": ctx.plan_rev,
        "scenes": [{"id": s["id"], "clip": s["clip_id"], "voice": s["voice_id"], "start_s": round(offsets[s["id"]], 3),
                    "duration_s": round(out["scene_durations"][s["id"]], 3)} for s in scenes],
        "video_project_revision": out["revision_id"],
        "stages_reused": out["reused"],
        "stages_built": out["built"],
        "duration_s": round(info["duration"], 3),
        "width": info["width"],
        "height": info["height"],
        "final_sha256": digest,
        "subtitles": ctx.td.rel(subtitles_path),
    }
    write_json_new(folder / "manifest.json", manifest)
    if info["duration"] > ctx.brief.max_s:
        raise WaitFor("duration_exceeded", f"{render_rev}: {info['duration']:.2f}s > max_s {ctx.brief.max_s:g}s")
    return Result(output=final)


def latest(ctx: Ctx) -> tuple[str, dict] | None:
    revisions = ctx.td.render_revisions()
    if not revisions:
        return None
    rev = revisions[-1]
    return rev, json.loads((ctx.td.root / "renders" / rev / "manifest.json").read_text(encoding="utf-8"))
