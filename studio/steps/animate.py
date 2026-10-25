"""Bước animate (theo scene, local, không trả phí): ảnh selected → clip MP4 theo khóa movement.

Renderer video_project chỉ nhận footage là video và không có chuyển động camera (05 · ghi chú renderer).
"""

from __future__ import annotations

import math

from studio import media
from studio.contracts import AssetCandidate
from studio.providers.base import Fatal
from studio.steps.base import Ctx, Item, Result
from studio.taskdir import commit_file

ANIMATE_VERSION = "kenburns-v1"
ZOOM = 0.12          # push_in 1.00 → 1.12 (04 · Khóa movement)
PAD_S = 0.5          # clip dài hơn giọng một chút; renderer cắt theo thời lượng giọng
PROVIDER = "local:kenburns"


def applies(ctx: Ctx, scene: str) -> bool:
    if ctx.plan.scene(scene).mode != "still":
        selected = ctx.td.selected(scene, "clip")
        if selected and selected.provider != PROVIDER:
            return False          # clip motion đạt thì dùng clip; lỗi thì vẫn dựng từ ảnh
    selected = ctx.td.selected(scene, "clip")
    return not (selected and selected.provider == "user")


def _settings(ctx: Ctx) -> dict:
    render = ctx.config["render"]
    return {"width": render["width"], "height": render["height"], "fps": render["fps"]}


def inputs(ctx: Ctx, scene: str) -> dict:
    image = ctx.td.selected(scene, "image")
    voice = ctx.td.selected(scene, "voice")
    return {
        "image": [image.id, image.sha256] if image else None,
        "voice": [voice.id, voice.duration_s] if voice else None,
        "movement": ctx.plan.scene(scene).shot.movement,
        "settings": _settings(ctx),
        "version": ANIMATE_VERSION,
    }


def zoompan_filter(movement: str, frames: int, width: int, height: int, fps: int) -> str:
    last = max(frames - 1, 1)
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if movement == "push_in":
        zoom, x, y = f"1+{ZOOM}*on/{last}", center_x, center_y
    elif movement == "pull_out":
        zoom, x, y = f"{1 + ZOOM}-{ZOOM}*on/{last}", center_x, center_y
    elif movement == "pan_left":
        zoom, x, y = f"{1 + ZOOM}", f"(iw-iw/zoom)*(1-on/{last})", center_y
    elif movement == "pan_right":
        zoom, x, y = f"{1 + ZOOM}", f"(iw-iw/zoom)*on/{last}", center_y
    else:
        zoom, x, y = "1", "0", "0"
    # Phóng to gấp đôi trước zoompan để chuyển động không giật theo pixel nguyên.
    big_w, big_h = width * 2, height * 2
    return (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},setsar=1,"
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps},format=yuv420p"
    )


def run(ctx: Ctx, item: Item) -> Result:
    image = ctx.td.selected(item.scene, "image")
    voice = ctx.td.selected(item.scene, "voice")
    if image is None or voice is None:
        raise Fatal(f"{item.scene}: thiếu ảnh hoặc giọng selected")
    settings = _settings(ctx)
    duration = float(voice.duration_s) + PAD_S
    frames = math.ceil(duration * settings["fps"])
    movement = ctx.plan.scene(item.scene).shot.movement
    tmp = ctx.td.tmp_path("clips", ".mp4")
    try:
        media.run([
            media.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(ctx.td.abs(image.path)),
            "-vf", zoompan_filter(movement, frames, settings["width"], settings["height"], settings["fps"]),
            "-frames:v", str(frames), "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(tmp),
        ])
        info = media.check_clip(tmp, settings["width"], settings["height"], float(voice.duration_s))
        final = ctx.td.root / "clips" / f"clip_{item.scene}_a{item.attempt}.mp4"
        digest = commit_file(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    candidate = AssetCandidate(
        id=ctx.td.candidate_id("clip", item.scene, item.attempt),
        kind="clip",
        path=ctx.td.rel(final),
        provider=PROVIDER,
        model=f"ffmpeg zoompan {movement}",
        plan_rev=ctx.plan_rev,
        ai_generated=image.ai_generated,
        source=image.source,
        rights=image.rights,
        sha256=digest,
        duration_s=round(info["duration"], 3),
        width=settings["width"],
        height=settings["height"],
        from_ids=[image.id, voice.id],
    )
    return Result(output=final, candidate=candidate)
