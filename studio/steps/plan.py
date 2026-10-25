"""Bước plan: LLMProvider → ScenePlan rev 1, kiểm với Brief (FR-02)."""

from __future__ import annotations

from studio import prompt
from studio.providers.base import QualityFail
from studio.steps.base import Ctx, Item, Result


def applies(ctx: Ctx, scene: str) -> bool:
    return ctx.plan_rev == 0          # chỉ tạo rev 1; sửa sau đó là revision của người


def inputs(ctx: Ctx, scene: str) -> dict:
    return {
        "brief": ctx.brief.model_dump(mode="json"),
        "bible": ctx.bible.model_dump(mode="json", include={"characters", "voices", "camera"}),
        "llm": ctx.adapter_id("llm"),
        "prompt": prompt.PLAN_PROMPT_VERSION,
    }


def run(ctx: Ctx, item: Item) -> Result:
    llm = ctx.adapter("llm")
    instructions = prompt.plan_instructions(ctx.brief, ctx.bible, item.feedback)
    schema = prompt.plan_schema(ctx.brief, ctx.bible)
    with ctx.call(item, llm):
        data = llm.generate_json(instructions, schema, "scene_plan")
    try:
        plan = prompt.plan_from_llm(data, ctx.brief, ctx.series)
    except (KeyError, TypeError, ValueError) as exc:
        raise QualityFail(f"ScenePlan sai schema: {exc}") from None
    problems = prompt.check_plan(plan, ctx.brief, ctx.bible)
    if problems:
        raise QualityFail("; ".join(problems))
    plan.note = f"plan: {ctx.adapter_id('llm').get('model')}"
    return Result(output=ctx.td.plan_path(1), plan=plan)   # luồng chính ghi revision thật
