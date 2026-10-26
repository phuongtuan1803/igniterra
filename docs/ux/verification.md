# Prototype UI verification

## Scope and safety

Implementation branch: `feat/approved-prototype-ui`, based on verified main `3f6905856a4befbcf0817fe03f30b009f603cfd2`.

All execution used isolated temporary data, fake model/GPU providers and authenticated local HTTP mocks. Browser requests outside loopback were blocked. No live GPU rental, paid model request, production-data deletion, push, PR or merge was performed. Application provider, service, pipeline, store and schema code are unchanged.

## Regression results

- Baseline: `uv run pytest -q` — 286 passed in 362.46s.
- First integrated run: 287 passed in 350.17s.
- After Monitor test and responsive fixes: 288 passed in 377.21s.
- Redirect success/error regression tests cover ordinary POST and HTMX; shell/filter/Monitor checks: 6 passed in 0.63s.
- Final full suite: **290 passed** in 348.72s. Definitive output: [evidence/pytest.txt](evidence/pytest.txt). Latest focused UI checks after adding actual task-series search data: 6 passed in 1.09s.
- One existing Starlette/AnyIO deprecation warning; live tests remain excluded by the repository's pytest configuration.

## Browser checks

Chrome via Playwright, using [the reproducible fake fixture](../../tests/ui/README.md). The browser report and captures are in [evidence](evidence/).

Final browser result: **224 checks passed**, **42 page/viewport checks**, **0 JavaScript exceptions**. Verification covered the full flow and completed output failure/recovery after honoring the service’s configured download retry budget. [Machine-readable result](evidence/browser-results.json).

The responsive matrix covers Tasks, Storyboard, final review, task detail, GPU rental list, offers, rental detail, ComfyUI library, approved workflow, blocked workflow, Monitor, Series list, Series Bible and Settings. Each is checked at 1440, 768 and 390 pixels for document overflow, content width, accessible form names and unique IDs.

Interaction checks include keyboard skip navigation; desktop navigation; modal Escape/focus restoration; canceled cost confirmation; invalid/valid Brief creation; task search/filter/empty state; selecting/editing/uploading scene candidates; keyframe approval; mandatory C01–C09 review before final gate; GPU quote confirmation, provisioning evidence, stop, destroy, unknown/reconcile behavior; workflow import/revision, explicit manifest CLI approval, run, output failure/retry and verified download; Monitor links and no provider calls on GET; CSRF rejection; mobile drawer focus trapping/bottom navigation; offer loading/network error/empty states; navigation and creation fallback without JavaScript.

## Visual comparison and intentional deviations

[Side-by-side comparison](evidence/visual-comparison.jpg) places the approved source on the left and production on the right at the same 1440px viewport. Individual source/production captures are also retained at all three sizes.

| Area | Matched presentation | Production difference retained |
| --- | --- | --- |
| Shared shell | 248px desktop sidebar (216px below 1220px), cream palette, terracotta actions, supplied icons/brand, Georgia display headings, grouped navigation, mobile drawer/bottom bar | No fake avatar, global search, demo environment claim or fabricated counts. Top bar identifies the local studio. |
| Tasks | Four metrics, review attention, task table, filters/search, next actions, quick links, create dialog | Uses actual IDs, raw statuses/wait reasons and unpriced-call indicators; explicit GET search submit works without JS. Full Brief JSON replaces the unsupported title-only demo form. No made-up percentages. |
| Storyboard | Three-column scene gallery, selection border, sticky inspector, selected preview, candidate strip and review below gallery | Uses real media/candidate counts; retains pause/resume/status details, revision/camera/mode, audio/clip/upload/source/rights and rerun controls. Fake-provider images in evidence are plain colors. Missing media is explicit. |
| Final review | Video preview and separate review/approval actions | Portrait real video and all nine C01–C09 result/timestamp/note fields replace the four illustrative checks. The review table scrolls inside a labeled region on narrow screens. |
| GPU | Four metrics, rental/offer tabs, quote confirmation, mobile offer cards, provisioning timeline, guarded actions | Cost/limit/manifest/orphan details are retained. Timeline includes skipped steps and actual attempts/errors. No simulated “next provisioning step”, resume action or success assumption for unknown operations. |
| ComfyUI | Library/run tabs, import dialog, dependency panel, ready-GPU run panel and revision history | Additional API JSON, hashes, immutable revisions, dependency gaps and CLI review guidance remain. Actual run details replace the demo modal; only verified output is linked/previewed. |
| Monitor | Attention first, task/run/GPU links, summary metrics, consistent shell | Additional stored task/GPU/run tables and errors remain; no node percentages invented. Optional pause for the existing 5s refresh supports reading and keyboard use. |
| Series / Settings | Consistent typography, panels, forms, shell and action treatment | Real series list, full Bible/reference jobs and all existing settings remain. No decorative demo character art, invented security toggle or extra provider option. |

## Focused fixes from verification

- The mobile backdrop initially occupied a desktop grid column; it is now absent from layout until opened.
- Grid children now shrink around internally scrollable review/audit tables; narrow offer rows become usable cards with visible quote confirmation.
- Added missing field labels, image descriptions and unique IDs; native dialogs and the mobile drawer restore focus.
- HTMX HTTP errors, connection failures and timeouts now produce a visible offer error with retry guidance.
- Restored provider identity and orphan-check error detail after regression tests caught missing labels.
- Fixed the existing redirect query concatenation bug: scene actions now retain scene selection and flash feedback, and workflow import retains its typed revision query. Normal redirect/HTMX status codes are unchanged.

## Changed file groups

- `studio/web/templates/`: shared shell/macros/status; Tasks/Storyboard/detail; GPU partials/detail; ComfyUI library/workflow/run; Monitor; Series; Settings.
- `studio/web/static/style.css`, `ui.js`, `icons.svg`: approved tokens, responsive components, presentation enhancement and supplied icon assets. Existing HTMX asset is unchanged.
- `studio/web/__init__.py`, `views.py`: presentation filtering/summary context (including actual task series), Monitor task context/refresh preference, and safe redirect query merging. Existing route paths/methods and mutation services remain.
- `tests/test_editorial_ui.py`, `tests/ui/`: UI contracts, regression checks and reproducible browser verification.
- `docs/ux/`: approved reference, mapping, verification and screenshot/test evidence.

## Commits

- `a5b42cc` — audit and approved source.
- `9843e6e` — stage 1: shared shell, Tasks, Storyboard.
- `2cfd431` — stage 2: GPU Rentals and ComfyUI.
- `63d4e79` — stage 3: Monitor, Series, Settings and responsive navigation.
- Final verification/fixes commit: recorded in feature-branch history.

## Limits

Actual provider behavior, paid execution, invoices and destructive production actions were deliberately not exercised. Manifest approval remains a CLI operation. No workflow graph editor, GPU resume/start, run cancellation or publication backend was added. Optional Basic auth and the existing process-local GPU/ComfyUI CSRF design remain unchanged; this work does not add per-user authorization or retrofit legacy task/series/settings CSRF. See [the mapping](implementation.md) for the full contract and unsupported-interaction inventory.

Final diff audit: `git diff --check` passed. Route decorators and mutation handlers/guards were compared to main; all route methods/paths are preserved. No provider, service, pipeline, store/schema, configuration or dependency file changed. Pre-existing untracked `.agents/`, `.claude/`, `.hermes/`, `.worktrees/`, `docs/diagrams/` and `skills-lock.json` were left untouched. The existing local main ref remains at the baseline commit.
