---
id: IGN-G2-REPORT
title: Igniterra — Báo cáo Gate 2
type: report
ver: "1.0"
status: draft
date: 2026-10-25
owner: Tuan
sum: Core, CLI và adapter Gate 2 đã viết và có 21 test xanh; 5 brief của Gate 0 ra MP4 1080×1920, 48–57 giây qua OmniRoute và Edge TTS, đạt hậu điều kiện kỹ thuật. Hóa đơn ảnh thật là 5,20 USD (vượt trần 5 USD); phiếu duyệt mới có t_0001 (C07 fail) và t_0003 (C09 unknown) chấm thật, nên chưa quyết định D-07.
rev:
  - ["1.0", 2026-10-25, "Bản đầu: Gate 2a và 2b đã chạy; phiếu duyệt và D-07 chờ Tuan"]
---

<!-- id:s1 -->
## Kết luận

<!-- id:s1.b1 -->
Phần máy của Gate 2 đã xong. Core (contract, StepRun, `costs.jsonl`, gate), CLI, bản giả và adapter thật (`OmniRouteLLM`, `OmniRouteImage`, `MptTTS`, `MptSubtitleAligner`, renderer `video_project` kèm bước `animate`) đã viết. Cùng 5 brief của Gate 0 đều ra MP4 9:16 có phụ đề và đạt hậu điều kiện. Phần người chưa xong: chưa có phiếu duyệt C01–C09, phút công hay số hóa đơn, nên Gate 2 chưa thể kết luận đạt mục tiêu Gate 0 và D-07 chưa quyết định.

<!-- id:s1.b2 -->
Table: Checklist Gate 2 (08 · Gate 2)
| Mục | Trạng thái | Bằng chứng |
| --- | --- | --- |
| Kiểm OmniRoute trước việc 7 | Xong | `/v1/models` có cả hai model; JSON schema `strict` chạy qua gateway (25/10/2026); gọi model id đầy đủ |
| Giới hạn chi tiêu ở upstream (D-03) | OpenRouter 5 USD (Tuan đặt 25/10/2026); codex là gói thuê bao | 05 · OmniRoute |
| Test quét import xanh | Xong | `tests/test_import_boundary.py` (4 test, kèm kiểm bản vendor trùng `mpt` 1.3.9) |
| Test core với bản giả | Xong | `tests/test_pipeline.py`: resume, retry, mồ côi → `unknown`, poll sau resume, sửa scene, T11, T12 (17 test) |
| 5 brief ra MP4 9:16 có phụ đề, đạt hậu điều kiện | Xong | Bảng ở [Kết quả chạy](#s3) |
| Phiếu duyệt C01–C09, phút công, so mục tiêu Gate 0 | **Chờ Tuan** | [Việc của Tuan](#s6) |
| Quyết định đi tiếp hoặc quay lại MPT (D-07) | **Chờ Tuan** | Cần phiếu duyệt và hóa đơn |

<!-- id:s2 -->
## Đã xây

<!-- id:s2.b1 -->
Bố cục theo [02 · Quy tắc phụ thuộc](../../src/design/02-architecture.md#s5). Chỉ `studio/providers/mpt/` import `app.*`; core không import adapter cụ thể. `studio/vendor/video_project.py` là bản copy không sửa của MPT `82bb4a7` (= `mpt` 1.3.9), kèm `LICENSE-MPT`. Môi trường là một `uv.lock` chung với `mpt==1.3.9`, chạy Python 3.13 (như pilot); FFmpeg 8.0.1 hệ thống.

<!-- id:s2.b2 -->
```sh
uv sync                                   # Python 3.13, mpt==1.3.9
uv run pytest                             # 21 test, khoảng 2 phút 20 giây, 0 USD
uv run python -m studio task new --brief briefs/brief-01.json
uv run python -m studio worker --once     # preflight rồi chạy tới gate
uv run python -m studio task show t_0001 --runs
uv run python -m studio gate t_0001 approve --note "..."
uv run python -m studio review t_0001 --checklist review.json --minutes 9
uv run python -m studio gate t_0001 approve
```

<!-- id:s2.b3 -->
Table: Quyết định khi code, đã ghi lại vào tài liệu thiết kế
| Chủ đề | Quyết định | Lý do |
| --- | --- | --- |
| Gate final trỏ tới đâu | Bản render ứng với input_hash của selected hiện tại, không phải thư mục `r_NNN` mới nhất | Test bắt lỗi: chọn lại ảnh cũ thì render cũ được dùng lại; nếu trỏ thư mục mới nhất thì người sẽ duyệt nhầm bản |
| Clip dùng lại | Khi `animate` bỏ qua vì đã có clip cùng đầu vào, luồng chính chọn lại clip đó | Clip là đầu ra dẫn xuất của ảnh và giọng selected |
| Provider chặn nội dung | `ContentRefused` (lớp con của `QualityFail`); dòng chi phí `failed`; hết lượt thì `retries_exhausted` | Gemini chặn moderation một prompt vô hại ở brief-02; ánh xạ cũ (`Fatal`) làm hỏng cả task t_0002 |
| Nhân vật khi chưa có series | `ScenePlan.cast`: LLM mô tả nhân vật một lần, `prompt.py` ghép y nguyên vào mọi scene | 5 brief không thuộc series nào; Series Bible đầy đủ thuộc Gate 3 |
| `LLMProvider` | `generate_json(prompt, schema, name) -> dict`; prompt và schema do `prompt.py` viết, bước dựng ScenePlan | Adapter mỏng, dùng lại được cho việc chấm điểm sau này |
| `SubtitleAligner` | `align(audio, script) -> [{start, end, text}]` | Bước subtitles gom cue theo câu; mốc tuyệt đối chốt ở render từ thời lượng scene renderer đo |
| Prompt ảnh khi thử lại | Chỉ thêm ghi chú của người; bị chặn nội dung thì thêm gợi ý an toàn tiếng Anh | Lý do kỹ thuật (kích thước) không giúp model ảnh |

<!-- id:s3 -->
## Kết quả chạy

<!-- id:s3.b1 -->
Chạy ngày 25/10/2026 trên máy Tuan, cấu hình `studio.toml` mặc định: `codex/gpt-5.6-luna` cho ScenePlan, `openrouter/google/gemini-3.1-flash-image-preview` cho keyframe (không ảnh tham chiếu vì không có series), Edge TTS `vi-VN-NamMinhNeural` tốc độ 1.0, nhạc tắt.

<!-- id:s3.b2 -->
Table: 5 brief qua studio (số đo từ manifest và costs.jsonl)
| Brief | Task | Scene | Từ | Thời lượng (giây) | Từ/giây đo được | Lượt gọi plan | Ảnh sinh được | Ảnh bị chặn |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | t_0001 | 4 | 167 | 53,33 | 3,13 | 1 | 4 | 0 |
| 02 | t_0006 | 4 | 157 | 49,68 | 3,16 | 1 | 4 | 5 |
| 03 | t_0003 | 5 | 150 | 48,27 | 3,11 | 1 | 5 | 0 |
| 04 | t_0004 | 5 | 168 | 56,70 | 2,96 | 2 | 5 | 0 |
| 05 | t_0005 | 5 | 162 | 54,00 | 3,00 | 1 | 5 | 0 |
| 02 (hỏng) | t_0002 | 5 | — | — | — | 1 | 2 | 1 |

<!-- id:s3.b3 -->
- Mọi bản `r_001` đều đạt hậu điều kiện: giải mã toàn bộ không lỗi, đúng 1080×1920, có audio, ≤ 60 giây. Cả 5 cũng nằm trong khoảng mềm 45–58 giây.
- Tốc độ đọc đo được 2,96–3,16 từ/giây, khớp số đo pilot (2,81–3,03). Vì vậy `wps = 2,9` giữ nguyên, và mọi ScenePlan rơi vào khoảng mục tiêu.
- Phụ đề lấy mốc từng từ của Edge TTS, gom theo câu. Mốc tuyệt đối tính từ thời lượng từng scene mà `video_project` đã dựng. Trong T01 với bản giả, câu cuối của scene cuối kết thúc trước khi video hết.
- t_0004: ScenePlan lượt 1 có 188 từ (≈ 64,8 giây) và bị kiểm `plan` từ chối. Lượt 2 nhận lý do đó trong prompt và đạt.
- brief-02: Gemini chặn moderation đúng cảnh "pin yếu, robot giữ ấm chậu" ở cả hai task. t_0002 hỏng vì ánh xạ cũ là `Fatal`. Ở t_0006, s02 lượt 1 và s03 lượt 1–4 bị chặn. Claude diễn đạt lại visual s03 (ScenePlan rev 2), rồi lượt 5 đạt. Mầm cây chỉ xuất hiện ở s04, tức lỗi C06 của pilot không lặp lại theo kiểm của agent; Tuan vẫn chấm C06.
- Toàn bộ bước miễn phí của một task (voice, subtitles, animate, render) mất khoảng 40–60 giây; tổng thời gian máy mỗi task từ plan tới gate final khoảng 1,5–2 phút, chưa tính chờ người.

<!-- id:s3.b4 -->
Table: Chi phí
| Mục | Số lượng | Giá |
| --- | --- | --- |
| Ảnh OpenRouter có kết quả | 25 | Response không có `usage`, OmniRoute không ghi chi phí; `est_usd = null`. **Đối chiếu ở openrouter.ai → Activity** |
| Ảnh bị chặn (moderation) | 6 | Ghi `failed`; cần xác nhận trên Activity là không tính phí |
| Lượt gọi LLM (codex, gói thuê bao) | 7, tổng 30.198 token | Trừ vào hạn mức gói, không tính theo lượt |
| Edge TTS | 23 đoạn | 0 USD |
| `unknown` | 0 | — |

<!-- id:s4 -->
## Nhận xét kiểm kỹ thuật của agent

<!-- id:s4.b1 -->
Claude đã duyệt gate `keyframes` của cả 5 task sau khi xem contact sheet, với ghi chú trong `gate_decisions`. Phép kiểm này chỉ là kỹ thuật (chữ trong ảnh, lỗi hình, lộ twist rõ ràng); nó không thay phiếu duyệt và không tính vào phút công. Các điểm Tuan nên chú ý khi chấm:

<!-- id:s4.b2 -->
- t_0001 (C07): bóng người ở s01 tóc dài, còn s02–s04 trông như nam. C06: "trạm ngoài không gian" chỉ thấy nội thất hành lang, không có cảnh bên ngoài trạm.
- t_0003 (C07): mặt đất ở s01 màu đỏ, s02–s05 màu xám.
- t_0006 (C07): robot ở s03 cao, có đầu tách rời; các scene khác là robot bánh xích thấp. Chậu đổi từ đen sang đất nung.
- C05: phụ đề xuống dòng theo âm tiết, nên có thể tách một từ ghép (ví dụ "mắc / kẹt").
- Không có ảnh tham chiếu nên nhân vật chỉ giữ được bằng mô tả văn bản. Đây là dữ liệu cho T09 ở Gate 3 (character sheet trong Series Bible).

<!-- id:s5 -->
## Giới hạn của báo cáo này

<!-- id:s5.b1 -->
- Chưa có phiếu duyệt hay `human_minutes`, nên không có số "video được duyệt", phút công hay chi phí trên mỗi video được duyệt; các số này để `null`, không báo bằng 0.
- Chi phí ảnh chưa có số tiền vì gateway không trả `usage`.
- Gate `keyframes` do agent duyệt, nên phút công đo ở gate `final` không gồm thời gian người xem keyframe.
- T04–T07 và T09 thuộc Gate 3. Các test core tương ứng với bản giả đã chạy; phiên bản đầy đủ chưa chạy.
- MP4 không commit (nằm trong `data/`); SHA-256 ở `evidence/final-sha256.txt`.

<!-- id:s6 -->
## Việc của Tuan

<!-- id:s6.b1 -->
```chk
- [ ] Xem 5 file `data/tasks/t_000{1,3,4,5,6}/renders/r_001/final_9x16.mp4`, chấm C01–C09 theo [g0-plan](../g0-g1/g0-plan.md), kèm timestamp
- [ ] Ghi phiếu cho từng task: chép `review-template.json`, rồi chạy `python -m studio review t_000N --checklist <file> --minutes <phút chủ động>`
- [ ] Duyệt hoặc từ chối gate final: `python -m studio gate t_000N approve|reject`; muốn sửa thì `scene edit`, `rerun`, `select` (06 · CLI)
- [x] Đối chiếu chi phí: toàn dự án (Gate 2 + 3) 5,20 USD trên OpenRouter; chưa tách riêng Gate 2 và chưa ghi vào `evidence/pilot-runs.jsonl`
- [ ] Quyết định D-07: so với mục tiêu Gate 0 (ít nhất 3/5 được duyệt, ≤ 15 phút chủ động mỗi video được duyệt, tổng ≤ 5 USD)
- [ ] Điều khoản gói codex khi dùng qua gateway cho sản xuất video (05 · việc cần kiểm)
```

<!-- id:s7 -->
## Bằng chứng

<!-- id:s7.b1 -->
- [evidence/pilot-runs.jsonl](evidence/pilot-runs.jsonl): một dòng mỗi task, theo mẫu nhật ký pilot.
- `evidence/t_000N/`: brief, ScenePlan revision, `assets.json`, `costs.jsonl`, manifest và `full.ass` của `r_001`, contact sheet keyframe.
- [evidence/step_runs.json](evidence/step_runs.json), [evidence/gate_decisions.json](evidence/gate_decisions.json): xuất từ `data/studio.db`.
- [review-template.json](review-template.json): mẫu phiếu duyệt C01–C09.
