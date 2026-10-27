# 02 — User Journeys dành cho người chưa biết Igniterra

## Đối tượng và tiêu chí UX

**Persona U0:** biết mục tiêu (tạo video AI hoặc dùng workflow ảnh), **không biết JSON, prompt schema, CLI, SHA-256, provisioning, API export**. Một người vận hành, chi phí thấp, có thể dừng và quay lại. **Persona U1:** người dùng kỹ thuật đang sử dụng hệ thống, cần giữ màn Nâng cao và link tới logs/config.

Nguyên tắc: (1) Mỗi bước trả lời "Tôi đang ở đâu? Tôi cần làm gì? Bấm tiếp sẽ có phí không?"; (2) Mỗi trạng thái có next-action; (3) Không âm thầm gọi provider/trả phí hoặc xóa dữ liệu; (4) Hướng dẫn chỉ hiện nếu có blocker; (5) Không bắt đầu từ một form JSON hoặc yêu cầu CLI.

## Sơ đồ 0 — Toàn cục

Nguồn D2: [`diagrams/00-first-use-overview.d2`](diagrams/00-first-use-overview.d2).

```d2
 direction: down
 start: "Open Igniterra"
 health: "Workspace readiness"
 goal: "Choose goal" {shape: diamond}
 video: "Create AI video"
 workflows: "Use ComfyUI workflow"
 assets: "Manage recurring characters"
 brief: "Create or select Brief"
 bible: "Optional Series Bible + reference images"
 task: "Create Task (cost warning)"
 review: "Review Storyboard / Final"
 result: "Download verified video"
 import: "Import GUI + API workflow JSON"
 manifest: "Review / approve dependencies"
 rental: "Find and rent GPU (cost confirmation)"
 run: "Run workflow + verify output"
 output: "Download verified image / output"
 start -> health -> goal
 goal -> video -> brief -> bible -> task -> review -> result
 goal -> workflows -> import -> manifest -> rental -> run -> output
 goal -> assets -> bible
```

## Journey J01 — Tạo video đầu tiên từ ý tưởng (P0)

**Điểm vào:** `Tasks` / dashboard rỗng → CTA **Tạo video đầu tiên**. 

1. Chọn **Tạo Brief mới** hoặc **Dùng Brief mẫu**. Brief mới có form: tên video; câu hook; các beats/diễn biến (ít nhất một); twist/kết; ngôn ngữ; dự kiến thời lượng; số scene; nhân vật và nội dung bắt buộc (optional). Diễn giải ngắn mỗi trường.
2. **Lưu nháp** (chưa tốn phí); **Xem trước Brief** (review validation); **Tạo task** là nút riêng, hiện cảnh báo worker có thể tiêu phí.
3. Hỏi có muốn áp dụng **Series Bible** không: bỏ qua được. Nếu dùng, hiển thị tình trạng nhân vật và ảnh đã duyệt, có link bổ sung trước khi tạo task; không block nếu pipeline/brief không bắt buộc refs.
4. Task được tạo: hiển thị status `pending`, worker status và step tiếp theo; không giả định là đang chạy hay đã có progress %.
5. ScenePlan/keyframes xuất hiện: người dùng xem từng scene, **chọn** ảnh, hoặc upload ảnh, ghi nguồn/quyền; nếu ảnh chưa có, có CTA tạo lại **có phí**.
6. Gate keyframes: nêu số scene đạt/thiếu; chỉ duyệt khi điều kiện server cho phép; sau duyệt worker xử lý.
7. Final render: xem MP4, hoàn thành checklist C01–C09, ghi phiếu, **duyệt cuối** tách biệt, tải file thật.
8. Trường hợp lỗi: Failed → Task Detail; Unknown call → chỉ retry/skip sau khi đọc cảnh báo có thể bị tính phí; Không có worker → hướng dẫn chạy worker (không bịa button start worker).

**Hoàn thành:** task `completed`, `final_9x16.mp4` có thật, review được lưu, người dùng truy cập file output. **Không** tự động đăng mạng xã hội (backend chưa hỗ trợ).

## Journey J02 — Tạo Series Bible và nhập ảnh (P0)

**Điểm vào:** Series → **Tạo Series** → chọn **Tạo mới** / **Khởi tạo từ Task đã có**.

1. Tên series (tên dễ đọc + slug hợp lệ), mô tả phong cách; giá trị mặc định có giải thích và preview. Lưu để tạo Bible.
2. Bước **Thêm nhân vật**: tên mã/khóa, mô tả rõ hình dáng, trang phục. Cho tạo nhiều nhân vật nhưng không bắt buộc.
3. Mỗi nhân vật có hai đường vào: **Tải ảnh có sẵn** hoặc **Tạo ảnh bằng AI (có phí)**. Upload có preview, nguồn ảnh, quyền sử dụng; kiểm PNG/JPEG/WebP, hạn chế kích thước, lỗi loại file trước submit.
4. Upload/generate → **Candidate**, chưa phải ảnh tham chiếu sử dụng. Người dùng chọn 1–3 ảnh tốt, gắn góc nhìn (front/side/three-quarter nếu metadata có), sau đó **Duyệt ảnh và tạo character sheet** (nền tảng cho pipeline). Code hiện chấp nhận ≥1 ảnh; nên khuyên 2–3 ảnh chứ không ghi bắt buộc.
5. Preview sheet thật, nhãn **Đã duyệt**, xem source/rights, link các task dùng series. Cho sửa style, giọng, phát âm. Cảnh báo thay đổi có thể ảnh hưởng task sau nhưng không âm thầm rerun mọi task cũ.
6. Trường hợp lỗi: ảnh không hợp lệ; job AI pending/failed; thư viện trống; thiếu nguồn/quyền; sheet FFmpeg lỗi; người dùng có thể thử lại không làm mất candidate.

**Hoàn thành:** Series có Bible lưu, nhân vật có refs/sheet đã duyệt. Không đồng nhất "Tạo Series xong" với "Ảnh đã sẵn sàng".

D2: [`diagrams/01-series-reference-images.d2`](diagrams/01-series-reference-images.d2).

## Journey J03 — Nhập, cập nhật và sử dụng Workflow (P0–P1; **đơn giản 2 bước**)

**Điểm vào:** ComfyUI Workflows → **Nhập workflow** (workflow mới) hoặc Workflow Detail → **Thêm phiên bản** (mặc định giữ đúng `workflow_id`, không hỏi lại). Không cần thuê GPU để nhập.

1. **Chọn file**: tên mặc định từ file (sửa được); kéo/thả `Save (GUI)` và `Export (API)` hoặc chọn một file. Giải thích 1 câu: GUI giúp đọc model/node, API dùng chạy. Người dùng không cần khai SHA/manifest ngay. UI chỉ yêu cầu file bổ sung nếu cần.
2. **Kiểm tra & Lưu**: parser backend hiển thị `Hợp lệ / Thiếu API / Thiếu dependency / Đã tồn tại`; so hash toàn thư viện và đưa CTA `Mở workflow đang có` thay vì âm thầm tạo trùng. Nếu cập nhật thì hiển thị summary thay đổi và xác nhận tạo revision bất biến. Không tự duyệt, không thuê GPU, không sửa revision cũ.
3. **Review dependency** chỉ khi có blocker; form cấu trúc model/custom node, hiện lý do từng lỗi, người nâng cao mới thấy canonical JSON/SHA. `attach_manifest` và `approve_manifest` cần web route mới nhưng dùng lại service kiểm tra SHA/license. Không đánh đồng approved với installed.
4. **Chạy**: trên Workflow Detail hiển thị cùng một form cho image/motion, các input xuất hiện theo input contract của revision đã khai báo/được duyệt: prompt (nếu có), ảnh (nếu workflow cần), duration (nếu motion), advanced tùy chọn. Người mới **không nhập raw node IDs, workflow ID, manifest hoặc API payload**. Chọn GPU ready bằng tên/giá, xác nhận phí, bấm **Chạy**. Nếu thiếu điều kiện, một CTA chỉ đến blocker đầu tiên.
5. **Theo dõi & nhận output**: dùng ComfyService chạy, Monitor hiển thị stage thực; chỉ cho tải output sau khi verified. Unknown chỉ reconcile, không submit lại mù. Revision cũ và lịch sử run giữ nguyên.

**Quản lý tối thiểu:** xem library, tìm kiếm/lọc Image hoặc Motion, mở workflow, nhập revision mới, xem lịch sử, trạng thái dependency, chạy. Đổi tên/archive chỉ P1 khi đã cần; không hard delete, không graph editor trong Igniterra.

**Giới hạn đã xác minh:** `start_run()` hiện chưa nhận dynamic bindings; chỉ có `VideoProvider.fake`, vì vậy **Motion Workflow gắn vào Storyboard là tính năng chưa triển khai**. Độc lập chạy motion cần input schema mapping + upload input + kiểm file video E2E. Không bật UI nếu chưa có backend tương ứng.

**Hoàn thành:** đúng `workflow_id/revision`, user biết lý do khi blocked, run có output xác minh; không duplicate ngoài ý muốn, không thay đổi config ngầm.

D2: [`diagrams/02-workflow-lifecycle.d2`](diagrams/02-workflow-lifecycle.d2).

## Journey J03b — Chọn Motion cho Scene (P1 riêng)

1. Trong Storyboard, `Chuyển động`: **Hiệu ứng ảnh tĩnh (local)** / **AI Motion ComfyUI** / **Dùng clip đã tải lên**.
2. Nếu chọn AI Motion: dropdown chỉ hiện revision workflow **có API, manifest approved, input/output contract thuộc loại Motion**. Hiển thị một ảnh source đã chọn; input prompt/duration tự điền từ scene khi mapping tồn tại; có thể sửa trước submit nếu phạm vi cho phép.
3. GPU ready? Nếu không: liên kết tới GPU Rentals và khi quay về vẫn giữ scene, workflow, revision. Hiện giá ước tính và xác nhận API/GPU trước khi chạy.
4. Chạy theo **cùng ComfyService**, thông qua một `VideoProvider` adapter mỏng; output `.mp4` qua kiểm định độ dài/tỉ lệ như `studio/steps/motion.py`. Khi không có adapter/graph hợp lệ, tùy chọn AI Motion bị vô hiệu hóa **kèm lý do**; không giả thành đã tạo clip. Nếu provider báo lỗi, chỉ fallback theo policy hiện tại và thông báo rõ.

**Không tạo thêm thư viện "Motion Workflows" hay runner thứ hai.** Image/Motion khác nhau ở `workflow_type` + mapping input/output, không khác luồng lifecycle.

## Journey J04 — Thuê GPU mà không biết GPU/Vast (P0)

1. Khi chọn **Chạy workflow** không có GPU ready: CTA tới GPU Rentals, giữ ngữ cảnh workflow/revision qua link query chỉ đọc (new UI integration, kiểm chuyển hướng an toàn).
2. Hiển thị prerequisites: provider đã bật? provisioning manifest đã duyệt? worker online? hạn mức giá/giờ/ngân sách? Nếu thiếu, nêu rõ và link hướng dẫn.
3. Tìm GPU: default VRAM/disk và giới hạn giá, giải thích VRAM/disk; show offer verified và estimated compute/disk/bandwidth. Không tự query provider khi mở trang.
4. Chọn offer → **xác nhận báo giá mới**, thời hạn và budget. Thao tác có phí rõ ràng.
5. Theo dõi provisioning từng bước: host → transport auth → complete marker → ComfyUI stats → smoke. Thao tác reconcile có mục đích; `create_unknown` thì không cho thuê lại mù quáng.
6. `ready` → trở về Workflow và chọn instance. Khi xong: download/verify output trước khi destroy. `stopped` còn tốn storage; `destroy_unknown` chưa chứng minh dừng tính phí; `destroyed` mới kết thúc.

D2: [`diagrams/03-cost-safe-gpu-flow.d2`](diagrams/03-cost-safe-gpu-flow.d2).

## Journey J05 — Xử lý sự cố (P0)

Monitor hiển thị **Cần xử lý trước**: Task waiting/failed/unknown, GPU create_unknown/destroy_unknown/provision_failed/over budget, ComfyUI submit_unknown/blocked/output_failed. Mỗi thẻ có 1 CTA đúng tới màn xử lý gốc, hiển thị **lý do và hậu quả về phí**. `Làm mới bây giờ` tách khỏi `Tự cập nhật`. Không xóa các trạng thái `unknown` bằng cách giả lập success.

## Mẫu empty state phải có

| Trang | Empty state | CTA duy nhất |
|---|---|---|
| Tasks | Chưa có video; giải thích Brief là gì | Tạo Brief đầu tiên |
| Series | Chưa có Series Bible | Tạo Series |
| Series Character | Chưa có nhân vật | Thêm nhân vật |
| Series Candidate | Chưa có ảnh | Tải ảnh hoặc tạo ảnh AI |
| Workflow Library | Chưa có workflow | Nhập workflow |
| Workflow blocked | Thiếu API JSON/manifest/ready GPU | Đi tới bước thiếu đầu tiên |
| GPU | Không có máy đang thuê | Tìm GPU (có cảnh báo phí) |
| Monitor | Chưa có job nào | Mở Tasks / ComfyUI |

## UX acceptance định lượng (mục tiêu thử nghiệm, không phải kết quả đo)

- 5 người chưa dùng thử đi từ thư viện rỗng đến **Brief hợp lệ** mà không mở JSON hoặc CLI: mục tiêu ≥4/5; ghi rõ nơi dừng.
- Tạo Series với 1 ảnh upload + preview + sheet đã duyệt: ≥4/5 làm được không trợ giúp.
- Nhập workflow GUI + API, giải thích blocker manifest/GPU bằng lời của mình: ≥4/5.
- Không có lời gọi trả phí khi chỉ tạo/lưu Brief, tạo Series, nhập workflow, duyệt manifest.
- Mọi nút hiển thị có handler/route thật hoặc **disabled kèm lý do**; không có action demo hay fake success.
