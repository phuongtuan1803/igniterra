"""Bước voice (theo scene): narration (đã thay lexicon) → giọng + mốc từng từ."""

from __future__ import annotations

from pathlib import Path

from studio import media, prompt
from studio.contracts import AssetCandidate
from studio.providers.base import QualityFail, Retryable
from studio.steps.base import Ctx, Item, Result
from studio.taskdir import commit_file


def applies(ctx: Ctx, scene: str) -> bool:
    return True


def voice_spec(ctx: Ctx):
    spec = ctx.bible.narrator
    override = ctx.config.get("voice") or {}
    return spec.model_copy(update={k: v for k, v in override.items() if k in ("name", "rate", "wps")})


def inputs(ctx: Ctx, scene: str) -> dict:
    return {
        "text": prompt.apply_lexicon(ctx.plan.scene(scene).narration, ctx.bible),
        "voice": voice_spec(ctx).model_dump(mode="json", exclude={"wps"}),
        "tts": ctx.adapter_id("tts"),
    }


def run(ctx: Ctx, item: Item) -> Result:
    tts = ctx.adapter("tts")
    text = prompt.apply_lexicon(ctx.plan.scene(item.scene).narration, ctx.bible)
    tmp = ctx.td.tmp_path("voice", ".mp3")
    try:
        with ctx.call(item, tts):
            track = tts.synthesize(item.scene, text, voice_spec(ctx), tmp)
        try:
            duration = media.check_audio(Path(track.path))
        except QualityFail as exc:
            raise Retryable(exc.reason) from None   # 03 · Hậu điều kiện: voice trượt → Retryable
        final = ctx.td.root / "voice" / f"vo_{item.scene}_a{item.attempt}{Path(track.path).suffix}"
        digest = commit_file(Path(track.path), final)
    finally:
        tmp.unlink(missing_ok=True)
    candidate = AssetCandidate(
        id=ctx.td.candidate_id("voice", item.scene, item.attempt),
        kind="voice",
        path=ctx.td.rel(final),
        provider=track.provider,
        model=track.model,
        plan_rev=ctx.plan_rev,
        sha256=digest,
        duration_s=round(duration, 3),
        words=track.words,
    )
    return Result(output=final, candidate=candidate)
