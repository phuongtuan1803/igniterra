# IGNITERRA — UX v2.1 đề xuất (review tại main 7d80e075)

**Trạng thái:** SUGGESTION — CHƯA phải tài liệu thiết kế chính thức, CHƯA thay đổi GitHub.

**Baseline:** `phuongtuan1803/igniterra`, `main`, commit `7d80e07553f5749dca68d4fd192838ee9ee49330` (GitHub commit: 2026-10-26 22:52:19 UTC; Việt Nam: 2026-10-27 05:52:19). Bản review dựa trên repository, route, template, JS, service và các báo cáo test đã commit. **Chưa chạy ứng dụng hoặc test trực tiếp trong lượt review này.** Các bug UI cần được tái hiện bằng browser test trước khi sửa.

## Thay đổi v2.1 theo phản hồi của người dùng

**Ba quyết định mặc định (đề xuất, chưa implement):**

1. **Motion:** thêm lựa chọn **Ảnh tĩnh chuyển động nhẹ / AI Motion (ComfyUI) / Upload clip**. Trong `main`, `mode=motion` đã có nhưng `video` adapter chỉ có `fake`; việc kết nối ComfyUI Motion vào pipeline **chưa có**. ComfyUI độc lập đã đọc/kiểm một số định dạng output video. Phải thử E2E từng mẫu workflow video trước khi đánh dấu hỗ trợ.
2. **Chống trùng:** cùng (source SHA + API SHA) với revision **mới nhất trong cùng** `workflow_id` đã idempotent; không có dedup toàn thư viện hay hard-delete. Trước khi thêm workflow mới phải tìm cùng hash toàn thư viện và **đề xuất mở/dùng lại**; không tự xóa/sát nhập dữ liệu cũ, nhất là workflow có run hoặc pin trong Settings. Lỗi `comfyui.html` trùng markup dialog/title là vấn đề **khác** cần sửa.
3. **Đơn giản vận hành:** một Workflow Library chung cho image/motion; **Import tối đa 2 bước (Chọn file → Kiểm tra & Lưu)**; một form **Run** sinh từ input contract được khai báo cho revision (`prompt`, `image`, `duration`, tùy loại), còn JSON, node IDs, SHA, manifest nằm dưới Nâng cao. **Không tạo riêng hai runner/comfy service cho image và motion.** Không suy đoán input của workflow bất kỳ nếu chưa được mapping và duyệt.

**Phân kỳ:** P0 sửa markup + cải thiện import/revision/dedup-warning + luồng chạy workflow đã duyệt không nhập ID; P1 thêm schema binding input kiểu dữ liệu và Motion ComfyUI độc lập; P1 tiếp theo tích hợp `VideoProvider` với Storyboard khi có E2E image-to-video ổn định. Không tạo button giả.

## Đọc theo thứ tự

1. [`01-current-state-and-gaps.md`](01-current-state-and-gaps.md): hiện trạng, vấn đề đã xác minh, giả thuyết UX, danh sách khả năng chưa có, audit các nút.
2. [`02-beginner-user-journeys.md`](02-beginner-user-journeys.md): luồng E2E dành cho người không biết JSON/CLI/GPU; mục tiêu, đường đi ngắn, nhánh lỗi và điều kiện hoàn thành.
3. [`03-screen-specifications.md`](03-screen-specifications.md): đặc tả UX/UI từng màn hình, dữ liệu/field, hành động, trạng thái, API, acceptance criteria.
4. [`04-engineering-test-plan.md`](04-engineering-test-plan.md): kế hoạch code theo lát cắt, test matrix, bảo mật/chi phí, điều kiện dừng và handoff Codex.
5. [`05-action-inventory.md`](05-action-inventory.md): mapping nút ↔ route ↔ service và đánh giá có/hay chưa có tác dụng.
6. [`diagrams/`](diagrams/): 4 diagram nguồn **D2**, hướng từ trên xuống (`direction: down`), nhãn tiếng Anh. Dùng Report Kit / D2 CLI trong repo để render sau khi duyệt; bộ này không giả nhận đã render.

## Nguyên tắc

- **Mục tiêu:** người lần đầu dùng có thể tạo brief, series, nhập ảnh, tạo task, duyệt kết quả, nhập/quản lý workflow, chọn GPU và xuất output **không cần tự soạn JSON hoặc dùng CLI cho đường đi cơ bản**.
- **Không làm quá:** giữ FastAPI + Jinja2 + HTMX, dùng lại `Actions`, `SeriesActions`, `ComfyService`, `GPUService`, Store, các route sẵn có. Chỉ thêm route/service khi có use case thực sự thiếu.
- **Không xóa logic bảo vệ:** mọi hành động có phí, thay đổi quyền/manifest, xóa dữ liệu, GPU destroy, trạng thái unknown phải được xác nhận và kiểm tra backend; không được giả lập thành công.
- **Tách:** `Confirmed (code)` ≠ `Needs live repro` ≠ `Proposed`.
- **Review trước:** không commit, push hay PR khi chưa có xác nhận.

## Nguồn chuẩn

- [Source repository](https://github.com/phuongtuan1803/igniterra/tree/7d80e07553f5749dca68d4fd192838ee9ee49330)
- [UI implementation mapping](https://github.com/phuongtuan1803/igniterra/blob/7d80e07553f5749dca68d4fd192838ee9ee49330/docs/ux/implementation.md)
- [Existing UI test evidence](https://github.com/phuongtuan1803/igniterra/blob/7d80e07553f5749dca68d4fd192838ee9ee49330/docs/ux/verification.md)
- [Templates/routes](https://github.com/phuongtuan1803/igniterra/tree/7d80e07553f5749dca68d4fd192838ee9ee49330/studio/web)
