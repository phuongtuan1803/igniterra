---
id: IGN-G01-REPORT
title: Igniterra — Báo cáo Gate 0 và pilot Gate 1
type: report
ver: "1.0"
status: draft
date: 2026-10-24
owner: Tuan
sum: Gate 0 đã chốt; Gate 1 dừng ở pilot ảnh local và preview kỹ thuật, chưa kết luận build hay reuse.
rev:
  - ["1.0", 2026-10-24, "Ghi nhận quyết định G0, kết quả thực thi G1 và giới hạn bằng chứng; không mở Gate 2"]
---

# Báo cáo G0/G1

## Kết luận

| Mục | Kết quả được ghi nhận |
| --- | --- |
| G0 — mục tiêu | Đã chốt thể loại, 5 brief, checklist và giới hạn thời lượng; xem [kế hoạch đóng băng](g0-plan.md) |
| D-02 | Short truyện sci-fi bí ẩn tiếng Việt; hư cấu, một tình huống và một twist |
| Thời lượng | Không quá **60 giây** cho toàn bộ video; mục tiêu mềm 45–58 giây, không kéo dài câu chuyện chỉ để đủ thời lượng |
| D-03 | Trần kế hoạch 5 USD; **chưa thiết lập trên dashboard provider**; chưa gọi API sinh ảnh/video trả phí trong pilot |
| G1 — môi trường | MPT cài/chạy được; Edge TTS và tạo SRT hoạt động; có 5 MP4 preview kỹ thuật |
| G1 — baseline roadmap | **Chưa hoàn tất**: chưa chạy 5 video MPT nguyên bản + Pexels và lượt ảnh AI bổ sung |
| D-01 — build hay reuse | **Chưa quyết định**; không có số liệu đủ để kết luận MPT trượt yêu cầu bắt buộc |
| D-07 — đầu tư tiếp | Chưa quyết định; báo cáo này không cho phép tự động mở Gate 2 |

Người dùng yêu cầu không kiểm tra video thêm, chuyển sang xem xét kiến trúc MPT và lưu báo cáo. Đây là quyết định dừng phạm vi thử ở trạng thái hiện tại, **không phải duyệt toàn bộ checklist hoặc tuyên bố G1 đạt**.

## Phạm vi và thay đổi so với kế hoạch

Nguồn yêu cầu: [08 — Roadmap](../../src/design/08-roadmap.md), mục Gate 0–1. Checklist pilot không thay thế các FR/NFR của tài liệu thiết kế hệ thống.

Kế hoạch ban đầu: cùng 5 brief, Edge TTS tiếng Việt + Pexels, thêm một lượt nguồn ảnh AI, nhật ký đo công và chi phí. Thực tế chưa có cấu hình Pexels/ảnh AI tại lần kiểm tra nên dùng **5 ảnh local người dùng cung cấp, mỗi ảnh ứng với một brief**. Không có bằng chứng trong phạm vi này để khẳng định công cụ/model đã tạo các ảnh đó hoặc quyền sử dụng của chúng.

Kịch bản được chuẩn bị trực tiếp, không gọi LLM của MPT. Ảnh nguồn 941×1672 được upscale/crop để xuất 1080×1920. Không gọi dịch vụ đăng bài.

## Môi trường đã thực thi

- Igniterra tại điểm bắt đầu báo cáo: `6d7e8a5639d25656dcaf2b68e1f012064a74bb73`, nhánh `claude/project-thread-79o0a1`.
- MPT: `82bb4a7502704559575f4e3d002b9dcb31d4ed6a`, package `1.3.9`.
- Python `3.13.16`; `uv sync --frozen --python /usr/local/bin/python --no-python-downloads` đã thành công. Tải Python 3.11 từng bị proxy chặn.
- FFmpeg `7.0.2` từ `imageio-ffmpeg`; Streamlit `1.59.1`.
- WebUI health từng trả HTTP 200; Streamlit AppTest không exception. Đây là kết quả tại lúc chạy, không bảo đảm process còn sống sau khi đổi sandbox.
- WebUI được khởi động lại trên `0.0.0.0:8501`, đã kiểm tra socket listen và health. Truy cập từ host còn phụ thuộc publish/forward cổng Docker; không báo đã xác minh đường truy cập host.
- Kiểm tra release GitHub bị proxy chặn không được tính là lỗi tạo nội dung.

## Kết quả thực thi

### Audio và phụ đề MPT

CLI batch dừng tại `subtitle`: **5 thành công, 0 thất bại ở lần batch chạy thành công**. Giọng `vi-VN-NamMinhNeural`, phụ đề Edge, nhạc tắt. Audio và SRT được dùng lại khi dựng preview; không gọi lại TTS.

Lần batch nhập đầu bị validation từ chối trước khi tạo task vì `bgm_type="none"` trong manifest. Đã đổi manifest sang `bgm_type=""`; CLI `--bgm-type none` và giá trị manifest không hoàn toàn tương đương ở commit này. Không sửa mã nguồn upstream.

### MP4 preview kỹ thuật

| Brief | Tên | Thời lượng MP4 (giây) | Render preview (giây) |
| --- | --- | ---: | ---: |
| 01 | Tín hiệu từ ngày mai | 34.51 | 39.30 |
| 02 | Người làm vườn cuối cùng | 33.67 | 42.55 |
| 03 | Dấu chân đến trước | 35.59 | 43.46 |
| 04 | Một phút ký ức | 34.80 | 41.23 |
| 05 | Bức thư gửi ngôi sao đã tắt | 35.33 | 34.66 |

Mỗi video: H.264/AAC, 1080×1920, 24 fps, một ảnh zoom nhẹ 5%, phụ đề burn-in. Số liệu render chỉ đo vòng render/kiểm tra của helper, **không phải thời gian đầu-cuối, phút công hoặc tổng thời gian pilot**. Audio/MP4 đều dưới giới hạn 60 giây, dù ngắn hơn mục tiêu mềm.

Các file đã qua giải mã FFmpeg toàn bộ không lỗi, kiểm tra kích thước/thời lượng/có audio và lấy mẫu khung hình không rỗng. Contact sheet có một frame mỗi video đã được xem; phụ đề nằm trong khung ở các mẫu đó. Chưa duyệt toàn bộ phát âm, timing phụ đề, quyền tài sản hoặc tính liên tục kể chuyện.

### Phương pháp render và lượt không đạt

Lượt đầu gọi trực tiếp `video.preprocess_video()` và `video.generate_video()` của MPT, nhưng helper bỏ qua bước combine/resize. File xuất giữ 941×1672, kiểm tra đích 1080×1920 thất bại. Đây là lỗi cách gọi helper, **không phải bằng chứng bug của luồng MPT đầy đủ**.

Sau đó dùng helper FFmpeg ngoài pipeline MPT để tạo 5 preview đạt kiểm tra kỹ thuật; MP4 thử native-resolution đầu tiên đã được thay thế. **Không gọi đây là MPT nguyên bản end-to-end hoặc hiệu năng renderer MPT**. Không sửa code MPT/không xây core Igniterra trong lượt thử.

## Đánh giá checklist và dữ liệu còn thiếu

- C01/C02: định dạng, kích thước và giới hạn thời lượng đã có kiểm tra kỹ thuật.
- C03–C05, C07–C09: chưa chấm đầy đủ; các kiểm tra lấy mẫu không thay thế xem/nghe toàn bộ.
- C06: một ảnh không thể hiện đủ chuỗi hành động theo brief; video 02 có mầm cây ngay từ đầu, lộ twist. Đây là giới hạn bộ đầu vào/preview đã chọn, chưa chứng minh MPT không thể dựng đúng nếu được cung cấp đủ cảnh.
- `accepted`/`quality_accepted`: giữ `null`, không đổi thành `true` hoặc gán toàn bộ thành `false` khi chưa duyệt.
- `human_minutes`, thời gian đầu-cuối, hóa đơn và chi phí trên video được duyệt: chưa đo đủ, giữ `null`. Không tính bằng 0.
- Không có phép so sánh đồng điều kiện giữa MPT nguyên bản và studio.

## Bằng chứng được commit

- [g0-plan.md](g0-plan.md): kế hoạch và brief chốt trước khi chạy, giữ nguyên nội dung lịch sử; phần trạng thái trong đó thuộc thời điểm lập kế hoạch.
- [MPT architecture review](mpt-architecture-review.md): bài học đọc mã, không phải kết quả benchmark.
- [tasks.json](evidence/tasks.json): lời kể và cấu hình batch thực tế.
- [subtitle-results.json](evidence/subtitle-results.json): task ID và kết quả batch, đường dẫn chuẩn hóa thành tham chiếu artifact.
- [render-report.json](evidence/render-report.json): số đo render đã lưu, chuẩn hóa đường dẫn; không phải kết quả đo lại khi viết báo cáo.
- [pilot-runs.jsonl](evidence/pilot-runs.jsonl): nhật ký tái dựng từ bằng chứng cho các stage thành công; không phải log đầy đủ ghi đồng thời từ đầu pilot.
- [execution-events.json](evidence/execution-events.json): các lần lỗi/đổi phương pháp đã biết; không gán thời gian/chi phí chưa đo.
- [artifact-manifest.json](evidence/artifact-manifest.json): SHA-256/kích thước của ảnh, MP3, SRT và MP4 thật; binary không được commit.
- [subtitles/](evidence/subtitles/): bản sao 5 SRT.

Ảnh/video/audio/ZIP và backup nằm ngoài Git, tại thư mục exports của phiên làm việc. `exports/...` trong manifest là gốc tham chiếu archive/local, không phải một đường dẫn được đảm bảo tồn tại trên máy clone repo. Các gói đã tạo: `igniterra-pilot-audio-subtitles.zip`, `igniterra-5-video-previews.zip`; backup `igniterra-work-backup.zip` được tạo **trước** ảnh/video preview và không chứa toàn bộ kết quả mới. Sao lưu riêng các gói mới nếu xóa dữ liệu exports.

## Hướng tiếp theo nếu mở lại pilot

Không tự chạy thêm theo báo cáo này. Nếu người dùng yêu cầu tiếp tục: chọn rõ baseline local hay thực hiện lại baseline Pexels theo roadmap; bổ sung scene; chạy luồng MPT end-to-end; ghi số đo công/chi phí và duyệt checklist trước khi quyết định D-01. Chỉ ra quyết định Gate 2 bằng bằng chứng mới, không suy ra từ việc có 5 file MP4.
