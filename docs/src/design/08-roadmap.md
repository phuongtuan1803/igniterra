---
id: IGN-PLAN
title: Igniterra — Lộ trình theo decision gate
type: plan
ver: "3.1"
status: draft
date: 2026-10-27
owner: Tuan
sum: Gate 2 và Gate 3 đã xây; Gate 4 đã triển khai GPU/ComfyUI A–E, ComfyImage và Monitor. Pilot FLUX tạo 5 keyframe và MP4 53 giây; Codex chấm 2/5 ảnh đạt nội dung sơ bộ, chờ người duyệt cuối. Không so sánh OmniRoute.
kv: [[Trạng thái, "ComfyImage có MP4 pilot; chờ duyệt nội dung"], [Chỉ số, "video được duyệt, phút công, chi phí trên video/ảnh được duyệt"], [Bằng chứng, "reports/g2, reports/g3, reports/g4-comfy-image"]]
rev:
  - ["2.8", 2026-10-25, "Gate 3 bắt đầu trước D-07; phần xây xong, T09 đã đo (lịch sử trước đó: git log)"]
  - ["3.0", 2026-10-26, "Soát theo code: bỏ nội dung pilot trước Gate 2, ví dụ t_0042 minh họa và phụ lục rà soát v1.1; thêm thứ tự triển khai GPU thuê/ComfyUI (09)"]
  - ["3.1", 2026-10-27, "PR #11–#15 hoàn tất GPU A–E; thêm ComfyImage, profile FLUX và Monitor; pilot theo yêu cầu Tuan, không so sánh OmniRoute"]
---

<!-- id:s1 -->
## Lộ trình theo decision gate

<!-- id:s1.b1 -->
Mỗi gate có đầu ra kiểm chứng và lối ra "dừng xây" (AD-12). Core được viết ở Gate 2 và không đổi ở Gate 4: mỗi năng lực mới là một adapter hoặc một module hạ tầng riêng.

<!-- id:s1.b2 -->
<!-- report-kit:d2
```d2
# cap: Ba decision gate còn hiệu lực; mỗi gate có lối ra dừng xây
direction: down
g2: "Gate 2 · Core + adapter, CLI\n5 brief ra MP4\n(đã xây)" {class: brain}
g3: "Gate 3 · Duyệt và sửa\ntrang review, selected,\nrerun, Series Bible\n(đã xây)" {class: brain}
g4: "Gate 4 · Mở rộng\nmột hạng mục mỗi lần" {class: hands}
x2: "Dừng hoặc quay lại MPT\n(không đạt mục tiêu)" {class: bad}
x3: "Bỏ tính năng\nkhông giải quyết lỗi đã thấy" {class: bad}
g2 -> g3: "đạt mục tiêu G-02"
g3 -> g4: "giảm sinh lại\nhoặc phút sửa"
g2 -> x2
g3 -> x3
```
-->
<picture>
<source media="(prefers-color-scheme: dark)" srcset="08-roadmap.figs/s1.b2-dark.svg">
<img alt="Ba decision gate còn hiệu lực; mỗi gate có lối ra dừng xây" src="08-roadmap.figs/s1.b2-light.svg">
</picture>

<!-- id:s1.b3 -->
Table: Cổng, đầu ra kiểm chứng và điều kiện
| Cổng | Đầu ra kiểm chứng | Đi tiếp khi | Dừng hoặc quay lại MPT khi | Trạng thái |
| --- | --- | --- | --- | --- |
| 2 — Core + adapter, CLI | Core (contract, StepRun, `costs.jsonl`), adapter thật, 5 brief trong `briefs/` ra MP4; test quét import | Đạt G-02 ([01](01-system-design.md#s3)): ít nhất 3/5 video đạt C01–C09, tối đa 15 phút chủ động mỗi video được duyệt, tổng chi trong 5 USD | Không đạt sau một vòng sửa | Đã xây; chờ phiếu duyệt ([g2](../../reports/g2/README.md)) |
| 3 — Duyệt và sửa | Trang review, selected, rerun scene, Series Bible; T02–T07, T09 | Giảm số lần sinh lại hoặc phút sửa so với Gate 2 | Tính năng không giải quyết lỗi đã thấy | Đã xây; điều kiện ra chờ số đo Gate 2 ([g3](../../reports/g3/README.md)) |
| 4 — Mở rộng | Một hạng mục mỗi lần (bảng ở mục Gate 4) | Có nhu cầu định lượng | — | GPU/ComfyUI A–E đã có code và thuê thật ([g4](../../reports/g4/README.md)); tiếp nối bằng ComfyImage + pilot FLUX theo tiêu chí tuyệt đối |

<!-- id:s3 -->
## Gate 2: core + adapter, CLI

<!-- id:s3.b1 -->
Gate 2 chia hai nửa: 2a không gọi API trả phí (bản giả, Edge TTS), 2b bật OmniRoute sau khi giới hạn chi tiêu đã đặt. Phần máy đã xong; còn phần người.

<!-- id:s3.b3 -->
```chk
- [x] ADR-0004, ADR-0005, ADR-0006 được duyệt (Tuan, 24/10/2026)
- [x] P-01: ghim `mpt==1.3.9`, vendor `video_project.py` tại `82bb4a7`
- [x] Kiểm OmniRoute trước khi bật adapter trả phí: model có trong `/v1/models`, ảnh tham chiếu, tắt fallback, JSON schema `strict` ([05 · OmniRoute](05-modules-providers.md#s5))
- [x] Giới hạn chi tiêu (D-03): OpenRouter 5 USD (25/10/2026); codex là gói thuê bao, giới hạn bằng hạn mức gói
- [x] Test quét import: chỉ `studio/providers/mpt/` import `app.*` (`tests/test_import_boundary.py`)
- [x] Test core với bản giả: resume, retry, mồ côi → `unknown`, poll sau resume, sửa scene, T11, T12 (`tests/test_pipeline.py`)
- [x] 5 brief ra MP4 9:16 có phụ đề, đạt hậu điều kiện (1080×1920, có audio, 48–57 giây; [báo cáo g2](../../reports/g2/README.md))
- [ ] (Chờ Tuan) Phiếu duyệt C01–C09 và phút công cho 5 lần chạy (`python -m studio review`); đối chiếu chi phí ảnh trên OpenRouter vì response không có `usage`
- [ ] (Chờ Tuan, sau phiếu duyệt và hóa đơn) D-07: đi tiếp hay quay lại MPT
```

<!-- id:s4 -->
## Kiểm thử chấp nhận

<!-- id:s4.b1 -->
Mỗi test chạy ở gate đầu tiên mà thành phần của nó tồn tại; test core tương ứng chạy với bản giả ở mọi lần commit ([05 · Kiểm thử](05-modules-providers.md#s11)). Hiện có 46 test (`uv run pytest`, 0 USD).

<!-- id:s4.b2 -->
Table: Test chấp nhận và gate chạy
| Test | Nội dung | Gate | Khẳng định |
| --- | --- | --- | --- |
| T01 | End-to-end 5 brief | 2 | 5 MP4 1080×1920 ≤ 60 giây, đạt hậu điều kiện; phiếu duyệt đủ dòng |
| T02 | Sửa một scene | 3 | Chỉ phần phụ thuộc chạy lại; scene khác dùng lại |
| T03 | Thay asset bằng upload và chọn | 3 | Ứng viên `provider: user`; chỉ render chạy lại |
| T04 | Lỗi tạm thời 429/5xx/timeout | 3 | Thử lại tối đa 3 lần có backoff |
| T05 | Tắt worker ngay sau khi gửi lượt gọi trả phí | 3 | `unknown`, task `waiting`, không gửi lại |
| T06 | Tắt worker khi đang poll job video | 3 (với `FakeVideo`), 4 (thật) | Poll `job_id` cũ, không `submit` mới |
| T07 | Đổi adapter TTS chỉ bằng cấu hình | 3 | Không sửa code core |
| T08 | Chi phí khi chạy nhiều scene song song | 4 | Giới hạn song song theo adapter được tôn trọng; chi phí khớp số lượt gọi |
| T09 | Trôi nhân vật qua nhiều scene | 3 | Đo mức nhất quán với Series Bible; quyết định có cần LoRA |
| T11 | Giọng vượt trần thời lượng | 2 (với `FakeTTS` trả audio dài) | Task `waiting` lý do `duration_exceeded`; không có render |
| T12 | Hình bắt buộc xuất hiện sớm | 2 (với `FakeLLM`) | ScenePlan đặt `mầm cây` ở scene 1 của brief-02 bị từ chối; `plan` gọi lại kèm lý do |

<!-- id:s4.b3 -->
T10 (so với MPT nguyên bản) không áp dụng vì D-01 đã chốt build và không có baseline MPT; thay bằng đo theo G-02.

<!-- id:s5 -->
## Gate 3 và Gate 4

<!-- id:s5.b1-2 -->
> [!WARNING]
> Gate 3 bắt đầu ngày 25/10/2026 khi Gate 2 chưa có phiếu duyệt (D-07 chưa quyết). Phần xây và T02–T07, T09 đã xong ([báo cáo g3](../../reports/g3/README.md)); điều kiện ra cần số đo phút công và số lần sinh lại của Gate 2 làm mốc.

<!-- id:s5.b2 -->
<!-- report-kit:d2
```d2
# cap: Gate 3 thêm giao diện và Series Bible; Gate 4 thêm adapter hoặc module hạ tầng, core không đổi
direction: down
g3: Gate 3 · Duyệt và sửa (đã xây) {
  class: zone
  a: "Trang review\nTasks, Storyboard,\nTask detail, Series" {class: plain}
  b: "selected, upload,\nrerun scene + ghi chú" {class: gate}
  c: "Series Bible\nảnh tham chiếu, sheet, lexicon" {class: data}
  d: "Settings\n(data/settings.toml)" {class: plain}
}
g4: Gate 4 · Mở rộng (mỗi lần một hạng mục) {
  class: zone
  e: "Image-to-video\nadapter VideoProvider\n(D-04)" {class: hands}
  f: "GPU thuê + ComfyUI\nVast.ai on-demand\n(D-05, thiết kế ở 09)" {class: hands}
  g: "Audiobook\nchapterize, master\n(D-06)" {class: hands}
  h: "Đăng bài\n(sau audit nền tảng)" {class: hands}
}
g3 -> g4: "giảm sinh lại hoặc phút sửa"
```
-->
<picture>
<source media="(prefers-color-scheme: dark)" srcset="08-roadmap.figs/s5.b2-dark.svg">
<img alt="Gate 3 thêm giao diện và Series Bible; Gate 4 thêm adapter hoặc module hạ tầng, core không đổi" src="08-roadmap.figs/s5.b2-light.svg">
</picture>

<!-- id:s5.b3 -->
Table: Điều kiện mở từng hạng mục Gate 4
| Hạng mục | Mở khi | Quyết định |
| --- | --- | --- |
| Image-to-video | Gate 3 cho thấy ảnh tĩnh không đủ cho thể loại đã chọn | D-04 |
| GPU thuê + ComfyUI | Chi phí trên asset được duyệt, hoặc nhu cầu model mở (FLUX.2 klein 4B, Wan 2.2), chứng minh lợi ích | D-05 |
| Audiobook | Có workload thật | D-06 |
| Đăng bài tự động | Audit YouTube/TikTok xong; cờ nội dung AI sẵn sàng | — |

<!-- id:s5.b4 -->
A–E đã triển khai qua PR #11–#15, có test ở [09 · Kiểm chứng](09-gpu-comfyui.md#s8) và [lượt thuê thật Slice E](../../reports/g4/README.md). Mỗi lượt thuê thật tiếp theo vẫn cần trần chi phí và xác nhận destroy; việc đã có code không tự cho phép chi không giới hạn.

<!-- id:s5.b5 -->
Table: Thứ tự triển khai GPU thuê và ComfyUI
| Lát | Phạm vi | Thoát khi |
| --- | --- | --- |
| A | Contract, bảng SQLite, `FakeGPUProvider`, trang GPU Rentals | G01–G04 |
| B | `VastGPUProvider` (REST, không cài gói `vastai`), label chủ sở hữu, reconcile, chi phí ước tính | G04, G09, G10 |
| C | Image ComfyUI ghim digest, provisioning manifest đã duyệt, health + smoke | G05–G07 |
| D | Thư viện workflow, chạy bằng `prompt_id` do client sinh, tải và kiểm output | G06, G08, G11 |
| E | Idle TTL, bảo mật, một lượt thuê thật có trần chi phí | G09–G12 + bằng chứng thật |

<!-- id:s5.b6 -->
Tiếp nối A–E theo yêu cầu Tuan ngày 27/10/2026: `ComfyImage` gọi workflow đã duyệt trên rental ready, profile FLUX.2 Klein 4B distilled có text-to-image và character sheet, và trang `/monitor` nối task/scene ↔ run ↔ rental. Không tự thuê GPU từ content adapter. Pilot dùng lại một kịch bản và Series Bible đã có, sinh keyframe qua FLUX rồi đi qua gate duyệt ảnh và render MP4; ghi thời gian, chất lượng/nhất quán, số lần thử, chi phí trên ảnh đạt và trạng thái dọn máy. Không chạy hoặc so sánh với OmniRoute. Giọng/phụ đề/render tiếp tục qua adapter hiện có; kết quả kỹ thuật không thay phiếu duyệt của người.

<!-- id:s5.b7 -->
[Pilot đã chạy](../../reports/g4-comfy-image/README.md): task `t_0011`, 5 PNG đã kiểm hash trên RTX 3090, MP4 1080×1920 dài 53,133 giây; cả hai lượt thuê đã destroy. Codex chấm nội dung sơ bộ 2/5 ảnh đạt; s01/s03/s05 còn lỗi, task chờ `gate:final`. Chi phí quan sát của riêng pilot 0,1245334693 USD (gồm lượt lỗi), khoảng 0,06227 USD/ảnh đạt sơ bộ; chưa phải hóa đơn hay kết quả người duyệt. Bước tiếp theo là người xem final và quyết định sửa nội dung trước khi mở batch lớn/model video.

<!-- id:s7 -->
## Quyết định cần chốt

<!-- id:s7.b2 -->
> [!NOTE]
> P-01 (đã chốt 24/10/2026): `mpt==1.3.9`, commit `82bb4a7`; vendor `video_project.py` cùng commit. Nâng phiên bản theo [07 · Nâng MPT](07-operations.md#s3).

<!-- id:s7.b3 -->
> [!NOTE]
> P-02 (đã chốt 24/10/2026, đổi model 25/10/2026): LLM và ảnh qua OmniRoute, model đặt trong `studio.toml` (`codex/gpt-5.6-luna`, `openrouter/google/gemini-3.1-flash-image-preview` với 1 character sheet). Dự phòng tự host: FLUX.2 klein 4B qua ComfyUI (D-05).

<!-- id:s7.b4 -->
```q
P-03 · Giọng đọc mặc định?
1. Edge TTS vi-VN qua MptTTS (miễn phí; đang dùng); ElevenLabs khi cần chất lượng cao hơn
2. ElevenLabs qua MptTTS ngay từ đầu
rec: 1
```

<!-- id:s7.b5 -->
Table: Quyết định vận hành
| ID | Quyết định | Trạng thái |
| --- | --- | --- |
| D-01 | Build hay reuse | Đã chốt build (core + adapter), 24/10/2026; không có baseline MPT nên không có T10 |
| D-02 | Thể loại video pilot | Đã chốt: Short sci-fi bí ẩn tiếng Việt, tối đa 60 giây (CON-08) |
| D-03 | Mức chi tiêu tối đa của pilot | Đã chốt 5 USD, đặt ở tài khoản của từng provider trả phí (OpenRouter 5 USD) |
| D-04 | Khi nào thêm image-to-video | Mở: khi Gate 3 cho thấy ảnh tĩnh không đủ |
| D-05 | Khi nào thuê GPU (ComfyUI) | A–E đã triển khai và thuê thật; ComfyImage/FLUX được thực hiện theo yêu cầu Tuan với trần chi phiên 2 USD. Năng lực/model tiếp theo cần mục tiêu và trần riêng; xem [09](09-gpu-comfyui.md) |
| D-06 | Khi nào làm audiobook | Mở: khi có workload thật |
| D-07 | Điều kiện tiếp tục đầu tư | Mở: chờ phiếu duyệt và hóa đơn Gate 2, so với G-02 |
