# Tài liệu thiết kế Igniterra

Bộ tài liệu thiết kế của hệ thống sản xuất nội dung AI dạng module (Architecture D), đi từ thiết kế hệ thống xuống thiết kế kiến trúc. Nguồn gốc: tài liệu ADD-AIVIDEO-001 v1.1 (19/10/2026). Bộ tài liệu dùng xuyên suốt quá trình phát triển: khi code đi lệch tài liệu, sửa tài liệu sở hữu sự thật đó trong cùng PR.

| Tài liệu | Nội dung |
| --- | --- |
| [01 · Thiết kế hệ thống](src/design/01-system-design.md) | Bối cảnh, mục tiêu, phạm vi, mô hình miền, yêu cầu (FR, NFR), ràng buộc, thuộc tính chất lượng |
| [02 · Kiến trúc tổng thể](src/design/02-architecture.md) | Nguyên tắc, góc nhìn process và thành phần, quy tắc phụ thuộc, hybrid, công nghệ, truy vết yêu cầu |
| [03 · Pipeline Runner](src/design/03-runner.md) | Vòng lặp runner, trạng thái, fan-out, checkpoint, retry, gate, ngân sách, song song |
| [04 · Contract và dữ liệu](src/design/04-data-contracts.md) | SQLite, contract nghiệp vụ, Series Bible, cấu hình, artifact store, version |
| [05 · Module và provider](src/design/05-modules-providers.md) | Mô hình module, danh mục, provider và model, router, agent, quy trình khác, kiểm thử |
| [06 · Giao diện web](src/design/06-web-ui.md) | 4 trang, Task detail, Storyboard và sửa output, Task API |
| [07 · Triển khai và vận hành](src/design/07-operations.md) | Triển khai, bí mật, quan sát, sao lưu, bảo mật, mở rộng, rủi ro, runbook |
| [08 · Lộ trình](src/design/08-roadmap.md) | 5 phase, thứ tự công việc Phase 1, ví dụ kiểm chứng đầu-cuối |
| [ADR](src/adr/index.md) | Sổ quyết định kiến trúc; 3 quyết định cần chốt trước Phase 1 |

## Cách viết và build

Tài liệu là Markdown của [Report Kit](https://github.com/phuongtuan1803/report-kit); diagram là khối `d2`, file SVG nằm trong thư mục `*.figs/` cạnh nguồn và được commit cùng Markdown. Không sửa tay SVG hay HTML.

Bản Report Kit dùng để viết bộ này: commit `966434a`.

```sh
git clone https://github.com/phuongtuan1803/report-kit && cd report-kit
git checkout 966434a && uv sync && npm ci
uv run report check  ../igniterra/docs/src/design ../igniterra/docs/src/adr   # kiểm nguồn, SVG cập nhật
uv run report build  ../igniterra/docs/src/design ../igniterra/docs/src/adr   # SVG + HTML vào docs/html/
uv run report adr-index ../igniterra/docs/src/adr                           # sau khi sửa ADR
```

Quy ước:

- Mỗi tài liệu một ngôn ngữ (tiếng Việt); thuật ngữ kỹ thuật giữ tiếng Anh như trong code.
- Đổi nội dung: tăng `ver` và thêm một dòng `rev` trong front matter.
- Quyết định khó đảo ngược: viết ADR mới trong `src/adr/` (`report new adr src/adr/NNNN-slug.md --id ADR-NNNN --title "..."`), rồi chạy `report adr-index`.
- Yêu cầu (FR, NFR, CON) chỉ định nghĩa ở 01; tài liệu khác dẫn lại bằng khối `req`.
- `docs/html/` là đầu ra build, không commit.
