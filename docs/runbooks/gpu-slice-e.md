# Slice E: guardrail và lượt thuê GPU thật

Phần code có test tự động bằng provider và ComfyUI giả, không tốn phí. Lượt thuê thật là kiểm chứng tích hợp có chi phí, chỉ thực hiện trong mức trần người vận hành đã duyệt. Kết quả thực tế và hạn chế được ghi trong [báo cáo G4](../reports/g4/README.md).

## Kiểm tra trước khi thuê

Chạy từ checkout chứa Slice E:

```sh
uv sync
uv run pytest -q
```

`studio.toml` mặc định tắt GPU. Dùng file cấu hình riêng qua `STUDIO_CONFIG`, cùng một `data_dir` cho web, worker và CLI. Chọn thư mục dữ liệu riêng cho lượt thử để worker không chạy các task sản xuất còn pending. `.env` tại thư mục dự án phải có `VAST_API_KEY`; không in key, `WEB_PASSWORD`, nội dung `/capabilities.credentials` hay toàn bộ request tạo máy vào báo cáo.

1. Đọc `GET /instances/` và đối chiếu các rental cũ trước khi tạo. Nếu có create/destroy chưa rõ, reconcile trước; không tạo lại bằng nonce cũ.
2. Xác minh image tồn tại và ghim digest Linux/amd64. Ghim ComfyUI bằng commit 40 hex; model phải có URL Hugging Face ghim commit, dung lượng và SHA-256 đúng.
3. Với kết nối HTTPS trực tiếp, tải CA từ `https://console.vast.ai/static/jvastai_root.cer` qua TLS đã xác thực, lưu riêng và đặt `[gpu.portal] verify_tls = "/đường/dẫn/tuyệt/đối/vast-root.pem"`. Không tắt kiểm TLS. Có thể dùng CA phù hợp khác nếu endpoint dùng chứng chỉ do CA đó cấp.
4. Tìm offer verified có đủ disk, VRAM, CUDA; kiểm thêm phí tải lên/xuống. Giá compute + disk là ước tính; bandwidth chưa nằm trong guardrail.

Cấu hình mẫu cho một lượt thử được duyệt tối đa 2 USD:

```toml
data_dir = "data/gpu-live"

[gpu]
provider = "vast"
image = "vastai/pytorch@sha256:<digest đã xác minh>"
quote_ttl_s = 300
provision_timeout_s = 900
guard_interval_s = 10

[gpu.limits]
idle_ttl_minutes = 5
max_runtime_minutes = 20
max_hourly_usd = 0.35
max_total_usd = 1.5

[gpu.portal]
scheme = "https"
status_path = "/capabilities"
verify_tls = "/đường/dẫn/tuyệt/đối/vast-root.pem"
timeout_s = 8
```

Trần 1,50 USD mỗi rental chừa khoảng cho bandwidth và độ trễ hạch toán, **không bảo đảm hóa đơn dưới 2 USD**. Chỉ thử một instance tại một thời điểm, theo dõi cả số dư provider và đồng hồ tường; hủy sớm nếu nạp image hoặc provisioning kéo dài. Không đặt tự nạp thêm tiền cho lượt thử.

## Thứ tự chạy

1. Tạo manifest provisioning JSON từ commit đã xác minh. Với smoke `EmptyImage → SaveImage`, không cần model/custom node. Ví dụ dùng commit của lượt thử trong báo cáo:

   ```sh
   uv run python - <<'PYTHON'
   from pathlib import Path
   from studio.gpu.provisioning import build_manifest
   manifest = build_manifest("cdf272f3514353aab04dc2d9fceef57a260dc156", max_retries=0)
   Path("/tmp/gpu-smoke-manifest.json").write_text(manifest.model_dump_json(indent=2))
   PYTHON
   uv run python -m studio gpu manifest add /tmp/gpu-smoke-manifest.json
   ```

   Đọc YAML và SHA in ra, rồi chạy `uv run python -m studio gpu manifest approve SHA`. Không dùng commit giả trong fixture test.
2. Mở web và worker ở hai terminal có cùng `STUDIO_CONFIG` và thư mục gốc:

   ```sh
   uv run python -m studio web --host 127.0.0.1 --port 8080
   uv run python -m studio worker --skip-preflight
   ```

   `--skip-preflight` phù hợp thư mục dữ liệu thử không có task video. Worker thường tự chạy guard; `worker --once` và `--task` không chạy guard. Giữ terminal worker mở trong suốt lượt thuê.
3. Tại `/gpu`, tìm lại offer và xác nhận báo giá trước khi Rent. Ghi lại rental ID và instance ID. Nếu `create_failed`, kiểm provider xác nhận không có instance; nếu `create_unknown`, chỉ Reconcile theo label, không gửi create lại.
4. Theo dõi timeline hoặc chạy `uv run python -m studio gpu poll`. Host `running` chưa phải `ready`. Portal/ComfyUI phải từ chối request không token; Portal phải báo `provisioning.status = complete`; `/system_stats` và smoke phải thành công. Manifest có model còn phải kiểm đủ SHA-256.
5. Nhập workflow API ở `/comfyui/workflows`, gắn và duyệt manifest dependency, chạy trên rental `ready`. Với smoke không model, duyệt manifest workflow rỗng sau khi xem JSON. Poll ở trang run hoặc `uv run python -m studio comfyui poll` cho đến khi mọi output đã tải và run `succeeded`. Ghi bytes, SHA-256 và loại media của output.
6. Kiểm G11 bằng lỗi tải được mô phỏng **tại client**, giữ nguyên output thật trên instance. Rental vượt giới hạn nhưng còn run `output_failed` phải tiếp tục tồn tại và hiện cảnh báo; guard không được destroy. Dùng Tải lại output hoặc `comfyui retry-download RUN_ID`; kiểm output rồi mới tiếp tục hủy. Không mô phỏng lỗi bằng cách làm hỏng dữ liệu trên provider.
7. Kiểm khởi động lại và idle: sau khi output đã kiểm, khởi động lại worker, để rental hết idle TTL. Guard phải gửi destroy một lần cho instance đúng provider + label. Nếu mô phỏng mất response DELETE tại client sau request thật, phải ghi rõ đó là lỗi được chèn, không phải sự cố provider.

Có thể thực thi riêng giới hạn bằng `uv run python -m studio gpu enforce-limits`; lệnh in rental vượt giới hạn, kết quả destroy hoặc các run đang chặn theo G11. Mỗi lượt guard có thể trễ hơn chu kỳ cấu hình vì chờ HTTP/SQLite.

## Dừng và xác nhận dọn sạch

- Khi đạt mục tiêu, sắp chạm trần hoặc provisioning hỏng, dùng Destroy ở trang rental. Nếu còn output chưa kiểm, tải trước hoặc xác nhận bỏ output; hành động bỏ được ghi thành `abandoned` trong cùng giao dịch destroy.
- `destroying` và `destroy_unknown` vẫn có thể tính phí. Reconcile tới khi `GET instance` và danh sách provider đều không còn ID đó. Không gửi lại mutation khi kết quả chưa rõ; nếu provider từ chối chắc chắn thì kiểm lại trước khi thao tác tay.
- Kiểm tất cả instance có label `igniterra:`. Orphan chỉ được cảnh báo, không tự hủy; xác định chủ sở hữu trước khi xử lý. Không đụng instance không thuộc lượt thử.
- Lưu thời gian bắt đầu/kết thúc, image digest, commit, giá offer, phí bandwidth, ID, trạng thái từng bước, hash output, số request destroy, trạng thái cuối và mức giảm số dư. Số dư có thể cập nhật trễ; ước tính của studio không phải hóa đơn.

## Giới hạn đã biết

- G11 có thể giữ máy chạy vượt giới hạn để tránh mất output. Cần người vận hành xử lý cảnh báo.
- `stopped` chỉ dừng compute; disk còn tính phí và không nằm trong tự động thu hồi của Slice E.
- `/capabilities` chỉ báo trạng thái provisioning tổng thể. Bước trong timeline không có số đo byte nếu Portal không cung cấp; log upstream ở `/var/log/portal/provisioning.log` cần kiểm riêng khi chẩn đoán.
- Các test checksum giả lập kiểm hash thiếu/lệch/đúng. Smoke không model không chứng minh đã tải hoặc chạy thành công model lớn như FLUX/Wan.
- Lát E đã kiểm báo giá Vast có `dph_total = dph_base + storage_total_cost`; offer mới được đánh dấu đã gồm disk, không cộng hai lần. Snapshot cũ/provider chưa khai báo giữ cách cộng disk bảo thủ. Vẫn phải đối chiếu hóa đơn và bandwidth.
