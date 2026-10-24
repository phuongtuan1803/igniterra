---
id: IGN-G01-MPT-REVIEW
title: Igniterra — Bài học kiến trúc từ MPT
type: report
ver: "1.0"
status: draft
date: 2026-10-24
owner: Tuan
sum: Đọc mã MPT để chọn cơ chế nên học hoặc tái sử dụng; không kết luận chất lượng sản phẩm từ cấu trúc mã.
rev:
  - ["1.0", 2026-10-24, "Ghi nhận bài học renderer revision, job trả phí, artifact commit và ranh giới adapter"]
---

# Nhận xét kiến trúc MPT

Phạm vi: đọc mã tại commit `82bb4a7502704559575f4e3d002b9dcb31d4ed6a`. Không chạy thêm video QA/test suite cho lần review; các test dưới đây được **đọc**, không báo là vừa chạy xanh. Đây là khuyến nghị, không thay ADR hoặc mở Gate 2.

## 1. Revision và cache theo dependency — ưu tiên học cao nhất

`video_project.py` khác luồng dựng chính `video.py`. Nó có stage graph audio → timing → scene, rồi assembly → export. Fingerprint chọn đúng dependency: đổi footage không làm mất cache audio; BGM chỉ ảnh hưởng export. Cache đối chiếu cả bytes thực tế, không chỉ tên file.

- [Stage graph, dòng 171–243](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/services/video_project.py#L171-L243)
- [Cache integrity, dòng 398–438](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/services/video_project.py#L398-L438)

Áp dụng: revision ScenePlan và input hash theo scene; giải thích rõ reuse/build. Chưa cần workflow engine tổng quát. Khi dùng cho AI phải hash thêm model/prompt/config ảnh hưởng đầu ra; hash toolchain hiện chỉ lấy dòng version đầu, không phải digest toàn môi trường.

## 2. Tách revision hiện hành khỏi export thành công

MPT giữ `current_revision` và `last_successful_export` riêng; revision cũ hoàn tất muộn không được promote đè bản hiện hành.

- [Revision snapshot, dòng 354–387](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/services/video_project.py#L354-L387)
- [Promotion, dòng 603–617](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/services/video_project.py#L603-L617)

Áp dụng: bổ sung lựa chọn/kết quả được người duyệt riêng với render thành công. Không coi quy ước revision bất biến là cơ chế filesystem cấm sửa; nhiều JSON vẫn không phải một transaction.

## 3. Paid submit khác poll/retry

MuAPI không retry POST mù khi timeout/5xx/response thiếu ID: job có thể đã được tính phí. Có ID thì poll cùng job; lỗi tải không đồng nghĩa phải tạo mới.

- [Submit và unconfirmed, dòng 241–300](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/services/muapi.py#L241-L300)
- [Poll có deadline, dòng 337–435](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/services/muapi.py#L337-L435)

Áp dụng: ghi ý định trước submit, lưu remote job ID ngay khi có, resume poll; mất response thì `unknown` và đối soát. Không mặc định mọi provider có idempotency. Luồng đang đọc vẫn submit/poll blocking và không chứng minh checkpoint bền vững cho mọi trường hợp crash.

## 4. Publish artifact trước, commit record sau

Renderer ghi artifact, hash/size/dependency rồi publish `record.json` cuối. JSON dùng file tạm cùng thư mục, fsync và replace.

- [Artifact commit, dòng 833–860](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/services/video_project.py#L833-L860)
- [Atomic JSON, dòng 29–64](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/services/task_artifacts.py#L29-L64)

Áp dụng: chỉ downstream-read artifact đã commit. File tồn tại không đủ điều kiện `done`. Không tuyên bố an toàn mọi lần mất điện: atomic một file không phải transaction nhiều file, cần xử lý file mồ côi và durability.

## 5. Dùng chung application service, preflight trước chi phí

CLI/API/WebUI dùng entrypoint task chung; có `stop_at` cho từng stage và preflight FFmpeg/provider trước khi tạo nội dung.

- [Pipeline/preflight, dòng 1527–1674](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/services/task.py#L1527-L1674)
- [Entrypoint, dòng 1925–1962](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/services/task.py#L1925-L1962)

Áp dụng: UI mỏng, worker/application service sở hữu nghiệp vụ. Không bê nguyên các nhánh provider trong task.py sang core; adapter phải dịch lỗi và dữ liệu về contract chung.

## 6. Registry nhỏ, queue có giới hạn, cấu hình nhất quán

- [LLMProviderSpec, dòng 29–55](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/models/llm_provider.py#L29-L55): metadata bất biến tách khỏi giao thức gọi. Đây là registry LLM, không phải abstraction chung mọi media provider.
- [Queue admission, dòng 54–84](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/controllers/manager/base_manager.py#L54-L84): đặt chỗ concurrency trước khi tạo thread; queue có giới hạn.
- [WebUI config lock, dòng 92–103](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/services/webui_task.py#L92-L103): giữ cấu hình ổn định suốt job; WebUI giới hạn một task.
- [Path boundary, dòng 4–35](https://github.com/harry0703/MoneyPrinterTurbo/blob/82bb4a7502704559575f4e3d002b9dcb31d4ed6a/app/utils/file_security.py#L4-L35): realpath/commonpath thay kiểm tra prefix chuỗi.

Áp dụng: registry vừa đủ, một worker ban đầu, snapshot cấu hình không chứa secret theo task; không cho UI thay cấu hình task đang chạy. Khi còn dùng MPT global config, snapshot bên Igniterra không tự giải quyết được đồng thời nhiều cấu hình MPT.

## Ranh giới khuyến nghị cho Igniterra

| Thành phần | Hướng xử lý |
| --- | --- |
| TTS/subtitle/provider kết nối | Reuse sau adapter; ghim commit và test riêng trước khi nâng |
| Renderer theo revision | Cân nhắc vendor có provenance, bản quyền và license theo thiết kế |
| Memory/Redis state hiện có | Không dùng làm nguồn sự thật duy nhất cho yêu cầu resume của core mới; ưu tiên SQLite + một worker ban đầu |
| WebUI lớn và pipeline task nhiều nhánh | Không bê nguyên làm core Igniterra |
| Chạy song song/phân tán/plugin framework | Chưa xây trước khi có nhu cầu đo được |

Thiết kế hiện tại nói vendor `video_project.py` tại commit `9e7d8b7`, trong khi review này khảo sát `82bb4a7`. Không copy bản hiện tại mà ghi nguồn gốc cũ; chọn commit và ghi license/provenance chính xác trước khi triển khai. Đây chưa phải quyết định thay phiên bản vendor.

Kết luận: học cơ chế phục hồi, dependency và lưu artifact của MPT; không lấy toàn bộ MPT làm core. Kết quả đọc mã không thay thế D-01 cần dữ liệu baseline.
