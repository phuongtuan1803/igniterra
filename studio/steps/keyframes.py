"""Bước keyframes (theo scene): prompt ghép xác định + ảnh tham chiếu → ứng viên ảnh."""

from __future__ import annotations

from pathlib import Path

from studio import media, prompt
from studio.steps.base import Ctx, Item, Result
from studio.taskdir import commit_file, sha256


SAFE_HINT = "calm, non-graphic, safe-for-all-audiences depiction; no injury, no distress"


def applies(ctx: Ctx, scene: str) -> bool:
    return True


def refs(ctx: Ctx, scene: str) -> list[Path]:
    """Ảnh tham chiếu của nhân vật trong scene; model chỉ nhận 1 ảnh thì dùng character sheet."""
    if ctx.series_dir is None:
        return []
    max_refs = int(ctx.config["omniroute"].get("image_max_refs") or 1)
    paths: list[Path] = []
    for key in ctx.plan.scene(scene).characters:
        character = ctx.bible.characters.get(key)
        if not character:
            continue
        if max_refs == 1 and character.sheet:
            paths.append(ctx.series_dir / character.sheet)
        else:
            paths.extend(ctx.series_dir / ref for ref in character.refs)
    return paths[:max_refs] if max_refs else paths


def inputs(ctx: Ctx, scene: str) -> dict:
    return {
        "prompt": prompt.keyframe_prompt(ctx.plan.scene(scene), ctx.plan, ctx.bible),
        "refs": [sha256(path) for path in refs(ctx, scene)],
        "image": ctx.adapter_id("image"),
        "size": "1080x1920",
        "version": prompt.KEYFRAME_PROMPT_VERSION,
    }


def run(ctx: Ctx, item: Item) -> Result:
    image = ctx.adapter("image")
    # Lý do kỹ thuật (kích thước, tỉ lệ) không giúp model ảnh; chỉ ghi chú của người và gợi ý khi bị chặn nội dung.
    notes = [item.note] if item.note else []
    if any(reason.startswith("refused:") for reason in item.feedback):
        notes.append(SAFE_HINT)
    note = "; ".join(notes) or None
    text = prompt.keyframe_prompt(ctx.plan.scene(item.scene), ctx.plan, ctx.bible, note)
    tmp = ctx.td.tmp_path("keyframes", "")
    try:
        with ctx.call(item, image):
            candidate = image.generate(text, refs(ctx, item.scene), "1080x1920", tmp)
        written = ctx.td.abs(candidate.path) if not Path(candidate.path).is_absolute() else Path(candidate.path)
        width, height = media.check_keyframe(written)
        final = ctx.td.root / "keyframes" / f"{item.scene}_a{item.attempt}{written.suffix or '.png'}"
        digest = commit_file(written, final)
    finally:
        tmp.unlink(missing_ok=True)
        for leftover in tmp.parent.glob(tmp.name + ".*"):
            leftover.unlink(missing_ok=True)
    candidate = candidate.model_copy(update={
        "id": ctx.td.candidate_id("image", item.scene, item.attempt),
        "kind": "image",
        "path": ctx.td.rel(final),
        "plan_rev": ctx.plan_rev,
        "sha256": digest,
        "width": width,
        "height": height,
    })
    return Result(output=final, candidate=candidate)
