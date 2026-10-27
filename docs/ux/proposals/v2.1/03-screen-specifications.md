# 03 — Đặc tả UX/UI chi tiết theo màn hình (đề xuất)

## Quy ước

- **E (Existing):** route/action đã có. **N (New):** cần thêm code backend hoặc UI route. **P (Presentation):** UI-only với dữ liệu đang có.
- Nội dung trang dành người mới dùng tiếng Việt dễ hiểu; thuật ngữ kỹ thuật có giải thích ngắn theo ngữ cảnh. Có **Nâng cao** tùy chọn, giữ hiển thị SHA, revision, IDs, logs cho U1.
- Layout desktop 1440px: sidebar có nhóm, trung tâm max-width, 1 thao tác chính/vùng, câu mô tả ngắn. Tablet 768px: form dọc, không đè sidebar. Mobile 390px: menu thu gọn, bảng → cards hoặc scroll container có nhãn.
- Mỗi action có *precondition → handler → result → failure/blocked* và ghi bằng chứng trong test. Không tạo nút nếu không có handler thật. Hành động có phí (API/GPU) luôn có xác nhận; destructive phải ghi rõ hậu quả.
- ID màn: `Sxx`, use case: `Jxx` ở `02-beginner-user-journeys.md`; Route là dự kiến, **không tự ý coi route N là đã tồn tại**.

---

## S00 — First-use / readiness (N, P0)

**Route:** `GET /start` hoặc panel theo ngữ cảnh trên `/` (ưu tiên panel để ít route). **Mục tiêu:** người mới biết lựa chọn "Tạo video", "Dùng ComfyUI", "Tạo Series".

**Bố cục:** top: 1 câu giải thích; 3 task cards; đoạn `Trạng thái sẵn sàng` gồm worker, provider, GPU provisioning manifest, workflow readiness (chỉ đọc từ nội bộ). **Không buộc setup tài khoản/cloud**.

**Action:** `Tạo Brief`, `Nhập Workflow`, `Tạo Series` đều dẫn tới đích thật. Nếu worker không chạy: "Đã lưu nhưng công việc chưa bắt đầu; cần khởi chạy worker" kèm lệnh cho operator dưới Nâng cao; **không** có nút Start Worker giả.

**Test:** Tài khoản/workspace mới + seeded; mọi CTA tới route; không gọi provider; trạng thái setup sai được hiển thị. Không hiển thị phí `0` nếu còn unknown.

## S01 — Brief Library (N, P0)

**Route:** `GET /briefs`; **hiện chưa có**. **Mục tiêu:** quản lý Brief trước khi tạo Task.

**Layout:** danh sách Brief đã lưu (title, ngôn ngữ, target duration, updated, source template/user), filter/search; hai CTA `Tạo Brief` và `Dùng Brief mẫu`; link `Xem/Sửa`, `Tạo Task từ Brief`. Cho lưu Brief người dùng tại vùng data riêng (`data/briefs/` hoặc module store), **không sửa** bản mẫu versioned `briefs/*.json`.

**Action:** tạo -> S02; chọn mẫu -> S02 prefill; create task -> S03 preselected; duplicate (P1), archive (P2). **Không gộp Save Brief và Submit Task**.

**Empty/errors:** chưa có -> nêu lợi ích + CTA; mẫu đọc không được -> thông báo; trùng id -> validation. **Test:** list từ data + mẫu; edit không ghi repo; XSS; bad path traversal.

## S02 — Brief Builder / Preview (N, P0)

**Route:** `GET /briefs/new`, `GET /briefs/{id}/edit`, `POST /briefs/validate`, `POST /briefs` (tên minh họa; quyết sau review). **Backend:** reuse `studio.contracts.Brief` Pydantic; cần storage service mỏng tránh route chứa logic.

**Form 3 bước:**

1. **Ý tưởng:** `title`, `hook` (giải thích hook mở đầu), `beats` danh sách item add/remove/reorder, `twist`. Trường cần: `id` generated slug/UUID unique; không cần người dùng tạo thủ công.
2. **Cài đặt:** `language` default `vi-VN`, `target_s` min/max (45–58), `max_s` 60, `scenes` min/max (4–6), `characters[]` (optional), `must_show[]` (`what`, `not_before`), `must_not[]`, `notes`, `music`. Chỉ lộ Nâng cao khi cần.
3. **Xem trước:** tóm tắt nội dung, scene dự kiến, tổng thời lượng và validate `Brief` server. `Lưu Brief` an toàn, không gọi API; `Tạo Task` -> S03.

**Ảnh:** *không có field ảnh trong Brief v1*. Trên form chỉ hướng dẫn: "Ảnh nhân vật thêm trong Series Bible; ảnh từng scene thêm sau khi có Storyboard"; không giả lập ảnh tự gắn vào brief. Nếu sau này thật sự cần ảnh trong Brief, làm ADR/schema migration/pipeline task riêng.

**Trạng thái:** unsaved, saving, saved, invalid `target_s`, empty beats, conflict, pending task. **Test:** parity form→Brief.model_validate; Unicode; range; empty title/hook/beats; invalid 2 ranges; reload draft; không chi phí.

## S03 — Tạo Task (E + P, P0)

**Route:** `POST /tasks` đang có; UI ở `/` và từ `/briefs/{id}`. **Precondition:** Brief hợp lệ; series optional; worker/provider status read-only.

**Layout:** bước xác nhận: preview brief → series chọn (nếu Bible chưa đủ refs giải thích) → summary cấu hình provider/chi phí "có thể phát sinh, chưa biết chính xác" → `Tạo Task`. Hộp JSON hiện chỉ trong Advanced import.

**Action:** E `POST /tasks` với valid brief_json/brief_file, series, overrides; return redirect Storyboard/Task pending. Không trực tiếp chạy model từ modal. **Errors:** provider off/missing worker, schema, invalid series, duplicate ids. **Test:** valid server action, invalid prevents, double submit guard/duplicate, cost confirm, new user không JSON.

## S04 — Tasks / My Productions (E + P, P0)

**Route:** `GET /?status=...&q=...` có sẵn. **Layout:** ưu tiên `Cần tôi xử lý` (waiting/failed) -> các tác vụ đang chạy -> list all, CTA `Tạo Brief/Task`; status thân thiện: Chờ duyệt / Đang xử lý / Hoàn tất / Lỗi.

**Actions:** search/filter/get task, mở Storyboard, Log & costs, chỉ action thật. **Empty:** không task -> `Tạo Brief`. **Test:** status=q preservation, labels, keyboard, no fabricated progress %, correct cost/unpriced metadata.

## S05 — Storyboard / Scene Inspector (E + P, P0)

**Route:** `GET /tasks/{id}?scene=sNN`; `POST .../scenes/{scene}/edit|select|rerun|upload`. **Layout:** header task/status; scene filmstrip; inspector một scene: ảnh chọn, ứng viên (image/voice/clip), text narration/visual, camera/motion, upload & rerun; status stale/unknown rõ ràng; sticky action save.

**Actions:** `Chọn ảnh`, `Lưu thay đổi` -> Plan revision; `Tải ảnh/audio/clip` -> validate source/rights, AI-generated; `Chạy lại` -> chọn step + note + cost confirmation. Đang chạy thì disable kèm lý do, không đơn giản giấu nút. **Ảnh không tự ghi Bible**: media upload cho scene khác reference character.

**Empty/errors:** chờ plan -> thông báo worker; chưa candidate -> `Tải ảnh` hoặc `Chạy lại keyframe`; unsupported format, stale revision, unknown paid call. **Test:** edit isolated scene/revision, selection persists after redirect, upload safe path/suffix/size/media, no duplicate paid calls.

## S06 — Duyệt keyframes & Final Review (E + P, P0)

**Route:** `POST /tasks/{id}/gate` và `/review`. **Layout:** 2 giai đoạn riêng. Keyframes: coverage N/N, cảnh chưa có selected; Final: preview MP4 dọc, full 9 tiêu chí C01–C09, kết quả/mốc/ghi chú, phút người dùng/Reviewer, tổng phí biết được.

**Action:** `Duyệt keyframes` chỉ ở gate:keyframes; `Ghi phiếu duyệt` rồi `Duyệt cuối` khi gate:final. Hai action tách rõ và đều có feedback. Có phí khi duyệt gate tiếp tục worker phải confirm.

**Errors:** checklist incomplete, no video, failed criteria, state changed/locked, unknown call. **Test:** không skip checklist, accepted calculation same core, submit once, final button state, correct render revision.

## S07 — Task Detail / Diagnostics (E + P, P1)

**Route:** `GET /tasks/{id}/detail`, `POST /tasks/{id}/unknown/{scene}`. **Layout:** timeline tiến độ (dữ liệu thực), failures/unknown trước, expandable technical logs, costs.jsonl/download refs, revision history.

**Action:** `Retry` / `Skip` unknown có cảnh báo "Provider có thể đã tính phí; phải kiểm tra trước"; không có `mark success` giả. **Test:** unknown state not resubmitted automatically; `retry/skip` only legal; logs redacted; accessible toggles.

## S08 — Series Library & Create (E + P, P0)

**Route:** `GET/POST /series`. **Layout:** cards với số characters, refs approved, trạng thái setup; `Tạo series` wizard nhẹ `Name`/`Khởi tạo từ task`/tùy chọn style; breadcrumb đến S09.

**Action:** E `POST /series`, không chi phí. **Errors:** invalid slug, existed, `from_task` không có plan. **Test:** blank/from_task create, no paid call, prevent collision, meaningful empty.

## S09 — Series Bible Overview (E + P, P0)

**Route:** `GET /series/{name}`, `POST /series/{name}/update`. **Layout:** *readiness checklist*: Style, Voice, Characters, Character references; cards theo nhân vật, danh sách công việc đang thiếu. Tabs `Tổng quan`, `Nhân vật`, `Giọng & phong cách`, `Jobs lịch sử` dùng liên kết sâu có fallback no-JS.

**Actions:** lưu style/negative/narrator/lexicon, thêm nhân vật -> S10, xem jobs, dùng Bible khi tạo Task. Nói rõ: "Đã tạo Bible" ≠ "đã duyệt ảnh". **History:** đã có file history ở backend, UI có thể show read-only; restore để lát sau.

**Test:** update preserves refs/sheet; validation; save shows real state; changes only affect future generation as service semantics.

## S10 — Character + Reference Image Manager (E + P, P0)

**Route:** E `POST /series/{name}/characters`, `.../{key}/upload|ref|approve`. **Layout:** 3 khối theo thứ tự: (a) mô tả nhân vật (b) Tải ảnh / Tạo ảnh AI (c) Candidate gallery -> Duyệt -> Sheet.

**Upload form:** chọn 1 ảnh PNG/JPEG/WebP, preview, `source` (nguồn), `rights` (quyền sử dụng), xác nhận. Upload thành **candidate**, không tự approve; lưu filename, kích thước thực và trạng thái. Nếu tạo AI: chọn front/side/three_quarter, note, cảnh báo phí, job ID và trạng thái.

**Approval:** gallery có checkbox, chọn **ít nhất 1**, khuyến nghị 2–3, hiển thị preview sheet, tạo khi bấm confirm. Dùng backend `SeriesActions.approve_refs()` nên refs/sheet versioned. Ngăn approve ảnh lỗi/ngoài folder.

**Test:** upload invalid mimetype, corrupt file, file size, path traversal, missing source/rights; job enqueue không sinh ảnh ngay; approve 1 và 3; FFmpeg error không để sheet giả; history unchanged.

## S11 — GPU Rentals List & Offer Search (E + P, P0)

**Route:** `GET /gpu`, `GET /gpu/offers`, `POST /gpu/instances`. **Layout:** alert đang billing/unknown; tabs `Máy của tôi` và `Tìm GPU`; "GPU cần điều kiện gì" dạng giải thích VRAM/disk; `Chi phí dự kiến` tách compute/storage/bandwidth và validity thời gian quote.

**Action:** search explicit, filter, confirm quote, rent. Khi thiếu provider/manifest hiển thị blocker + Nâng cao hướng dẫn CLI; không render enabled Rent khi impossible. **Test:** stale/repriced quote, verify/offers missing data, caps, debounce/no search on page open, double submit nonce, no real provider in tests.

## S12 — GPU Rental Detail / Provisioning (E + P, P0)

**Route:** `GET /gpu/instances/{id}`, `POST .../reconcile|stop|destroy`. **Layout:** persistent cost banner, timeline provisioning, known/unknown status, output chưa kiểm, actions ở dưới, chi tiết kỹ thuật collapsed.

**Action:** reconcile một lần khi unknown; Stop nếu provider hỗ trợ (vẫn billing storage); Destroy (xóa dữ liệu thuê; nếu unverified output cần accept_unverified + confirm). Chỉ nói kết thúc phí khi provider xác nhận destroyed. **Test:** CAS/races, duplicate clicks, unknown create/destroy, password/secret redaction, cost estimates caveat, orphan, worker-off.

## S13 — Workflow Library chung Image / Motion (E + N, P0)

**Route:** `GET /comfyui/workflows` E. **Bố cục:** một thư viện duy nhất, mỗi card/row gồm tên, loại **Image/Motion/Custom**, revision, trạng thái `Ready/Needs review`, số run; tìm kiếm và filter. Không tạo tab hoặc bảng riêng cho Motion. `workflow_type` là **metadata user-confirmed (N)**, không suy ra chắc chắn từ đuôi JSON. Loại không được khai báo vẫn là Custom.

**CTA:** `Nhập workflow` mở S14 ở mode New; `Thêm phiên bản` từ Detail mở S14 ở mode Update; `Mở` xem Detail; `Chạy` vào cùng panel S17. `Đổi tên/Archive/Restore` để P1 tùy nhu cầu, không hard delete. **Trùng hash:** cảnh báo và điều hướng workflow sẵn có, không tự xóa/sát nhập.

**Tests:** empty, search/filter, legacy no type, active pins, duplicate SHA across different IDs, revision old runs intact, no dead button.

## S14 — Import / Update Workflow: tối đa 2 bước (E service + N UI, P0)

**Step 1: Chọn file.** Người dùng chỉ chọn `Save (GUI)` và/hoặc `Export (API)` bằng upload/drop; tên mặc định từ file, có thể sửa, ghi chú optional. `New` được xác định khi đi từ Library; `Update` tự lấy `workflow_id` từ Workflow Detail — **không thêm một bước chọn New/Update**.

**Step 2: Kiểm tra & Lưu.** Server parse (size 5MB, UTF-8, detect, check_api, extract_gui) **trước khi ghi revision**; hiển thị loại JSON, điều kiện thiếu, hash-match existing, summary vs latest nếu Update. Nếu trùng: `Mở workflow đang có` là CTA chính; cần thao tác rõ và dấu vết nếu chủ động tạo bản khác. Khi lưu: một `POST /comfyui/workflows` E; same latest content vẫn idempotent; khác nội dung thành revision bất biến. Không approve, rent hoặc chạy tự động.

**Errors:** thiếu API hoặc dependency là *blocker có hướng giải quyết* chứ không báo success runnable. Không tạo những revision nửa chừng khi preview thất bại; request giới hạn dung lượng sớm, hỗ trợ upload không JS.

## S15 — Workflow Detail / Revision Manager (E + N, P0)

**Layout:** header tên/loại/rev/status; form chạy S17 ngay bên dưới nếu có thể chạy; `Thiếu gì để chạy?` nếu chưa được; phía dưới là revision list, dependency summary và lịch sử run. Nâng cao ẩn raw JSON, SHA, class_types, node IDs. `Thêm phiên bản` S14 Update; cập nhật không đổi pin trong Settings ngầm.

**Định nghĩa loại:** `Image` = output image; `Motion` = output video, thường cần một ảnh đầu vào; `Custom` = dạng chưa hỗ trợ form đơn giản, chỉ chạy bản đã xác nhận input cố định. Đây là **khai báo**, không thay validate runtime. Metadata/type binding muốn chạy động phải gắn với **đúng revision**.

**Quản lý:** view/compare metadata (không dựng graph), archive/restore chỉ khi cần P1; bảo toàn old runs, files và approved manifests.

## S16 — Dependency Review (N web + E service, P0)

**Điểm vào:** chỉ khi workflow báo `Thiếu dependency` hoặc người dùng bấm `Xem dependency`. Ở chế độ cơ bản, hiển thị `Model cần bổ sung`, `Custom node cần bổ sung`, `License cần xác nhận` với form ngắn và lỗi tại field. JSON canonical/SHA ở tab **Nâng cao**. Không bắt nhập manifest trong mọi lần chạy.

**Web route đề xuất:** attach và approve reuse `ComfyService.attach_manifest/approve_manifest`, giữ check full SHA, pinned URL/license, CSRF, xác nhận rõ; approved **không** đồng nghĩa GPU có dependency. Test tampering/races/license.

## S17 — Một form Run, inputs theo Workflow revision (N input binder + E run, P0/P1)

**Phần cơ bản trên Detail:** `Prompt` (nếu exposed) → `Ảnh đầu vào` (nếu exposed) → `Thời lượng` (chỉ Motion nếu exposed) → `GPU` (chọn từ ready rental) → **Xác nhận phí & Chạy**. Thiếu GPU/manifest/API thì hiển thị lý do và **một** CTA sửa trước. Không màn preflight riêng, không yêu cầu nhập `workflow_id`, `rental_id`, node IDs, raw JSON.

**Input contract (NEW, lưu/pin theo revision, server-validated):** tập khai báo nhỏ `{key, kind, required, default, bind_to_node_input}`; hỗ trợ MVP `prompt:text`, `image:upload`, `duration:number` khi workflow thật có input mapping phù hợp. Không auto-map tùy tiện hoặc gửi giá trị chưa kiểm qua `/prompt`; upload ảnh bằng đường an toàn, xác thực path, type/size, runtime `/upload/image`. Mọi binding tạo execution-specific prompt/fingerprint, không ghi đè workflow gốc. Chọn nhiều loại input không đòi viết nhiều form/service.

**Run core:** dùng lại `ComfyService.start_run()` sau khi có cách nhận resolved payload mà không làm yếu idempotency, `object_info`, manifest SHA, nonce, CAS và unknown-reconcile. `Motion` chỉ đánh dấu supported sau khi đã test runtime + video output; hiện Storyboard chưa có adapter ComfyUI Video. Tránh ép custom graph thành form dù không có mapping.

## S18 — Run & Verified Outputs (E + P, P0)

**Route:** GET `/comfyui/runs/{id}`, POST `.../refresh|retry-download`. Một trang dùng chung Image/Motion: trạng thái trung thực, lỗi cụ thể, output preview ảnh hoặc `<video controls>` **chỉ khi verified**, tải output, xem rental/chi phí. Không thêm nút `Cancel` hay `Retry paid submit` giả; unknown chỉ reconcile. Với video kiểm MIME/magic/hash đã có và thêm kiểm duration/dimension trước khi đưa vào Storyboard.

**Tests:** no output, invalid video masquerading `.mp4`, failed download, duplicate run, old revision, blocked node/model, audio/image outputs, no paid retry.

## S19 — Monitor / Attention (E + P, P0)

**Route:** `GET /monitor?task=...&refresh=off`. **Layout:** banner cần xử lý, card Task/GPU/ComfyUI, list gần đây; mỗi vấn đề 1 CTA tới xử lý. `Làm mới bây giờ` và `Tự cập nhật` là 2 control khác nhau, không còn nhãn gây hiểu nhầm.

**Test:** no provider GET, refresh toggle, preserve task filter, no auto-refresh while interacting, no status fabrication, every CTA valid.

## S20 — Settings / Provider Setup (E + P / N dropdown, P1)

**Route:** `GET/POST /settings`; **layout:** nhóm `Cơ bản`: lựa chọn provider/model, gating, concurrency; `ComfyUI Image` picker từ approved workflow + ready GPU (N read-only populated options); `Nâng cao`: raw IDs and configs, guide restart worker; không show secret.

**Action:** E save `data/settings.toml`, validate adapter init without paid calls; N dropdown mapping uses selected IDs. Không tự bật paid adapter hoặc overwrite current active configuration. **Test:** valid/invalid configs, process restart notice, stale IDs, no passwords/API keys in HTML/logs, CSRF if security slice approved.

---

## Screen coverage / phạm vi tối thiểu để triển khai

| Slice | Màn hình | Mục tiêu |
|---|---|---|
| Fix-0 | S19, S13 và shared layout | Sửa lỗi title/monitor, nút không rõ chức năng, baseline |
| Slice-1 | S00–S04 | Người mới tạo và quản lý Brief → Task mà không cần JSON |
| Slice-2 | S08–S10 | Người mới tạo Bible + upload ảnh + duyệt sheet |
| Slice-3 | S13–S18 | Một thư viện, import 2 bước, chống trùng có cảnh báo, manifest web, run theo input contract |
| Slice-4 | S05–S07, S11–S12, S19–S20 | Tối ưu luồng trên giao diện đã có; pickers/readiness; regression |

## Nút vắng backend — quy tắc

Trong prototype mới, mọi CTA đang đề xuất (Create Brief, Save Brief, Import Preview, Web Approve Manifest, Rename/Archive Workflow, Pick GPU/Workflow) phải được đánh dấu N cho đến khi có route/service + test. **Không cho merge template render nút enabled** khi chưa có handler thật hoặc when permission/guard invalid. Không cần thêm editor đồ thị ComfyUI, đăng video TikTok/YouTube, scheduler, nhiều operator, marketplace, thanh toán mới.
