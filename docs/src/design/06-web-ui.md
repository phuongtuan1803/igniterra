---
id: IGN-UI
title: Igniterra — Giao diện review và CLI
type: design
ver: "2.0"
status: draft
date: 2026-10-22
owner: Tuan
sum: CLI ở Gate 2 và một ứng dụng web nhỏ do server render (FastAPI + Jinja2 + HTMX) từ Gate 3; màn hình quan trọng nhất là Storyboard, nơi xem kịch bản theo scene, chọn ứng viên, sửa và chạy lại ở cùng một chỗ.
kv: [[Nguồn, "ADD-AIVIDEO-003 v3.0 · mục 8"], [Thành phần, "studio/cli.py, studio/web/"], [Gate xây, "2 (CLI), 3 (trang review, Settings)"]]
rev:
  - ["1.0", 2026-10-19, "Bản đầu (Giao diện web và Task API), tách từ ADD-AIVIDEO-001 v1.1"]
  - ["2.0", 2026-10-22, "Viết lại theo ADD-AIVIDEO-003 v3.0: CLI trước, 4 trang review, chọn ứng viên selected, sửa tạo revision, quyết định lượt gọi unknown; bỏ ngân sách và Settings ghi profile"]
---

<!-- id:s1 -->
## Kiến trúc giao diện

<!-- id:s1.b1 -->
Không có bước build frontend. CLI và web gọi cùng một lớp hành động trong core (`gates.py`, `taskdir.py`), nên mọi thứ làm được trên web cũng làm được bằng CLI. Ảnh, audio, video hiển thị bằng thẻ `<img>`, `<audio>`, `<video>` trỏ tới file trong `data/`.

<!-- id:s1.b2 -->
```d2
# cap: CLI và web dùng chung lớp hành động của core; không ai trong hai gọi provider
direction: right
op: Người vận hành {class: hands}
cli: "CLI\npython -m studio ...\n(Gate 2)" {class: plain}
web: "Web (Gate 3)" {
  class: zone
  pages: "Route HTML\nJinja2" {class: brain}
  part: "Partial HTMX\n(trạng thái, lưới scene)" {class: brain}
  files: "/files/*\nphục vụ data/ chỉ đọc" {class: plain}
}
act: "Lớp hành động (core)\ntạo task, quyết định gate,\nchọn ứng viên, sửa scene,\nupload, chạy lại" {class: gate}
db: "studio.db" {shape: cylinder; class: data}
fs: "data/tasks/" {shape: cylinder; class: data}
op -> cli
op -> web.pages
cli -> act
web.pages -> act: "hx-post"
web.part -> db: đọc
act -> db
act -> fs: "rev mới, selected,\nứng viên upload"
web.files -> fs
```
<picture>
<source media="(prefers-color-scheme: dark)" srcset="06-web-ui.figs/s1.b2-dark.svg">
<img alt="CLI và web dùng chung lớp hành động của core; không ai trong hai gọi provider" src="06-web-ui.figs/s1.b2-light.svg">
</picture>

<!-- id:s1.b3 -->
```req
FR-01
FR-08
FR-10
FR-11
FR-12
FR-14
NFR-07
```

<!-- id:s2 -->
## Sơ đồ trang

<!-- id:s2.b1 -->
```d2
# cap: 4 trang; Storyboard là nơi làm việc chính ở gate keyframes và gate final
direction: right
tasks: "Tasks\n(danh sách)" {class: brain}
new: "Tạo task\n(brief, series,\nNâng cao: ghi đè adapter/model)" {class: plain}
story: "Storyboard\n(lưới scene)" {class: hands}
detail: "Task detail\n(dòng thời gian, log, chi phí)" {class: brain}
series: "Series\n(Series Bible)" {class: brain}
settings: "Settings\n(adapter mặc định, Gate 3)" {class: plain}
tasks -> new: "Tạo task"
tasks -> story: "task đang chờ gate"
tasks -> detail: "chọn task"
story -> detail: "xem log, chi phí"
detail -> story: "sửa scene"
story -> series: "nhân vật, giọng của series"
settings -> new: "mặc định cho task mới"
```
<picture>
<source media="(prefers-color-scheme: dark)" srcset="06-web-ui.figs/s2.b1-dark.svg">
<img alt="4 trang; Storyboard là nơi làm việc chính ở gate keyframes và gate final" src="06-web-ui.figs/s2.b1-light.svg">
</picture>

<!-- id:s3 -->
## Trang và hành động

<!-- id:s3.b1 -->
Table: Trang, nội dung và hành động
| Trang | Hiển thị | Hành động |
| --- | --- | --- |
| Tasks | Danh sách task: tiêu đề, trạng thái, bước hiện tại, chi phí đã ghi | Tạo task (brief, series, phần Nâng cao để ghi đè adapter/model) |
| Storyboard | Lưới scene: ảnh được chọn, số ứng viên, narration, góc máy, revision, chi phí | Chọn ứng viên, sửa scene, upload asset riêng, chạy lại, duyệt/từ chối gate |
| Task detail | Dòng thời gian các bước, input/output, log, lỗi, chi phí từng lượt gọi, video cuối | Chạy lại bước, quyết định với lượt gọi `unknown`, tải toàn bộ artifact |
| Series | Series Bible: style, nhân vật kèm ảnh tham chiếu, giọng, từ vựng camera, từ điển phát âm | Tạo ảnh tham chiếu, chọn ảnh được duyệt |
| Settings (Gate 3) | Adapter mặc định cho từng năng lực, giới hạn song song | Chọn adapter; kiểm adapter khởi tạo được trước khi lưu `studio.toml` |

<!-- id:s4 -->
## Storyboard

<!-- id:s4.b1 -->
Storyboard gộp kịch bản, ứng viên và hành động sửa vào một chỗ. Bên trái là lưới thẻ scene; bên phải là khung sửa của scene đang chọn với dải ứng viên.

<!-- id:s4.b2 -->
```d2
# cap: Bố cục Storyboard: lưới thẻ scene, khung sửa và dải ứng viên của scene đang chọn
grid-columns: 2
grid-gap: 24
cards: "Lưới scene · t_0042 rev 3 · waiting (gate: keyframes)" {
  class: zone
  grid-columns: 2
  s01: "s01 · still\n[ảnh selected]\nwide · eye_level · static\n1 ứng viên · 0,067 USD" {class: plain}
  s02: "s02 · still\n[ảnh selected]\nmedium · low · pan_left\n1 ứng viên · 0,067 USD" {class: plain}
  s03: "s03 · still (đang chọn)\n[img_s03_a2]\nclose_up · eye_level · push_in\n3 ứng viên · rev 3" {class: gate}
  s04: "s04 · still\n[ảnh selected]\nmedium · high · static\n1 ứng viên · lệch revision" {class: bad}
}
panel: "Khung sửa · s03" {
  class: zone
  grid-columns: 1
  f1: "narration · visual\n(văn bản)" {class: brain}
  f2: "size · angle · movement\n(dropdown từ vựng camera)" {class: gate}
  f3: "mode: still | motion\ncharacters: kai" {class: gate}
  f4: "Dải ứng viên\nimg_s03_a1 · img_s03_a2 ✓ · up_s03_1\n[Chọn] [Upload]" {class: hands}
  f5: "[Lưu → rev mới] [Chạy lại + ghi chú]\n[Duyệt gate] [Từ chối]" {class: hands}
}
```
<picture>
<source media="(prefers-color-scheme: dark)" srcset="06-web-ui.figs/s4.b2-dark.svg">
<img alt="Bố cục Storyboard: lưới thẻ scene, khung sửa và dải ứng viên của scene đang chọn" src="06-web-ui.figs/s4.b2-light.svg">
</picture>

<!-- id:s4.b3 -->
- Thẻ scene mang huy hiệu "lệch revision" khi ứng viên selected có `plan_rev` cũ hơn nội dung hiện tại của scene.
- Lưu khung sửa luôn tạo ScenePlan revision mới; nút chỉ bật khi có thay đổi.
- Dropdown góc máy chỉ chứa khóa có trong `camera` của Series Bible, nên người cũng không viết lệch được.
- Với audiobook (Gate 4), Storyboard hiển thị dạng danh sách đoạn: narration, speaker, nút nghe.

<!-- id:s5 -->
## Người dùng can thiệp thế nào

<!-- id:s5.b1 -->
Mọi thay đổi là bản ghi mới, không ghi đè, nên luôn quay lại được bản cũ. Phạm vi chạy lại của từng hành động ở [03 · Gate, sửa và chạy lại](03-runner.md#s7).

<!-- id:s5.b2 -->
Table: Hành động và kết quả trong hệ thống
| Hành động | Kết quả trong hệ thống |
| --- | --- |
| Chọn ứng viên khác | Đổi `selected` trong `assets.json`; chỉ `render` chạy lại |
| Sửa narration, visual, góc máy (dropdown theo từ vựng camera), mode | Tạo ScenePlan revision mới; chỉ scene bị sửa sinh lại ảnh và/hoặc giọng |
| Upload ảnh, clip, nhạc của riêng mình | Thêm ứng viên `provider: user` và chọn nó |
| Chạy lại kèm ghi chú | Ghi chú đưa vào prompt của lượt sau |
| Ghi đè adapter/model cho một task | Lưu trong task; không đổi cấu hình chung; model thật được ghi vào ứng viên và chi phí |
| Quyết định lượt gọi `unknown` | Sinh lại mục đó hoặc bỏ qua; quyết định ghi vào `gate_decisions` |

<!-- id:s5.b3 -->
```d2
# cap: Sửa visual của s03 trên Storyboard: revision mới, worker chỉ chạy lại s03 rồi dừng lại ở gate
shape: sequence_diagram
op: Người vận hành
ui: Storyboard
act: "Lớp hành động"
fs: "data/tasks/t_0042"
db: "studio.db"
wk: Worker
op -> ui: "sửa visual s03: hands out of frame"
ui -> act: "save_scene(s03, fields)"
act -> act: "kiểm schema ScenePlan, khóa camera"
act -> fs: "plan/rev_003.json (parent 2)"
act -> db: "tasks.plan_rev = 3; task → pending"
wk -> db: "nhận task"
wk -> wk: "input_hash đổi chỉ ở keyframes/s03"
wk -> fs: "img_s03_a2 → selected"
wk -> db: "gate keyframes (rev 3) → waiting"
ui -> op: "thẻ s03 cập nhật qua HTMX"
```
<picture>
<source media="(prefers-color-scheme: dark)" srcset="06-web-ui.figs/s5.b3-dark.svg">
<img alt="Sửa visual của s03 trên Storyboard: revision mới, worker chỉ chạy lại s03 rồi dừng lại ở gate" src="06-web-ui.figs/s5.b3-light.svg">
</picture>

<!-- id:s6 -->
## Task detail và lượt gọi không rõ kết quả

<!-- id:s6.b1 -->
Task detail là nơi vận hành và debug: dòng thời gian các bước, mỗi bước mở được input, output, log, lỗi, chi phí từng lượt gọi. Khi task `waiting` vì `unknown_call`, trang hiện câu hỏi kèm dòng `pending` gốc.

<!-- id:s6.b2 -->
```text
┌─ t_0042 · Tín hiệu từ năm 2126 ──── waiting (unknown_call) ── đã ghi ~$0,60 ─┐
│ ⚠ Lượt gọi t_0042:s06:keyframes:1 (gemini · nano-banana-2) gửi lúc 10:14:07, │
│   không có kết quả. Có thể đã bị tính phí.                                    │
│   [Sinh lại s06]  [Bỏ qua, giữ ứng viên hiện có]                              │
├────────────────────────────────────────────────────────────────────────────────┤
│ ✔ plan        1 lượt   $0,06   ▸ input  ▸ ScenePlan rev 1  ▸ log              │
│ ◐ keyframes   s01–s05 ✔  s06 ?  s07–s08 ·                                     │
│ · gate: keyframes · voice · subtitles · render · gate: final                  │
├────────────────────────────────────────────────────────────────────────────────┤
│ costs.jsonl (12 dòng) · log worker (50 dòng cuối) · [Tải toàn bộ artifact]     │
└────────────────────────────────────────────────────────────────────────────────┘
```

<!-- id:s7 -->
## CLI (Gate 2)

<!-- id:s7.b1 -->
CLI là giao diện duy nhất ở Gate 2, đủ để chạy 5 brief ra MP4 và duyệt gate bằng cách mở file. Tên lệnh là đề xuất; chốt khi code.

<!-- id:s7.b2 -->
Table: Lệnh CLI đề xuất
| Lệnh | Làm gì |
| --- | --- |
| `python -m studio task new --brief brief.json [--series lyra7]` | Tạo task `pending`, in id |
| `python -m studio worker` | Chạy worker (vòng lặp) |
| `python -m studio task show t_0042` | Trạng thái, bước, `wait_reason`, đường dẫn ứng viên, chi phí |
| `python -m studio gate t_0042 approve\|reject [--note ...]` | Quyết định gate hiện tại |
| `python -m studio select t_0042 s03 img_s03_a2` | Đổi selected |
| `python -m studio scene edit t_0042 s03 --visual "..."` | Tạo ScenePlan revision mới |
| `python -m studio unknown t_0042 s06 retry\|skip` | Quyết định lượt gọi `unknown` |
| `python -m studio run-step keyframes --task t_0042 --scene s03` | Chạy một bước cho một scene khi debug |

<!-- id:s8 -->
## Truy cập và bảo mật

<!-- id:s8.b1 -->
Giao diện chạy ở `localhost` hoặc sau Tailscale/basic auth; API key không hiển thị trên UI. Chọn adapter mặc định làm qua file cấu hình ở Gate 2, qua trang Settings đơn giản từ Gate 3. Không có đa người dùng hay phân quyền; thêm đăng nhập ở lớp web khi có người dùng thứ hai.

<!-- id:s9 -->
## Nguyên tắc giữ UI nhỏ

<!-- id:s9.b1 -->
- Giữ 4 trang và Settings; mọi trang mới phải chứng minh nhu cầu bằng một lỗi đã thấy ở gate trước.
- Không dashboard thống kê: `costs.jsonl` và `jq` đủ cho báo cáo tháng.
- Không workflow editor: danh sách bước là mã Python.
- Không thay trình dựng phim: chỉnh nhịp từng khung phải sửa dữ liệu rồi render lại, hoặc xuất timeline sang trình dựng ngoài.
