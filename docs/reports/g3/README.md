---
id: IGN-G3-REPORT
title: Igniterra — Báo cáo Gate 3 (đang làm)
type: report
ver: "1.1"
status: draft
date: 2026-10-25
owner: Tuan
sum: Gate 3 bắt đầu trước D-07 theo yêu cầu của Tuan. Trang review, chọn/upload/chạy lại, Series Bible với character sheet và test T02–T07 đã xong (44 test xanh); T09 cho thấy character sheet giữ nhân vật nhất quán 5/5 scene ở cả hai brief thử. Điều kiện ra của Gate 3 (giảm sinh lại hoặc phút sửa so với Gate 2) chưa đo được vì Gate 2 chưa có phiếu duyệt.
rev:
  - ["1.0", 2026-10-25, "Bản đầu: phần xây và T09; chờ số đo Gate 2 của Tuan"]
  - ["1.1", 2026-10-26, "Điều kiện mở Gate 3 dẫn về G-02"]
---

<!-- id:s1 -->
## Kết luận

<!-- id:s1.b1 -->
> [!WARNING]
> Gate 3 bắt đầu ngày 25/10/2026 theo yêu cầu của Tuan, khi Gate 2 chưa có phiếu duyệt C01–C09 hay phút công (D-07 chưa quyết). Lộ trình chỉ mở Gate 3 sau khi Gate 2 đạt G-02. Cái giá: điều kiện ra của Gate 3, "giảm số lần sinh lại hoặc phút sửa đã đo ở Gate 2", chưa có mốc để so.

<!-- id:s1.b2 -->
Phần xây của Gate 3 đã xong. Gồm trang review (Tasks, Storyboard, Task detail, Series, Settings), chọn ứng viên, upload kèm nguồn và quyền, chạy lại scene kèm ghi chú, phiếu duyệt trên web, và Series Bible đầy đủ (ảnh tham chiếu sinh bằng job của worker, character sheet). T02–T07 chạy với bản giả. T09 chạy thật trên 2 brief: character sheet giữ được nhân vật, còn đồ vật (chậu đất) vẫn trôi.

<!-- id:s1.b3 -->
Table: Hạng mục Gate 3 (08 · Gate 3)
| Hạng mục | Trạng thái | Bằng chứng |
| --- | --- | --- |
| Trang review: Tasks, Storyboard, Task detail, Series | Xong | `studio/web/`, `python -m studio web` (127.0.0.1:8600), ảnh chụp ở `evidence/web-*.jpg` |
| selected, upload, rerun scene + ghi chú | Xong | T03 (`test_upload_*`), `test_select_other_image_*`, rerun s04 của t_0008 |
| Series Bible: ảnh tham chiếu, sheet, lexicon, giọng | Xong | `studio/series.py`, series `garden` và `station` |
| Settings đơn giản | Xong | Ghi `data/settings.toml` sau khi kiểm adapter khởi tạo được (T07) |
| T02–T07 | Xong với bản giả; T04 thêm ở tầng adapter với server HTTP giả | `tests/test_pipeline.py`, `tests/test_web.py`, `tests/test_omniroute.py` |
| T09 trôi nhân vật | Đã đo (định tính) | [T09](#s3) |
| Điều kiện ra: giảm sinh lại hoặc phút sửa so với Gate 2 | **Chưa đo được** | Cần phiếu duyệt Gate 2 |

<!-- id:s2 -->
## Đã xây

<!-- id:s2.b1 -->
```sh
uv run python -m studio web                 # http://127.0.0.1:8600 ; STUDIO_BASIC_AUTH=user:pass để bật basic auth
uv run python -m studio worker              # chạy task và job ảnh tham chiếu của series
uv run python -m studio series new garden --from-task t_0006
uv run python -m studio series ref garden robot_bao_tri front          # job trả phí, worker chạy
uv run python -m studio series approve garden robot_bao_tri refs/candidates/robot_bao_tri_front_a1.jpg
uv run python -m studio task new --brief briefs/brief-02.json --series garden
```

<!-- id:s2.b2 -->
Table: Quyết định khi code Gate 3, đã ghi vào tài liệu thiết kế
| Chủ đề | Quyết định | Lý do |
| --- | --- | --- |
| Sinh ảnh tham chiếu từ trang Series | Bảng `series_jobs`; web chỉ xếp job, worker chạy và ghi `data/series/<tên>/costs.jsonl` | Web không gọi provider (02) |
| Góc nhìn tham chiếu | Ảnh duyệt đầu tiên làm mốc: các góc sau gửi kèm nó làm ảnh tham chiếu | 3 góc sinh độc lập không cùng một nhân vật (`evidence/refs-*-independent.jpg`) |
| Character sheet | Ghép các ảnh duyệt cùng chiều cao 1536 px → `refs/<nhân vật>_sheet_vN.png`, chỉ thêm | OpenRouter nhận 1 ảnh tham chiếu |
| Settings | Ghi lớp phủ `data/settings.toml`, không sửa `studio.toml` đã commit | Giữ git sạch; xóa file là về mặc định |
| Gemini 400 "could not generate an image (STOP)" | `ContentRefused` như moderation | t_0007 hỏng cả task vì một scene khi lỗi này còn là `Fatal` |
| Phút chủ động trên phiếu duyệt | Tự điền theo thời gian mở Storyboard, người sửa được | 04 · reviews |
| Log worker | `data/tasks/<id>/worker.log`, đã lọc khóa; Task detail hiện 50 dòng cuối | 06 · Task detail |

<!-- id:s3 -->
## T09: trôi nhân vật

<!-- id:s3.b1 -->
Cách làm: lấy 2 brief bị trôi rõ nhất ở Gate 2 (brief-02 robot, brief-01 người gác). Tạo series từ `cast` của task Gate 2, sinh 3 góc nhìn, dựng character sheet rồi chạy lại brief trong series. Chấm bằng mắt từ contact sheet: chủ thể ở mỗi scene có cùng thiết kế với scene đầu không. Đây là nhận xét của Claude, chưa phải số đo của Tuan, và chưa dùng VLM chấm.

<!-- id:s3.b2 -->
Table: Nhất quán của chủ thể chính (Claude chấm từ contact sheet)
| Brief | Không series (Gate 2) | Có character sheet (Gate 3) | Ghi chú |
| --- | --- | --- | --- |
| 02 robot | t_0006: 3 thiết kế khác nhau trong 4 scene (s03 cao, đầu tách rời) | t_0009: 5/5 cùng thiết kế với sheet | Chậu đổi kiểu ở cả hai lượt (đất nung, đen, kim loại): đồ vật không có trong sheet |
| 01 người gác | t_0001: s01 tóc dài, s02–s04 tóc ngắn | t_0008: 5/5 mũ trùm, bộ đồ navy, đèn vai | t_0008 s04 lượt 1 có người nằm ngang lơ lửng (C08); rerun kèm ghi chú thì đạt |

<!-- id:s3.b3 -->
Ảnh so sánh (hàng trên không series, hàng dưới có sheet): `evidence/t09-garden.jpg`, `evidence/t09-station.jpg`.

<!-- id:s3.b4 -->
Đề xuất cho quyết định "có cần LoRA" (Tuan chốt): chưa cần LoRA cho thể loại này. Character sheet qua 1 ảnh tham chiếu đủ giữ nhân vật xuyên suốt một video. Phần còn trôi là đồ vật và bối cảnh lặp lại. Có hai hướng: thêm mục `props` (đồ vật có ảnh tham chiếu, ghép chung sheet) vào Series Bible, hoặc mô tả đồ vật cố định trong `style`. Sheet cũng kéo theo bối cảnh của ảnh mốc (garden: nhà xưởng) vào cả những scene lẽ ra là nhà kính.

<!-- id:s4 -->
## Phát hiện về provider

<!-- id:s4.b1 -->
- Gemini chặn moderation có hệ thống các cảnh "robot yếu pin / tắt" của brief-02, ở cả 3 task: t_0002 s03, t_0006 s02/s03, t_0007 s02/s04, t_0009 s05. Viết lại trung tính vẫn bị chặn. Chỉ khi đưa robot ra khỏi trung tâm khung thì mới đạt. Với twist của brief-02 ("robot tắt đi"), hình phải gợi bằng đồ vật, còn lời kể mang ý chính.
- Tổng ảnh có kết quả qua OpenRouter: 25 ở Gate 2, 25 ở Gate 3 (10 tham chiếu, 15 keyframe); thêm 15 lượt bị chặn hoặc không sinh được ảnh, ghi `failed` (6 ở Gate 2, 9 ở Gate 3). Gateway không trả giá. Với khoảng 0,04–0,1 USD mỗi ảnh, tổng có thể đã gần trần 5 USD. **Claude đã dừng lượt gọi trả phí; cần kiểm openrouter.ai → Activity trước khi chạy thêm.**

<!-- id:s5 -->
## Việc của Tuan

<!-- id:s5.b1 -->
```chk
- [ ] Kiểm số tiền đã dùng trên openrouter.ai → Activity (khoảng 50 ảnh, 15 lượt bị chặn) trước mọi lượt gọi trả phí tiếp theo
- [ ] Chấm phiếu duyệt Gate 2 (t_0001, t_0003–t_0006) trên trang Storyboard: đây là mốc để đo điều kiện ra Gate 3
- [ ] Chấm thêm t_0008, t_0009 (có series); so phút công và số lần sinh lại với bản không series cùng brief
- [ ] Chốt T09: có cần LoRA không; có thêm `props` vào Series Bible không
- [ ] Quyết định D-07 (Gate 2) và điều kiện ra Gate 3
```

<!-- id:s6 -->
## Bằng chứng

<!-- id:s6.b1 -->
- `evidence/web-board.jpg`, `web-detail.jpg`, `web-tasks.jpg`: ảnh chụp trang trên dữ liệu thật.
- `evidence/refs-garden-independent.jpg`, `refs-station-independent.jpg`: 3 góc sinh độc lập, bị trôi. `refs-anchored.jpg`: các góc sinh kèm ảnh mốc.
- `evidence/series-garden/`, `series-station/`: `bible.yaml` và `costs.jsonl` của series.
- `evidence/t_0007/`, `t_0008/`, `t_0009/`: ScenePlan revision, `assets.json`, `costs.jsonl`, manifest.
