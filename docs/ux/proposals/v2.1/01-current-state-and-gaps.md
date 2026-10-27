# 01 — Review hiện trạng, khoảng trống & UX audit

## 1. Mục đích và giới hạn bằng chứng

Đánh giá **trải nghiệm người dùng không biết hệ thống** dựa trên code `main@7d80e075`. Đọc `studio/web/{__init__.py,views.py,templates/*,static/ui.js}`, `studio/contracts.py`, `studio/series.py`, `studio/comfyui/{workflows.py,manifest.py,service.py}`, `studio/cli.py`, và `docs/ux/{implementation,verification}.md`. Đây là **code/contract audit**, không phải screenshot audit của app đang chạy. Báo cáo PR #17 cho biết **290 pytest pass và 224 browser checks** ở thời điểm merge; đây là kết quả được ghi trong repo, **không phải kiểm chứng độc lập hôm nay**.

## 2. Kết luận

Giao diện đã có điều hướng, task lọc/tìm, Storyboard, Series Bible, GPU Rent, Workflows, Monitor. Thiếu *đường đi hoàn chỉnh* cho người mới: chưa có Brief library/editor; cách sử dụng ảnh tham chiếu thiếu walkthrough; các bước ComfyUI manifest/điều kiện GPU nằm rải rác ở CLI và Settings. Đồng thời thuật ngữ `brief_json`, `API export`, `SHA`, `manifest`, `ready` không có hướng dẫn hành động đơn giản ngay trong màn hình.

## 3. Danh sách phát hiện (ưu tiên)

| ID | Loại / mức | Bằng chứng ở main | Tác động người mới | Đề xuất |
|---|---|---|---|---|
| F01 | **Confirmed gap · P0** | `GET /` và `POST /tasks` chỉ liệt kê `briefs/*.json` hoặc nhận `brief_json`; không có route `/briefs` | Người mới không biết tạo/sửa/lưu brief | Thư viện Brief + builder thân thiện, preview/validate/lưu draft và tạo task từ brief |
| F02 | **Confirmed gap · P0** | `Brief` trong `studio/contracts.py` chỉ gồm id/title/hook/beats/twist/...; **không có ảnh** | Tưởng có thể gắn ảnh ngay vào Brief | Giải thích ảnh nhập qua Series/Storyboard; *tùy chọn sau*: liên kết ảnh có cấu trúc nếu có nhu cầu thật |
| F03 | **Confirmed gap · P0** | Tạo series chỉ name + from_task, nhập ảnh trong form của từng character | Chưa hiểu "tạo Bible", "tạo nhân vật", "ảnh sẽ dùng thế nào" | Setup checklist, CTA thêm nhân vật/ảnh rõ ràng, thẻ candidate → duyệt → sheet, nguồn/quyền |
| F04 | **Confirmed gap · P0** | `comfyui.html` upload GUI/API; `comfyui_workflow.html` yêu cầu manifest attach/approve CLI | Luồng nhập workflow bị chặn giữa chừng | Wizard import + dependency review web (mới; bắt buộc server validation/approval) |
| F05 | **Confirmed gap · P1** | `ComfyService.import_workflow()` tạo revision bất biến; UI chỉ import thêm revision và list revision | Khó phân biệt version nào mới/cũ/đang dùng | Revision compare metadata + impact/checks + trạng thái *active reference* (không làm in-place edit) |
| F06 | **Confirmed gap · P1** | Không thấy route đổi tên, ghi chú, archive, restore workflow trong `studio/web/__init__.py`; `ComfyService` không cung cấp lifecycle này | Thư viện khó quản trị khi đông workflow | Chọn tập tối thiểu: metadata edit + archive/restore (không delete vật lý trong MVP) |
| F07 | **Confirmed gap · P1** | Comfy Image settings yêu cầu nhập `rental_id`, `workflow_id`, `reference_workflow_id` dạng text | Người mới phải tự tra ID | Cho chọn rental + workflow approved qua select, giữ giá trị thực, không tự thay đổi mặc định |
| F08 | **Confirmed gap · P1** | Manifest GPU provision cũng được thêm/duyệt bằng CLI | Không thể thuê GPU từ web khi thiếu provisioning manifest | Guided setup: hiện blocker/đường đi; add/approve UI là lát cắt riêng có security review |
| F09 | **Confirmed code defect · P1** | `studio/web/templates/comfyui.html` khai báo `{% block title %}ComfyUI Workflows</section>` rồi chứa `<dialog>...` và `<details>...` trước `{% endblock %}`; bản tương ứng lặp lại trong `body` | Trang title bị nhiễm markup, dễ gây hỏng metadata/DOM tùy parser | Sửa block title thuần văn bản, giữ một dialog trong body; browser regression test |
| F10 | **Confirmed misleading action · P1** | `monitor.html` nút "Làm mới" dùng href `?refresh=off`; route GET `refresh=off` làm `auto_refresh` tắt | Nhãn nói refresh nhưng thực tế đồng thời tắt auto-refresh | Tách **Làm mới bây giờ** (GET current view) và **Tự cập nhật Bật/Tắt**; test cả hai |
| F11 | **Confirmed UX gap · P1** | `series.py` chấp nhận tối thiểu **1 ảnh**; UI gợi ý "chọn 2–3 góc" | Người dùng nghĩ phải có 2–3 ảnh | UI ghi "ít nhất 1 ảnh; khuyến nghị 2–3 góc"; kiểm trạng thái candidate/sheet |
| F12 | **Risk / needs repro · P1** | `series.py` upload_ref chỉ copy file và ghi sources, không thấy xác thực nội dung ảnh ở bước upload; validate khi approve_refs | Có thể giữ ảnh lỗi tới bước duyệt | Kiểm size/MIME/header trước lưu, giới hạn upload, test ảnh giả đuôi `.png`, large file |
| F13 | **Risk / needs repro · P1** | `studio/web/__init__.py` task/series/settings POST chưa có cùng CSRF check như GPU/ComfyUI, được ghi rõ trong docs/ux | Rủi ro khi bấm action trong môi trường truy cập qua web | Bổ sung test + CSRF cho POST theo phạm vi có cấu hình/auth; không tạo false sense of security |
| F14 | **UX opportunity · P2** | `comfyui_workflow` hiện dữ liệu SHA, model, custom nodes dạng bảng; không có kiểm tra có thể chạy theo ngôn ngữ dễ hiểu | Không biết bước nào phải làm tiếp | Checklist 4 gates: file API → dependency approved → GPU ready → compatible `/object_info` |
| F15 | **Confirmed gap · P1** | Workflow run không có cancellation, editor graph, publish API; mapping PR #17 xác nhận | Prototype có thể gợi ý nút mà code không hỗ trợ | **Không render** các nút giả: Cancel run, Edit graph, Publish, Resume GPU; nếu đề xuất phải có backend + spec/test riêng |
| F16 | **UX opportunity · P1** | Sửa Series Bible lưu history, nhưng UI không đưa ra phiên bản/chỗ quay lại | Sợ sửa hỏng nhân vật/giọng | Hiển thị last saved + history read-only; restore là tùy chọn sau |
| F17 | **Confirmed gap · P1** | Nút "Tạo task" từ modal chỉ accept brief_file hoặc **Brief JSON đầy đủ**, không có title-only | Mô hình mới không thể tạo nhanh video từ ý tưởng | New user wizard cần convert form -> valid Brief, validate server, preview trước khi enqueue worker |
| F18 | **UX opportunity · P1** | `comfyui` preview `note` và ID kỹ thuật, GPU preconditions tách 2 trang | Quá nhiều chuyển trang khi thực hiện workflow đầu tiên | "Check before run" có CTA nối đúng sang bước còn thiếu |

**Không coi mọi button là dead:** nút Tạo task, Tạo Series, Upload ảnh, Duyệt refs, Import workflow, Chạy workflow, Stop/Destroy đều có route thật. Vấn đề là *không đủ điều kiện, UX khó hiểu*, hoặc thiếu UI cho thao tác phía CLI. Danh mục nút riêng ở `05-action-inventory.md`.

## 4. Vì sao người mới vấp ở chặng đầu

- **Brief ≠ Task:** Brief là đặc tả đầu vào; Task là lượt chạy có thể tốn phí. Hiện UI ghép hai việc vào cùng modal.
- **Bible ≠ ảnh:** Bible là cấu hình series; ảnh thuộc từng nhân vật. Upload ảnh chỉ tạo candidate, cần chọn duyệt thì mới trở thành refs/sheet. Không được báo upload xong là sẵn sàng.
- **GUI JSON ≠ API JSON:** GUI chứa metadata để soi dependency; API dùng submit. Nhập GUI đơn lẻ có thể chưa runnable. API đơn lẻ không đủ metadata và cần manifest do người vận hành duyệt.
- **Workflow manifest ≠ GPU provisioning manifest:** Workflow manifest *không tự cài* model/custom nodes lên máy. Ready GPU còn cần `/object_info` tương thích. Không nói "Approved → can run" nếu thiếu điều kiện khác.
- **Stop ≠ Destroy:** GPU stopped vẫn có thể tính storage; chi phí chỉ dừng hẳn sau khi xác nhận destroyed.

## 5. Decisions trước khi code

| Quyết định | Mặc định khuyến nghị | Vì sao |
|---|---|---|
| D01 Brief | Thư viện file-based lưu dưới `data/briefs/` hoặc vùng dữ liệu được cấu hình, **không ghi đè** `briefs/` versioned của repo | Giữ brief seed trong source sạch, tránh sửa file Git từ web |
| D02 Ảnh Brief | Không thêm ảnh vào schema Brief v1 ngay; minh họa rõ luồng ảnh qua Series/Storyboard | Tránh đổi pipeline/contract vượt nhu cầu |
| D03 Workflow delete | Archive/restore metadata, **không hard-delete** revisions/runs/artifacts | Audit, provenance, an toàn dữ liệu |
| D04 Revision activate | Không thay đổi pinned IDs/revisions trong Settings ngầm; người dùng chọn và xác nhận | Tránh đổi pipeline đang chạy |
| D05 Manifest approval web | Phê duyệt có summary content SHA, license warning, confirmation, audit event; chỉ quyền operator hiện có | Đưa CLI vào UI nhưng giữ validation/service gốc |
| D06 First-use | Hướng dẫn theo ngữ cảnh; không onboarding modal bắt buộc mỗi lần | Giữ đường đi của người đã quen |

## 6. Kết luận triển khai

P0: sửa F09/F10 → Brief create/manage → Series beginner onboarding + ảnh → Workflow import/manage basics + web manifest review → readiness checklist; P1: rename/archive, Settings pickers, history; P2: các chức năng phức tạp như graph editor hoặc thông minh hóa dependency, **không nằm trong scope MVP**.

## 7. Bổ sung kiểm tra v2.1 — Motion, trùng lặp và ít thao tác

| ID | Mức / bằng chứng | Kết luận | Xử lý đề xuất |
|---|---|---|---|
| F19 | Confirmed gap · P1; `studio/steps/motion.py` gọi `ctx.adapter('video')`, `studio/providers/__init__.py` chỉ đăng ký `video.fake`, `studio.toml` đặt `video='none'` | `Scene.mode='motion'` **không phải** workflow ComfyUI Motion; hiện không thể chọn ComfyUI Motion trực tiếp cho scene | Một lựa chọn motion mode ở Storyboard, kết nối adapter video dùng chung ComfyService ở giai đoạn sau; không hứa hỗ trợ trước khi test E2E |
| F20 | Partially supported · `studio/comfyui/runtime.py` đọc history `videos`, `studio/comfyui/runs.py` nhận `.mp4/.webm/.mov` có magic check | Run ComfyUI có thể thu output video đúng định dạng, **nhưng** `start_run` không nhận input form/prompt/image/duration động; không thể khẳng định mọi motion graph chạy được | MVP chọn motion workflow đã duyệt với input contract rõ; kiểm `object_info`, media, output; lưu nonce/provenance |
| F21 | Confirmed gap · `ComfyService.import_workflow()` so source/API hashes **chỉ với latest của cùng ID**; `store.add_workflow_revision` không unique fingerprint toàn thư viện | **Chưa khử trùng** nhiều `workflow_id` hoặc revision lịch sử; chưa có xóa trùng. Không tự xóa vì còn references | Global hash lookup + cảnh báo `Đã tồn tại: ...` và CTA `Mở bản hiện có`; report dữ liệu trùng để operator quyết định archive (P1), không auto-merge |
| F22 | Confirmed defect · `comfyui.html` chứa dialog/details trong `{% block title %}` và lặp cùng id trong body | DOM bị lặp; **khác** duplicate data/workflow | Chỉ 1 dialog trong body, title thuần text, browser regression |
| F23 | UX simplification · bản đề xuất cũ dùng 5 bước import và màn hình riêng preflight | Quá nhiều lớp tương tác, khó học và maintain | 2-step import; input form ngay tại Workflow Detail; blocker hiện inline; technical details collapsed |

### Nguyên tắc tránh nhầm trùng

- **Trùng dữ liệu**: cùng fingerprint; không tạo workflow mới nếu người dùng chọn dùng lại. Cảnh báo trước insert, backend kiểm lại; race/khác tên không làm mất quyền chọn tạo bản độc lập khi có lý do rõ và cần lưu dấu.
- **Trùng revision**: import cùng content ở cùng workflow không thêm revision; thêm test cho import cùng nội dung sau các rev trung gian (không tự ghi đè lịch sử).
- **Trùng giao diện**: xóa block HTML/dialog lặp, không đụng SQLite.
- **Trùng logic**: `ComfyService` là entrypoint cho image & motion; input mapping khác nhau chỉ là data contract, không viết hai luồng import/run mới.
