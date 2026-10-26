# FLUX.2 Klein 4B distilled · keyframe profile

Profile cho `ComfyImage`, dựa trên [workflow chính thức của ComfyUI](https://docs.comfy.org/tutorials/flux/flux-2-klein). Nguồn và commit template ở [sources.json](sources.json); ba file model có URL ghim revision, dung lượng, SHA-256 và license ở [manifest.json](manifest.json). Không cần custom node.

- [text2image.json](text2image.json): text → một PNG.
- [reference.json](reference.json): một character sheet → `VAEEncode` → `ReferenceLatent` cho cả positive/negative conditioning. Đầu ra theo kích thước keyframe, không lấy tỉ lệ của sheet.
- [gpu-manifest.json](gpu-manifest.json): cùng model/dependency để provisioning rental; cần duyệt riêng với manifest workflow.

Dùng model distilled FP8, Qwen3 4B và VAE của Klein; 4 bước Euler, CFG 1. Adapter làm tròn kích thước lên bội số 16: `1080x1920` thành `1088x1920`, vẫn đạt hậu điều kiện keyframe. Mỗi request giữ seed, graph, manifest và output đã kiểm trong SQLite; bước render crop về 1080×1920. Không coi ảnh đạt nội dung chỉ vì đã tải được.

## Nhập và duyệt

```sh
uv run python -m studio comfyui import-image-profile workflows/flux2-klein-4b
uv run python -m studio gpu manifest add workflows/flux2-klein-4b/gpu-manifest.json
```

Đọc file JSON/YAML và đối chiếu nguồn model trước khi chạy `comfyui manifest approve SHA` và `gpu manifest approve SHA` với SHA tương ứng. Nút **Nhập profile FLUX** trên trang ComfyUI chỉ nhập workflow/manifest, không duyệt, thuê GPU hay tải model.

Sau khi rental đã ready, chọn `[adapters] image = "comfyui"` và đặt:

```toml
[comfyui.image]
rental_id = "r_<12 hex>"
workflow_id = "wf_<ID text2image>"
revision = 1
reference_workflow_id = "wf_<ID reference>"
reference_revision = 1
model = "FLUX.2-klein-4b-fp8"
timeout_s = 600
poll_s = 2
```

Settings trên web có ba trường ID và lựa chọn adapter; dùng TOML khi cần ghim revision khác. Khởi động lại worker sau khi đổi cấu hình. GPU cần được thuê và chọn riêng, adapter không tự thuê máy.

## Vận hành và giám sát

- `/monitor`: bảng read-only cập nhật mỗi 5 giây, nối task/scene ↔ run ↔ rental, chi phí ước tính và cảnh báo output chưa kiểm. Không gọi provider từ GET monitor.
- `/comfyui/runs/{id}`: graph/revision và trạng thái đã lưu, preview output đã kiểm, tải lại output khi lỗi. Keyframe trên Storyboard có link về run.
- Timeout sau submit: task chờ `unknown_call`; xem run đã lưu thay vì gửi lại. Cùng attempt được nối vào cùng run và không submit lần hai. Thay prompt/refs trong cùng attempt bị từ chối; người vận hành cần tạo attempt mới.
- Ảnh tham chiếu được upload với tên SHA-256; chỉ PNG/JPEG/WebP, tối đa 64 MiB. Một sheet mỗi lần gọi. Adapter không bỏ qua refs nếu thiếu workflow tương ứng.
- GPU tính phí theo thời gian thuê; `costs.jsonl` của task ghi lượt gọi và lượng ảnh, không gán giả USD/ảnh. Đo chi phí của một pilot trên toàn bộ rental rồi chia cho số ảnh đạt, ghi rõ người chấm và độ trễ hạch toán.
