# Tài liệu thiết kế Igniterra

Bộ tài liệu thiết kế của hệ thống sản xuất video AI (core tối thiểu + adapter, tái dùng MoneyPrinterTurbo), đi từ thiết kế hệ thống xuống thiết kế kiến trúc. Nguồn gốc: tài liệu ADD-AIVIDEO-003 v3.0, bản hợp nhất (thay thế ADD-001 v1.1). Bộ tài liệu dùng xuyên suốt quá trình phát triển: khi code đi lệch tài liệu, sửa tài liệu sở hữu sự thật đó trong cùng PR.

| Tài liệu | Nội dung |
| --- | --- |
| [01 · Thiết kế hệ thống](src/design/01-system-design.md) | Bối cảnh, mục tiêu, phạm vi, mô hình miền, yêu cầu (FR, NFR), ràng buộc, thuộc tính chất lượng |
| [02 · Kiến trúc tổng thể](src/design/02-architecture.md) | Nguyên tắc, 4 tầng, góc nhìn process và thành phần, quy tắc phụ thuộc, công nghệ, AD-01..AD-13, truy vết |
| [03 · Worker và luồng thực thi](src/design/03-runner.md) | Danh sách step, vòng lặp worker, trạng thái task, StepRun, paid call, gate, retry, đồng bộ thời lượng |
| [04 · Contract và dữ liệu](src/design/04-data-contracts.md) | SQLite, contract, ScenePlan revision, candidates/selected, sổ chi phí, Series Bible, cấu hình |
| [05 · Provider, adapter và MPT](src/design/05-modules-providers.md) | 5 interface, ranh giới MPT, adapter, model mặc định, agent, quy trình khác, kiểm thử |
| [06 · Giao diện review và CLI](src/design/06-web-ui.md) | Bản đồ trang, Storyboard, can thiệp, Task detail, lệnh CLI |
| [07 · Triển khai và vận hành](src/design/07-operations.md) | Triển khai, bí mật, nâng cấp MPT, quan sát, sao lưu, bảo mật, rủi ro, runbook |
| [08 · Lộ trình theo decision gate](src/design/08-roadmap.md) | Gate 2–4, test T01–T12, thứ tự triển khai GPU thuê, quyết định còn mở |
| [09 · GPU thuê và ComfyUI](src/design/09-gpu-comfyui.md) | Gate 4: GPUProvider, Vast.ai, provisioning, ComfyUI workflow, guardrail và G01–G12 |
| [Runbook Slice E](runbooks/gpu-slice-e.md) | Kiểm thử, thuê thật trong trần chi phí, xác nhận destroy và giới hạn còn lại |
| [ADR](src/adr/index.md) | Sổ quyết định; 0001–0003 bị loại theo ADD-003, 0004–0006 đã duyệt |

## Báo cáo pilot

- [Gate 2: core, CLI và 5 brief ra MP4](reports/g2/README.md) — 5 MP4 đạt hậu điều kiện; phiếu duyệt C01–C09 và D-07 chờ Tuan.
- [Gate 3: trang review, Series Bible, T09](reports/g3/README.md) — bắt đầu trước D-07; character sheet giữ nhân vật 5/5 scene; điều kiện ra chờ số đo Gate 2.
- [Gate 4: ComfyImage và FLUX.2 Klein 4B](reports/g4-comfy-image/README.md) — 5 keyframe trên GPU thật và MP4 53 giây; review sơ bộ 2/5 ảnh đạt nội dung, chờ duyệt cuối; có Monitor task/run/rental.

Báo cáo là ảnh chụp tại thời điểm viết; trạng thái hiện hành nằm ở [08 · Lộ trình](src/design/08-roadmap.md). Chạy bộ kiểm thử bằng `uv run pytest`.

## Cách viết và build

Tài liệu là Markdown của [Report Kit](https://github.com/phuongtuan1803/report-kit); diagram là khối `d2`, file SVG nằm trong thư mục `*.figs/` cạnh nguồn và được commit cùng Markdown. Không sửa tay SVG hay HTML.

Bản Report Kit dùng để viết bộ này: commit `cefd883`. Mã D2 nằm trong comment `<!-- report-kit:d2 ... -->` để GitHub Preview chỉ hiện SVG; xem mã ở chế độ Code/Raw. Sau khi thêm khối d2 mới, chạy `report id` để bọc mã và ghi thẻ `<picture>`.

```sh
git clone https://github.com/phuongtuan1803/report-kit && cd report-kit
git checkout cefd883 && uv sync && npm ci
uv run report check  ../igniterra/docs/src/design ../igniterra/docs/src/adr   # kiểm nguồn, SVG cập nhật (thêm --browser để soát bố cục)
uv run report build  ../igniterra/docs/src/design ../igniterra/docs/src/adr   # SVG + HTML vào docs/html/
uv run report adr-index ../igniterra/docs/src/adr                           # sau khi sửa ADR
```

Quy ước:

- Mỗi tài liệu một ngôn ngữ (tiếng Việt); thuật ngữ kỹ thuật giữ tiếng Anh như trong code.
- Đổi nội dung: tăng `ver` và thêm một dòng `rev` trong front matter.
- Quyết định khó đảo ngược: viết ADR mới trong `src/adr/` (`report new adr src/adr/NNNN-slug.md --id ADR-NNNN --title "..."`), rồi chạy `report adr-index`.
- Yêu cầu (FR, NFR, CON) chỉ định nghĩa ở 01; tài liệu khác dẫn lại bằng khối `req`.
- `docs/html/` là đầu ra build, không commit.
