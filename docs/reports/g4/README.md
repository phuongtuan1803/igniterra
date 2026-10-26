# Gate 4 · Slice E

Kiểm chứng ngày 26/10/2026 UTC (đến 27/10 theo Asia/Bangkok), trong trần người vận hành duyệt **2 USD**. Code guardrail và tích hợp ComfyUI đã chạy qua provider giả và hai lượt thuê thật. [JSON bằng chứng đã bỏ bí mật](evidence/live.json), [PNG tải từ máy thật](evidence/smoke.png), [runbook](../../runbooks/gpu-slice-e.md).

## Kết quả trên máy thật

| Mục | Bằng chứng |
| --- | --- |
| GPU | Quadro RTX 5000, CUDA, VRAM 16.700.997.632 byte theo `/system_stats` |
| Image | `vastai/pytorch@sha256:3fc057381629c2afb6f86901e762b3753cc1d81e3f894352ad9f09e228bb06bf` |
| Tag tra cứu | `2.9.1-cuda-12.8.1-py312-24.04-2026-10-23`; lúc tạo dùng digest |
| ComfyUI commit | `cdf272f3514353aab04dc2d9fceef57a260dc156` |
| Rental cuối | `r_ff34c5af352e`, instance `57844652` |
| Provisioning | Portal báo complete, ComfyUI thấy CUDA, smoke `EmptyImage → SaveImage` thành công; `ready` sau khoảng 100 giây |
| G12 | Cả Portal và ComfyUI từ chối request không token; nhận Bearer đúng; HTTPS kiểm CA Vast, không tắt TLS |
| G11 | Chèn lỗi cục bộ vào `ComfyUIRuntime.download`; run `output_failed`; sau idle TTL guard trả `blocked_unverified_output`, instance vẫn tồn tại |
| Tải lại | Run `cr_accb2304c131` thành `succeeded`; PNG 319 byte, `image/png`, hash kiểm lại trên đĩa |
| SHA-256 output | `c36aa470d0d424b37e3aa08c67a13195c9f9ed91fa547f361a9a1e08df337dfb` |
| G09–G10 | Tạo lại service từ SQLite, recover rồi tick sau idle TTL; gửi một DELETE thật, cố ý ném `GPUUnknown` tại client sau request; hai lượt tick không gửi DELETE lần hai |
| Dọn sạch | Rental `destroyed`; cả GET instance và list owned không còn máy; `remaining_owned = []` |

Lỗi tải và mất response trong bảng là **lỗi được chèn tại client**, không phải sự cố Vast.ai. Lượt cuối kéo dài khoảng 143 giây. Lượt này kiểm restart của service và cùng đường `tick` mà worker dùng; vòng đời luồng nền trong process worker được kiểm riêng bằng test tự động.

## Chi phí và các lần thử

| Lần | Kết quả | Mức giảm số dư quan sát |
| --- | --- | --- |
| Offer `49383451` | Provider từ chối `no_such_ask`, không tạo instance | 0 USD |
| Instance `57844176` | Provisioning và tải PNG đạt; harness G11 chèn nhầm hàm, nên dừng và destroy trước khi thử lại | 0,0054544991 USD |
| Instance `57844652` | Đạt smoke, xác thực, G11, G09–G10 và xác nhận destroy | 0,0096934480 USD |
| Tổng tại lúc kết thúc lượt thử | Hai instance thật đều đã hủy | **0,0151479471 USD** |

Kiểm lại lúc 17:04:40 UTC: tài khoản không còn instance nào và mức giảm số dư cộng dồn là **0,0164128571 USD**. Đây là mức giảm số dư API tại thời điểm đo, có thể hạch toán trễ; chưa phải hóa đơn cuối. Giá offer đã thuê là `dph_total = 0.11185185185185186` USD/giờ, storage `0.2666666666666666` USD/GB-tháng cho 50 GB, bandwidth down/up lần lượt `0.0026041666666666665` / `0.00390625` USD/GB. Không còn instance để tiếp tục phát sinh phí compute/storage.

Response thật từ `/bundles/` đã xác nhận `dph_total = dph_base + storage_total_cost`. Ví dụ offer `50606673`: `0.2185185185185185 = 0.2 + 0.0185185185185185` USD/giờ. Adapter từ nay đánh dấu offer đã gồm disk; tính compute bằng tổng trừ disk, máy stopped vẫn tính disk. Các snapshot tạo trước khi sửa và provider chưa khai báo giữ cách cộng disk bảo thủ. Lượt thử ở trên dùng snapshot trước sửa nên ước tính local có cộng dư disk; số dư API không bị ảnh hưởng.

## Phạm vi và phần chưa chứng minh

- Test tự động bao phủ giới hạn thời gian, giá, ngân sách, race idle với hoạt động mới, output chưa kiểm, provider/label sai, destroy lỗi/timeout, worker restart, che token và checksum model thiếu/lệch/đúng.
- Smoke thật không tải model lớn hoặc custom node. Nó chứng minh khởi động ComfyUI, thấy GPU, submit theo `prompt_id`, tải và kiểm PNG; chưa chứng minh chất lượng hay tốc độ FLUX/Wan.
- `/capabilities` chỉ cung cấp trạng thái provisioning tổng thể. Code không suy ra phần trăm byte hoặc coi host running là ready.
- Guardrail mềm cần worker đang chạy. G11 có thể giữ máy vượt giới hạn vì còn output; máy stopped và orphan cần người vận hành xử lý theo runbook.
