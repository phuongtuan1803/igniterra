# Approved prototype integration

## Baseline and scope

- Verified `origin/main` with `git ls-remote origin refs/heads/main`: `3f6905856a4befbcf0817fe03f30b009f603cfd2` (2026-10-27). Local HEAD matches.
- Feature branch: `feat/approved-prototype-ui`. No push, PR, or merge authorized.
- Visual source: `reference/igniterra-prototype.html`, supplied during the audit. Its browser arrays and success simulations are reference material only.
- Implementation stays in FastAPI/Jinja2/HTMX. No schema, provider, pipeline, or service redesign.

## Prototype → production contracts

| View / interaction | Existing production route and contract | Integration boundary |
| --- | --- | --- |
| Tasks list, metrics, search/status filters | `GET /`; `views.task_summary` supplies actual brief title, ID, recorded cost, missing prices, renders, task status/current step | Search/filter are presentation over stored tasks. No invented progress percentages or activity. |
| Create task | `POST /tasks`: `brief_file` or complete `brief_json`, optional `series`, `overrides` | Prototype title-only creation is unsupported. Keep validated Brief input and task-only overrides; worker executes later. |
| Storyboard / inspector | `GET /tasks/{task_id}?scene=sNN`; `GET /tasks/{task_id}/status` HTMX polling | Real ScenePlan, selected assets, stale revision and unknown run indicators. |
| Save / candidate / rerun / upload | `POST /tasks/{task_id}/scenes/{scene}/edit|select|rerun|upload` | Preserve field names, camera vocabulary, revision creation, source/rights/AI metadata, step-specific invalidation and running-task lock in `gates.Actions`. |
| Keyframe approval | `POST /tasks/{task_id}/gate`: `decision`, `note` | Approval requeues pipeline; reject records decision. Confirm potential paid continuation. |
| Final review | `POST /tasks/{task_id}/review`, then `/gate` | Keep all C01–C09 result/timestamp/note fields, reviewer/minutes, render revision and separate gate decision. Four illustrative prototype checks cannot replace them. Existing final guard requires a review record; review acceptance is computed separately. |
| Task controls / diagnostics | `POST /tasks/{id}/pause|resume|cancel`; `GET /tasks/{id}/detail`; `POST /tasks/{id}/unknown/{scene}` | Keep allowed wait reasons, worker boundary pause, costs/log/revision access and explicit unknown retry/skip. |
| GPU offers | `GET /gpu`, `GET /gpu/offers`: `search`, `min_gpu_ram_gb`, `disk_gb`, `max_dph` | Explicit search only; preserve HTMX response errors and real quote timestamps. |
| Rent | `POST /gpu/instances`: `csrf`, unique `rental_id`, quote `offer_id/dph_total/quoted_at`, filters, `confirm` | Service revalidates quote age/price, verified offer, approved pinned provisioning manifest and limits before create. |
| Rental lifecycle / cost / provisioning | `GET /gpu/instances/{rental_id}`; `POST .../reconcile|stop|destroy` | Preserve CSRF, confirm checkbox, provider stop support, legal transitions and unknown-operation restrictions. Destroy with unverified output requires separate `accept_unverified`; confirmed disappearance is required for destroyed. Show estimates, disk/bandwidth caveats, limits and orphans. |
| Workflow library / import / revision | `GET/POST /comfyui/workflows`; `GET /comfyui/workflows/{id}?revision=N` | Preserve `csrf`, multipart `file`, optional `api_file`, name, workflow_id, note; immutable revisions and dependency review state. |
| FLUX profile import | `POST /comfyui/image-profile`: `csrf`, `confirm` | Imports two workflows and a pending manifest only; no rental, model download or approval. |
| Run workflow | `POST /comfyui/workflows/{id}/runs`: `csrf`, unique `run_id`, `revision`, `rental_id`, `confirm` | Approved runnable revision + ready GPU + runtime dependency checks + one run per instance. |
| Run status / output | `GET /comfyui/runs/{id}`; `POST .../refresh|retry-download` with CSRF | Unknown submission resolves via queue/history, never resubmits. Success requires downloaded verified output. Preserve provenance links to task/scene/rental and events. |
| Monitor | `GET /monitor?task={id}` | Read stored task/run/GPU state; intervention links to guarded task, rental and run pages. Keep GET free of provider mutations/polling. |
| Series | `GET/POST /series`; `GET /series/{name}`; `POST .../update|characters|characters/{key}/ref|upload|approve` | Keep Bible fields, cast, queued reference generation, source/rights, multi-select approval and character sheet. |
| Settings | `GET/POST /settings` | Keep adapter/model/concurrency/keyframe gate + ComfyImage IDs; validate providers without paid calls and write data/settings.toml only. |
| Shared shell / assets | `base.html`, `_macros.html`, `/static/*`, `/files/{relative:path}` | Preserve Basic auth, escaped Jinja output, contained file paths and exclusion of private data/gpu files. No external fonts/runtime dependency. |

## Unsupported interactions and deliberate differences

- No prototype demo IDs, simulated provisioning steps, completion buttons, in-memory rental/run state or synthetic percentage progress.
- No backend title-only task editor, workflow graph editor, run cancellation, GPU resume/start, invoice API or publication action. Do not add success controls for these.
- Manifest attachment/approval exists in service/CLI, without a corresponding web mutation route. Retain actual CLI guidance and review blockers.
- Limits/provider configuration and ComfyImage revision pins remain in TOML/CLI where existing web routes do not expose them.
- Monitor currently shows ComfyUI runs and GPU state, with task provenance only for linked image requests. General task attention can be derived from stored tasks without provider calls.
- Production assets replace the prototype's decorative space illustrations. Missing media must say so, rather than show invented output.
- Existing security boundary: Basic auth is optional; there are no per-user roles. GPU/ComfyUI POSTs have process-local CSRF (one web worker). Legacy task/series/settings POSTs do not share that CSRF mechanism; this UI work must not claim otherwise or change authentication architecture.

## Stages

1. Shell/tokens/navigation/shared assets; Tasks search/create/attention; Storyboard/inspector/final review.
2. GPU offers/rentals/provisioning/costs/limits and ComfyUI import/library/dependencies/revisions/run/output.
3. Monitor task attention/navigation, Series/Settings consistency and responsive/accessibility polish.
4. Regression and browser evidence at 1440, 768 and 390 pixels; deviations and final diff audit.

## Verification

Baseline `uv run pytest -q` started before implementation; result and browser evidence will be recorded after completion. All verification must use isolated temporary data, fake providers and local test HTTP servers, never the live studio configuration.
