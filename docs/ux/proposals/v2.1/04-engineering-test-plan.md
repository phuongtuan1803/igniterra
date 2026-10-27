# 04 — Kế hoạch code & kiểm thử dựa trên workflow người dùng

## 1. Yêu cầu triển khai không được vi phạm

- Repo `phuongtuan1803/igniterra`, **re-check HEAD trước code**; baseline tài liệu `7d80e075`. Nếu head đã đổi, reconcile diff và cập nhật risk matrix trước khi chạy.
- Giữ FastAPI/Jinja2/HTMX + plain JS hiện có. Không migrate React, không tách microservice, không chạm pipeline/provider nếu chưa có test chứng minh blocker.
- Reuse `Brief` schema, `Actions`, `SeriesActions`, `ComfyService`, `GPUService` và `Store`. New UI route phải gọi service, không tự tạo logic approval/billing ở template/JS.
- **Không** live rent GPU, paid API, destructive cloud op, clone node repo không tin cậy trong tests. Dùng temp DB/fs, fake providers, local ComfyUI/Portal mock đã có.
- **Không** PR/push/merge khi chưa được người dùng xác nhận. Đầu ra hiện tại chỉ là `SUGGESTION` tách tài liệu chính thức.

## 2. Kiến trúc thay đổi tối thiểu

```d2
 direction: down
 browser: "Browser (Jinja2 + HTMX + small JS)"
 ui: "Web routes: existing + small new endpoints"
 application: "Application services: Actions, SeriesActions, ComfyService, BriefLibrary"
 store: "SQLite / data directory (versioned records)"
 adapters: "Provider adapters (unchanged)"
 browser -> ui -> application -> store
 application -> adapters
```

**BriefLibrary (new)** chỉ chịu trách nhiệm CRUD user-owned briefs, validation Pydantic, atomic write + path containment + versioning. Không đổi `Brief` V1, `Action.create_task` hoặc worker. Seed `briefs/*.json` trong repo hiển thị read-only; user brief data nằm dưới data_dir.

**Workflow metadata (new optional P1)**: bảng riêng `workflow_metadata` hoặc bảng field tối thiểu, join `workflow_id`; archive flag không xóa revision/run. Tránh schema nếu P0 chỉ import/update/view/manifest approve đủ; P1 mới migration.

**Manifest web (new P0)**: route attach/approve gọi thẳng ComfyService, server gate/hash/license/audit; submit CSRF, body allowlist, controls immutable revision. Giá trị `accept_licenses` phải được người dùng xác nhận rõ.

**Readiness (mostly presentation)**: aggregate các thông tin đã có để render checklist, **không infer** GPU compatibility trước `/object_info` tại server start; marker `Needs runtime check`.

## 3. Thứ tự code theo vertical slices

| Slice | Nội dung cụ thể | File dự kiến | Test gate | Rủi ro |
|---|---|---|---|---|
| 0 — Audit fixes | F09 title markup; F10 refresh label + true reload; preflight control audit | `templates/comfyui.html`, `monitor.html`, `ui.js`, `tests/test_editorial_ui.py`, `tests/ui` | UI title, refresh/reload, 390/768/1440 | Thấp |
| 1 — Brief MVP | list/creator/editor/preview; safe save; create task using validated Brief | `studio/briefs.py` (new), `studio/web/__init__.py`, `templates/brief_*.html`, `tasks.html`, tests | U0 tạo Brief không JSON, lưu reload, tạo Task; không paid API | Trung bình |
| 2 — Series References | onboarding checklist, candidate upload preview, clearer approve + sheet, upload validation | `series_list.html`, `series.html`, `studio/web/__init__.py` upload validation (focused), tests | Tạo Series + 1/3 refs + approve, malformed/large image reject | Trung bình |
| 3 — Workflow P0 | import wizard GUI/API, dependency gaps, manifest attach/approve UI, readiness | `comfyui*.html`, `studio/web/__init__.py`, `ComfyService` reused, tests | Import→approved rev without CLI, blocked fallback, CSRF/license/SHA | Cao: security |
| 4 — Workflow management P1 | metadata rename/note, archive/restore, metadata compare, version usage & safe Settings picker | `studio/comfyui/service.py`, `store.py` migration if approved, templates + tests | Legacy compatibility + archive retains runs/artifacts, no implicit active switch | Trung bình |
| 5 — Integration polish | cross-links from Storyboard/GPU/Workflow/Monitor, accessibility, onboarding copy | templates, `ui.js`/CSS, browser tests | 5 user journeys, no dead controls | Thấp–trung bình |

Mỗi slice là **một commit hoặc ít commit liên quan**, code review + focused tests trước khi đi tiếp. Những slice tăng scope backend (3,4) phải trình bày riêng để duyệt, không tự làm ngay cùng CSS.

## 4. Implementation tickets chi tiết

### T0.1 — Khắc phục trang ComfyUI (F09)

- Sửa `{% block title %}ComfyUI Workflows{% endblock %}`, chỉ 1 `dialog#import-workflow` trong `body`.
- Assert HTML generated title clean/DOM count; visual snapshot.
- Không xóa import profile FLUX; chuyển vị trí hợp lệ.

### T0.2 — Làm mới Monitor (F10)

- `Làm mới bây giờ`: GET đúng `/monitor` với preserved `task` & `refresh` mode; nếu auto-refresh off thì không thay đổi off.
- `Tự cập nhật` on/off là control độc lập; document 5s interval chỉ reload data, không gọi provider.
- Focus preservation when refresh/manual mode.

### T1.1 — Brief persistence API (N)

- `BriefLibrary` read-only seed + writable user briefs; identity/slug collision, edit/duplicate semantics, atomic save, schema validation, size limit, path sandbox, history as needed.
- Route: `GET /briefs`, `GET /briefs/new`, `POST /briefs`, `GET/POST /briefs/{id}`, `POST /briefs/validate`; **route naming indicative**, settle based on current conventions.
- Store only complete valid `Brief` for publish to Task; incomplete drafts optional in separate `draft` struct; never expose unvalidated draft via production task creation.

### T1.2 — Brief form + Task integration

- Form dynamic beats / must_show, server-side validated. Avoid `JSON.parse()` on free text for U0.
- Task screen links valid user brief. Existing `POST /tasks` still accepts saved seed + `brief_json`; safest initial implementation passes validated JSON generated server-side, later direct library ref if needed.
- Explicit `Lưu Brief` no paid; `Tạo Task` confirm provider may bill, worker status.

### T2 — Series beginner wizard

- Combine create → add character → upload/generate candidate → approval in one breadcrumb journey but reuse existing routes.
- Media validate MIME/content/size before file persist; `source` + `rights` required; preserve `sources.yaml`; avoid deleting approved refs when updating character description.
- One required ref; 2–3 recommended; verify sheet and real files.

### T3.1 — Workflow import wizard

- Preview phase server checks with `workflows.load/detect/check_api/extract_gui` **without writing revision**. Import route executes only after confirmation; file cap already 5MB JSON, add request/upload guard.
- New import vs update explicit; existing workflow selection; note; metadata diff of revision (no node editor).
- Duplicate content no additional revision; errors inline; preserve CLI compatibility.

### T3.2 — Manifest attach/approve web

- `attach`: parse `WorkflowManifest`, validate with service, persist pending review only. `approve`: re-read canonical hash, ensure status pending, surface licenses, require explicit confirmation, audit event. CSRF/actor/capability local auth policy; don't allow arbitrary file path/load from server.
- Show "Approved dependency ≠ installed": runtime `/object_info` still checked at run; show blockers if no ready GPU.
- Test race/tamper/reuse and noncommercial license warnings; don't weaken existing guard.

### T4 — Optional workflow management

- Metadata only rename/notes, soft archived; read-only revision compare (models/custom node diff/hash). Keep revision immutable, pinned references unaffected, archived workflows still resolvable for old run; no hard-delete.
- Explicit cross reference to Settings configured ComfyImage workflows; give warning before archive of in-use workflow; behavior (allow/deny archive) must be decided before implement.

### T5 — More understandable Settings, Monitor & links

- Read-only helper lists `ready rentals`, `approved runnable workflows` with IDs, name, revision and cost; selected values shown with labels; validate stale references before save. Don't auto-switch production image model or rent.
- Onboard use-case CTA `Bước còn thiếu` deep-link to corresponding screen. If manifest review not implemented show honest CLI guidance, not success button.

## 5. Test pyramid & fixture reuse

### Unit/model

- `Brief.model_validate`: ranges, optional fields, JSON serialization.
- BriefLibrary atomic/path/permissions/draft vs validated, no repo write, concurrent update conflict.
- Workflow `load/detect/extract/check_api` for GUI, API, both, unknown/malformed/large, SHA duplicates.
- Workflow Manifest URL pinning/full SHA, model hash/size/license, duplicate custom node and unsafe requirement rejection, tamper hash.
- Series media validation source/rights, MIME/content mismatch, invalid dimensions, workbook ffmpeg failures.
- Workflow archive metadata won't remove old revision/run; CAS and idempotency unchanged.

### Route integration (FastAPI TestClient)

- Real POST contract for every enabled CTA, expect redirect/flash/errors correct; CSRF on new mutation routes, nonce on paid.
- User input with malicious strings not execute HTML; upload path traversal and file disclosure blocked.
- Task/Series/Workflow/GPU state transitions exercise guard and exact response, no direct adapter calls in web.
- `create_unknown`,`destroy_unknown`,`submit_unknown` never lead auto repeat paid request.
- Settings validated, fails safe for stale pins, no leaked `VAST_API_KEY`, `WEB_PASSWORD` or API credentials.

### Browser E2E (existing fixture)

Use existing `tests/ui/fixture_server.py` + `tests/ui/verify.cjs` (fake providers, loopback ComfyUI/Portal, no paid network). Add `tests/ui/beginner-journeys.cjs` or extend test without hardcoding demo data in app.

| Test ID | Scenario | Expected |
|---|---|---|
| B01 | First launch with no tasks/briefs | Clear choice; CTA goes to Brief Builder |
| B02 | Brief valid + save/reload | Same data; no paid request |
| B03 | Brief invalid beats/duration | Inline error; no task |
| B04 | Brief → Task confirmed | Task stored; status reflects worker, no fabricated progress |
| B05 | Series create + add character | Bible saved; candidate list empty |
| B06 | Character upload with source/rights → approve 1 image | Candidate persisted; sheet generated, marked approved |
| B07 | Character generation trigger | Cost confirm, queued job, no instant image |
| B08 | Invalid/corrupt/oversized image | Rejected, no persisted accepted ref |
| B09 | Workflow GUI+API import | Parsed, new rev, correct runnable/needs-review state |
| B10 | Workflow update with identical bytes | No new rev |
| B11 | Workflow update with changed JSON | New immutable revision; older runs still point to old |
| B12 | Manifest missing model/requirements | Actionable gaps; cannot approve |
| B13 | Manifest valid + risky license | Explicit license confirmation; no silent approval |
| B14 | Tamper approved manifest record | Run refused/approval rejected as applicable |
| B15 | Workflow ready but GPU missing | `Chọn GPU` CTA, no fake success |
| B16 | GPU ready but missing node/model | Blocked, no POST /prompt |
| B17 | Run success with verified output | Output downloadable only after verified |
| B18 | Failed output download and retry budget | Correct state/guard, no paid resubmit |
| B19 | GPU destroy while output unverified | Double confirmation, no deletion without acceptance |
| B20 | GPU `destroy_unknown` | Cannot assume stopped billing, reconcile only |
| B21 | Workflow archive/restore (P1) | Hidden by default, history intact, no data loss |
| B22 | Monitor refresh vs auto toggle | Manual does not flip toggle, task filter preserved |
| B23 | ComfyUI title regression | Title clean, one dialog ID in body |
| B24 | Every visible button/action | Real handler or disabled with actionable reason |
| B25 | Responsive 390, 768, 1440 | No page clipping, focus trapping, form accessible, tables usable |
| B26 | Tab, Esc, no-JS fallback | Keyboard functional; essential forms accessible |
| B27 | Block live network/paid calls | Zero outbound non-loopback; fake providers only |

### UX task-based usability sessions

5 first-time testers (or 5 independent dry runs) each attempt J01, J02, J03 without docs/CLI. Record time, number of blockers, helper prompts, successful finish; **don't falsely claim these are already done**.

## 6. Test execution / evidence

```bash
# baseline (local isolated environment)
uv run pytest -q

# fake browser fixture; no real Vast / paid provider
UV_CACHE_DIR=/tmp/igniterra-uv-cache uv run python tests/ui/fixture_server.py
# second terminal, if Playwright available:
node tests/ui/verify.cjs

# after new tests are added:
uv run pytest -q tests/test_briefs.py tests/test_web.py tests/test_comfyui.py tests/test_editorial_ui.py
```

Ghi commit HEAD tested, test counts, failures, browser screenshots 1440/768/390, input fixtures, expected vs actual. PR #17 reported **290 pass + 224 browser checks**; không được coi những con số đó là kết quả của lần sửa tương lai.

## 7. Definition of Done

- End-to-end 3 beginner goals (Brief→Task→video review; Series→refs→sheet; Import/update workflow→manifest→GPU→verified output) có từng trạng thái/CTA rõ ràng.
- Mọi control CTA `Enabled` có route/handler, full server validation, predictable result; không có action giả hoặc no-op.
- 100% checks relevant passing, no new security/cost regression; no paid GPU/network tests; screenshots được review.
- Tài liệu/diagrams mapping với từng route/test; không duplicate vội tài liệu official `docs/src/design/`; đầu ra lúc này là proposal.
- Scope creep (graph editor, auto model downloader, payment, multiuser, social publish, workflow hard deletion) ra ngoài MVP.

## 8. Handoff prompt cho Codex (copy sau khi duyệt)

```text
Repository: phuongtuan1803/igniterra.
Read UX proposal bundle 01–05 and diagrams. Verify latest origin/main HEAD and update the baseline/evidence matrix.
Task: implement vertical slices 0 -> 1 -> 2 -> 3, preserving existing FastAPI/Jinja2/HTMX contracts. Treat new UI routes as proposals until implemented. Use existing services and safe fake-provider tests; never fabricate backend success. Add integration/browser regression tests mapping B01–B27 and evidence. Respect manifest/license/CSRF/quote/nonces/unknown/cost guards. Implement slice 4 metadata/archive only after separate approval; do not introduce graph editor/cancel run/real GPU action.
Deliver: concise diff/route-action matrix, test counts, browser screenshots at 390/768/1440, known blockers, grouped commits. Do NOT push/create PR/merge without my explicit approval. Stop at end of each slice for review.
```

## 7. Bổ sung v2.1 — ưu tiên maintain: một input binder + một runner

**Không tách motion thành service hoặc thư viện riêng.** Lõi giữ nguyên `ComfyService`, `comfy_workflows`, `comfy_runs`, các guard; chỉ thêm metadata phiên bản nếu thật sự cần: `workflow_type` (image/motion/custom), một input mapping ngắn và kiểm định output. Metadata có thể thêm vào record phụ 1:1 theo `(workflow_id, revision)` để tránh sửa revision đã approved; khi user sửa mapping sau approval phải tạo revision mới hoặc xét lại approval và fingerprint theo cơ chế chuẩn.

### T3.3 — Input mapping khai báo, tránh generic graph editor (P1)

- Khi nhập image/motion, chọn loại mục đích và khai báo **chỉ input muốn người vận hành nhập** (prompt / image / duration). Chưa hỗ trợ mapping thì hiển thị `Workflow dùng giá trị cố định, chưa hỗ trợ nhập tùy chỉnh` hoặc blocked theo chính sách rõ ràng.
- Không tự dò node rồi viết vào arbitrary `inputs` của ComfyUI. `node_id, input_key, type, required, allowed_values, bounds` phải được xác thực server-side với API graph revision và `/object_info` khi submit.
- Mỗi lần Run: validate input → upload ảnh được phép (hash-checked) → clone graph đã pinned → bind input allowlist → chạy qua **ComfyService hiện có**, không đổi source revision; lưu fingerprint/provenance và request nonce trước paid submit.
- `start_run` hiện chỉ nhận `(run_id, workflow_id, revision, rental_id)`; cần thay đổi contract một lần, giữ backward compatibility cho CLI/web hiện tại, toàn bộ unknown/race/cost guard.
- Video output: `runtime.output_files` đọc `videos`; `runs.media_type` nhận MP4/MOV/WEBM/MKV sau magic validation. Thêm kiểm tỉ lệ/thời lượng khi chuyển clip vào pipeline. Fixture ComfyUI image-to-video thật (mock server có đủ output metadata) là gate trước khi bật motion.

### T3.4 — Motion chọn trong Storyboard (P1 riêng, sau T3.3)

- Scene dropdown `Ảnh chuyển động nhẹ (local) / AI Motion (ComfyUI) / Clip tải lên`. Hai chế độ đã có không bị thay đổi.
- `studio.providers.REGISTRY['video']` hiện chỉ có `fake`; tạo adapter mỏng `ComfyVideo` bám contract `VideoProvider.submit/poll` hoặc adapter tương thích song song, **không viết lại ComfyService**.
- Chỉ bật khi revision đã duyệt và mapping output/input type Motion valid; tách cấu hình chọn default motion workflow khỏi image workflow, nhưng dùng **cùng picker**. Cho override per-scene nếu thật sự cần sau MVP; không thêm hàng loạt config knobs.
- Bảo toàn `job_id`, unknown resume không submit lại, `media.check_clip`, cost warning và fallback hiện có. Test motion run fail/fallback, paid confirmation, restart/recovery.

### Dedup policy theo ba lớp

1. **UI HTML:** loại dialog `import-workflow` lặp trong `comfyui.html` và test only-one `id` + clean title.
2. **Import/revision:** existing dedup latest same-ID tiếp tục; global duplicate lookup **warning only**; hiển thị `Use existing` / `Import as separate` có chủ đích và audit; không hard-delete hoặc tự merge references.
3. **Dữ liệu cũ:** báo cáo nhóm cùng content hashes, label về số pinned/runs; quyết định từng nhóm trước khi archive; không migrate ngầm.

### Minimal maintenance acceptance gates

- U0 có thể **nhập workflow bằng ≤2 bước**, không nhập SHA/node IDs ở chế độ Basic.
- Mở workflow approved → 1 form chung; `prompt`, `image`, `duration` **chỉ hiện khi được mapping**, default còn lại giữ trong approved graph.
- Không phát sinh API/GPU phí khi upload/check/import/approve; chỉ ở Rent/Run theo confirmation.
- Không có codepath tách riêng image/motion cho import, revision, duplicate check, manifest approval, run monitor và verified output.
- Test end-to-end bằng fake ComfyUI, fake GPU; không hứa hỗ trợ live motion nếu chưa có evidence.

### Browser checks bổ sung (thêm vào B01–B27)

| ID | Scenario | Expected |
|---|---|---|
| B28 | Import same content twice under different IDs | Duplicate warning + link existing; no silent delete |
| B29 | Import same content into existing workflow latest | No extra revision |
| B30 | Import content equal older revision | Warning and preserved history; explicit action required |
| B31 | Import GUI/API with missing part | One clear blocker; no fake runnable |
| B32 | ComfyUI library title/dialog | Only one dialog in body and clean title |
| B33 | Select Image workflow | Basic fields from its mapping only |
| B34 | Select Motion workflow | Image + duration if mapped, select GPU, fee confirm |
| B35 | Motion video verified download | Real video playback/link; invalid magic/hash blocked |
| B36 | Motion selection in Storyboard without VideoProvider | Disabled + reason; no fake run |
| B37 | User changes input mapping for approved revision | Cannot silently mutate approved graph; requires new version/review |
| B38 | Unknown paid motion submission | Reconcile existing run/job; never paid resubmit |
