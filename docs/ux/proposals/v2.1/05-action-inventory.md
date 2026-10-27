# 05 — Action inventory: button/link ↔ chức năng thực tế

**Snapshot:** `main` at `7d80e075`. Đây là **static evidence** từ template `studio/web/templates/`, `studio/web/static/ui.js`, route ở `studio/web/__init__.py`, services. Không kết luận một button "dead in browser" nếu chưa có reproduction; `Có route` không có nghĩa use case người mới thông suốt.

**Legend**: `E` = action/route đã tồn tại, `P` = presentation link / JS, `N` = chưa có action tương ứng / đề xuất, `R` = cần test browser/repro. **Critical:** chỉ hiển thị nút `N` khi backend hoàn tất.

| ID | Màn hình, nút hoặc control | Endpoint / script | Phân loại | Kết luận và hành động |
|---|---|---|---|---|
| A01 | Tasks → Tạo task (mở dialog) | `data-open-dialog=new-task` → `ui.js` | P/E | Mở dialog hợp lệ với JS; không phải tạo Brief. Nên đưa bước Brief Builder trước; test no-JS fallback. |
| A02 | Tasks → Chọn brief | `<select brief_file>` đọc `briefs/*.json` | E | Chọn được file mẫu, nhưng **không có nút Tạo Brief mới**. |
| A03 | Tasks → Dán Brief JSON → Tạo task | `POST /tasks` → `Actions.create_task` | E | Backend tạo được task nếu đúng schema; người mới khó soạn JSON. |
| A04 | Tasks → Search/filter | GET `/` query `q,status` | E | Handler tồn tại; kiểm empty/focus/query. |
| A05 | Task → Mở Storyboard / Logs | GET `/tasks/{id}`, `/detail` | E | Link thật. |
| A06 | Storyboard → Chọn candidate | POST `.../select` → `Actions.select` | E | Ứng viên được chọn, cần trạng thái selected rõ. |
| A07 | Storyboard → Lưu rev | POST `.../edit` → `Actions.edit_scene` | E | Tạo Plan revision; form JS disabled khi không thay đổi. |
| A08 | Storyboard → Upload và chọn ảnh/audio/clip | POST `.../upload` → `Actions.upload` | E | Source/rights required; không thêm vào Series Bible. |
| A09 | Storyboard → Chạy lại | POST `.../rerun` → `Actions.rerun` | E | Có thể phát sinh chi phí, cần confirm và trạng thái worker. |
| A10 | Storyboard → Duyệt / Từ chối keyframes | POST `.../gate` → `Actions.decide` | E | Gated, không phải nút giả; chỉ hiện trạng thái hợp lệ. |
| A11 | Storyboard → Ghi phiếu | POST `.../review` → `Actions.review` | E | 9 checklist C01–C09, KHÔNG nên bỏ. |
| A12 | Storyboard → Duyệt cuối | POST `.../gate` | E | Tách khỏi ghi phiếu; xác nhận có phí nếu queue tiếp. |
| A13 | Task detail → Retry/Skip Unknown | POST `/tasks/{id}/unknown/{scene}` | E | Có rủi ro bị tính tiền lần nữa; không tự retry. |
| A14 | Task → Pause/Resume/Cancel | POST `/tasks/{id}/{action}` | E | Route backend thật; trạng thái hiển thị theo điều kiện legal. |
| A15 | Series → Tạo series | POST `/series` → `SeriesActions.create/create_from_task` | E | Tạo Bible mặc định, **không** tạo nhân vật/ảnh tự động. |
| A16 | Series → Lưu Bible | POST `/series/{name}/update` | E | Có snapshot history trong filesystem; UI chưa mô tả rõ. |
| A17 | Series → Thêm/Lưu nhân vật | POST `/series/{name}/characters` | E | Backend `set_character`; cần tách hướng dẫn key/description. |
| A18 | Series → Upload ảnh | POST `.../characters/{key}/upload` → `SeriesActions.upload_ref` | E+R | Có lưu candidate + source/rights. Cần test corrupt image, size, large upload. |
| A19 | Series → Tạo ảnh AI | POST `.../characters/{key}/ref` → `request_ref` | E | Tạo job chờ worker, có thể trả phí; **không** lập tức có ảnh. |
| A20 | Series → Duyệt ảnh và dựng sheet | POST `.../characters/{key}/approve` → `approve_refs` | E | Có FFmpeg ghép sheet, ≥1 ảnh; UI gợi ý 2–3 ảnh chỉ là khuyến nghị. |
| A21 | ComfyUI → Nhập workflow (dialog) | `data-open-dialog=import-workflow` → `ui.js` | P/E+R | Handler JS có; `comfyui.html` có **markup lặp bị nhét vào `block title`**; sửa bug và test title. |
| A22 | ComfyUI → Nhập JSON | POST `/comfyui/workflows` → `ComfyService.import_workflow` | E | Có tạo revision bất biến; auto-detect GUI/API; cần hướng dẫn thêm file API. |
| A23 | ComfyUI → Nhập profile FLUX | POST `/comfyui/image-profile` | E | Nhập workflow + manifest chờ duyệt, không thuê GPU/tải model. |
| A24 | Workflow → Nhập revision mới | POST `/comfyui/workflows` với `workflow_id` | E | Revision tăng nếu nội dung khác, không phải edit in-place. |
| A25 | Workflow → Chọn revision cũ | GET `/comfyui/workflows/{id}?revision=N` | E | Xem được; **không có nút compare**. |
| A26 | Workflow → Gắn dependency manifest | CLI `studio comfyui manifest attach`, service `.attach_manifest` | **N web** | Thao tác core có, **không có web POST**; cần UI + route mới. |
| A27 | Workflow → Duyệt manifest | CLI `studio comfyui manifest approve`, service `.approve_manifest` | **N web** | Cần web approve có license, hash, CSRF, audit; không thay bằng client-only nút. |
| A28 | Workflow → Chạy | POST `/comfyui/workflows/{id}/runs` → `.start_run` | E | Chỉ khi approved + API runnable + GPU ready; runtime object_info kiểm cuối. |
| A29 | Run → Kiểm tiến độ | POST `/comfyui/runs/{id}/refresh` → `.refresh` | E | Có một lượt poll, không tạo run mới. |
| A30 | Run → Tải lại output | POST `/comfyui/runs/{id}/retry-download` → `.retry_download` | E | Có, hạn mức retry; chỉ show output verified. |
| A31 | Workflow → Rename, Update note | Không thấy web handler/service tương ứng | **N** | Cần metadata-only revision-independent service và tests. |
| A32 | Workflow → Archive/Restore | Không thấy handler/status | **N** | Chỉ soft archive; KHÔNG xóa runs/manifests/files. |
| A33 | Workflow → Compare revisions | Không có trong UI | **N / P** | Diff metadata dựa trên recorded revision fields; không graph editor. |
| A34 | Workflow → Edit graph / Run cancel | Không có route/service | **N / out-of-scope** | Không cho nút enabled; không đưa vào MVP. |
| A35 | GPU → Tìm offer | GET `/gpu/offers` → `gpu.search_offers` | E | Search explicit, không tự search khi GET list. |
| A36 | GPU → Thuê | POST `/gpu/instances` → `.rent` | E | Quote, CSRF, nonce, confirm, budget, manifest guards. |
| A37 | GPU → Reconcile | POST `.../reconcile` | E | Có; không giả lập thành destroyed/ready. |
| A38 | GPU → Stop | POST `.../stop` | E | Nếu provider hỗ trợ; stopped vẫn có disk billing. |
| A39 | GPU → Destroy | POST `.../destroy` | E | Confirm và `accept_unverified` khi còn output chưa kiểm; chỉ destroyed sau provider confirms. |
| A40 | GPU → Resume/start instance | Không thấy web action | **N / out-of-scope** | Không hiển thị action giả. |
| A41 | Monitor → `Làm mới` | GET `/monitor?...&refresh=off` | **E nhưng nhãn sai** | **Tắt auto-refresh đồng thời reload**; đổi tên/route để tránh nhầm. |
| A42 | Monitor → `Tạm dừng cập nhật` | GET `?refresh=off` | E | Hoạt động theo contract; giữ query task. |
| A43 | Monitor → `Xử lý` | GET Task/GPU/Run detail | E | Link thật; mỗi alert có next-action. |
| A44 | Settings → `Validate & save` | POST `/settings` → `studio_config.write_settings` | E | Chỉ validation khởi tạo, không gọi paid provider, worker restart notice. |
| A45 | Settings → Comfy workflow ID fields | Input text `rental_id`, `workflow_id`, `reference_workflow_id` | E+UX gap | Có lưu config nhưng cần picker + label rõ, validate object references. |
| A46 | Brief → Create/Save/Edit/Delete | Không có `GET/POST /briefs` route | **N** | Cần Brief Library + Builder + safe storage; không đưa nút enabled trước backend. |

## Regression audit cần bổ sung

1. Parse HTML `title` tại `/comfyui/workflows` với real template: `<title>` chỉ có `ComfyUI Workflows · Igniterra`, **không chứa** `<dialog>`, `<form>`, `<section>`; đúng một `#import-workflow` trong `body`.
2. Monitor: GET `refresh=off` tắt auto-refresh; nút "Làm mới bây giờ" không được làm mất trạng thái toggle; query task giữ nguyên.
3. Series reference upload file giả PNG và ảnh cỡ lớn; xác nhận rejected sớm, không sinh candidate/sources sai.
4. Dialog/modal accessibility keyboard, no-JS alternative, filter/error focus.
5. Explicitly inspect every `<button>` `form action` / JS handler using fixture and assert a real transition or an actionable blocker, không chỉ HTTP 200.

## Action inventory bổ sung v2.1 (chưa có trong main)

| ID | UX Control | Trạng thái main@7d80e075 | Quy tắc code/test |
|---|---|---|---|
| A47 | Workflow → Chọn loại Image / Motion | **N** | Metadata revision user-confirmed; không tự suy ra khả năng chạy từ tên |
| A48 | Storyboard → Chọn ComfyUI Motion Workflow | **N** | Không render enabled trước khi có VideoProvider ComfyUI và motion input/output verified |
| A49 | Workflow Import → Kiểm tra file | **N web preview, E parsers** | Validate không ghi DB, chuẩn hóa lỗi, giới hạn file |
| A50 | Workflow Import → Phát hiện trùng toàn thư viện | **N** | So source/api SHA toàn bộ revisions, thông báo và dùng lại; không xóa |
| A51 | Workflow Import → Trùng latest của same ID | **E** | `ComfyService.import_workflow` đã idempotent; test không thêm revision |
| A52 | Workflow Run → Form input prompt/image/duration | **N** | Chỉ hiện field được binding ở revision; server validates và bind execution graph |
| A53 | Workflow Run → Select ready GPU + Run | **E (fixed graph)** | Hiện có; giữ CSRF, confirm, CAS và `/object_info`, input động chưa hỗ trợ |
| A54 | Workflow Run → Preview video đã kiểm | **Partially E / N UI** | Runtime nhận video output, trang run chưa có `<video>` preview; tải verified đã có |
| A55 | Workflow Library → Delete duplicates | **N / không tự động** | Chỉ lập báo cáo trùng, archive thủ công nếu được duyệt; không phá refs/run |
| A56 | ComfyUI Import Dialog | **Confirmed markup duplication** | Loại markup trong title, đúng 1 dialog ID; test browser |
| A57 | Workflow → Run with default inputs | **E** | Fixed `start_run` hiện có; form đơn giản phải giữ đường đi không cần mapping |
