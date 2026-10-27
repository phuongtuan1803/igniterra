# Checklist review UX theo màn hình chuẩn

File sinh từ `screens.json` bằng `node docs/ux/spec/build.cjs`; không sửa tay. Cách dùng: [README](README.md).

## S00-start · Bắt đầu

Người mới chọn một trong ba việc và thấy ngay điều gì còn thiếu. Hành trình J01, J02, J03 · giai đoạn 6 · mới. Ảnh chuẩn: [1440](shots/1440-S00-start.png) · [768](shots/768-S00-start.png) · [390](shots/390-S00-start.png).

Phải có:

- [ ] Ba thẻ việc: Tạo video, Dùng ComfyUI, Tạo Series; mỗi thẻ một nút dẫn tới trang thật
- [ ] Khối Trạng thái sẵn sàng chỉ đọc dữ liệu nội bộ: worker, máy GPU, workflow đã duyệt, ngân sách còn lại
- [ ] Mục chưa sẵn sàng có link tới đúng trang sửa

Không được có:

- [ ] Nút khởi động worker hoặc thuê GPU ngay trên trang này
- [ ] Gọi provider khi mở trang
- [ ] Hiện chi phí 0 khi chưa biết giá

## S10-gpu-machines · Máy GPU

Một danh sách chung cho máy thuê Vast.ai và máy cố định (VPS có GPU). Hành trình J04, J06 · giai đoạn 1 · sửa. Ảnh chuẩn: [1440](shots/1440-S10-gpu-machines.png) · [768](shots/768-S10-gpu-machines.png) · [390](shots/390-S10-gpu-machines.png).

Phải có:

- [ ] Cột Loại phân biệt Thuê (Vast.ai) và Cố định (VPS)
- [ ] Máy ready có nút Mở ComfyUI
- [ ] Máy thuê hiện trạng thái tính phí và hạn idle; máy cố định hiện giá tự khai hoặc Không tính, và không cộng vào ngân sách
- [ ] Nút Thêm máy cố định cạnh Tìm GPU thuê

Không được có:

- [ ] Nút Stop/Destroy cho máy cố định
- [ ] Gộp máy cố định vào số 'có thể đang tính phí' của Vast.ai

## S11-machine-add · Thêm máy cố định

Khai một VPS có GPU đã cài ComfyUI để Igniterra dùng như máy ready. Hành trình J06 · giai đoạn 1 · mới. Ảnh chuẩn: [1440](shots/1440-S11-machine-add.png) · [768](shots/768-S11-machine-add.png) · [390](shots/390-S11-machine-add.png).

Phải có:

- [ ] Trường: tên, URL ComfyUI (https), token hoặc mật khẩu truy cập, giá USD/giờ (tùy chọn)
- [ ] Nút Kiểm tra kết nối gọi /system_stats với token và báo GPU, VRAM, phiên bản ComfyUI
- [ ] Chỉ lưu được sau khi kiểm tra thành công
- [ ] Ghi rõ token lưu ở đâu và không hiện lại

Không được có:

- [ ] Chấp nhận URL http công khai không xác thực
- [ ] Tự cài model hay custom node lên máy cố định

## S12-machine-ready · Máy ready: mở ComfyUI

Từ máy ready, mở giao diện ComfyUI gốc ở tab mới và đưa workflow vừa sửa về thư viện. Hành trình J06, J08 · giai đoạn 2 · sửa. Ảnh chuẩn: [1440](shots/1440-S12-machine-ready.png) · [768](shots/768-S12-machine-ready.png) · [390](shots/390-S12-machine-ready.png).

Phải có:

- [ ] Nút chính Mở ComfyUI (tab mới, link trực tiếp)
- [ ] Ghi chú: chạy trong ComfyUI gốc không có kiểm output, nhưng thời gian máy vẫn tính phí và vẫn bị giới hạn idle/ngân sách
- [ ] Hạn idle hiện tại và việc dùng ComfyUI gốc có gia hạn idle
- [ ] Nút Lưu workflow từ máy về thư viện
- [ ] Liên kết Chạy có kiểm (form Igniterra)

Không được có:

- [ ] Hiện token trong URL hiển thị trên trang
- [ ] Báo output đã kiểm cho run chạy trong ComfyUI gốc

## S13-save-from-machine · Lưu workflow từ máy

Chọn workflow đã lưu trong ComfyUI trên máy và nhập thành workflow mới hoặc revision mới. Hành trình J08 · giai đoạn 2 · mới. Ảnh chuẩn: [1440](shots/1440-S13-save-from-machine.png) · [768](shots/768-S13-save-from-machine.png) · [390](shots/390-S13-save-from-machine.png).

Phải có:

- [ ] Danh sách workflow đọc từ máy (tên, giờ sửa)
- [ ] Chọn Workflow mới hoặc Thêm revision cho workflow có sẵn
- [ ] Bước tiếp theo là Kiểm tra & Lưu như nhập file (S15)
- [ ] Có đường dự phòng: tải file JSON lên

Không được có:

- [ ] Ghi đè revision cũ
- [ ] Tự duyệt dependency

## S14-library · Thư viện workflow

Mọi workflow ở một chỗ, lọc theo loại, có mẫu sẵn cho người mới. Hành trình J03, J07 · giai đoạn 3 · sửa. Ảnh chuẩn: [1440](shots/1440-S14-library.png) · [768](shots/768-S14-library.png) · [390](shots/390-S14-library.png).

Phải có:

- [ ] Bộ lọc loại: Tất cả, Ảnh từ chữ, Sửa ảnh, Video từ ảnh, Phóng to, Giọng đọc, Sửa dị dạng, Giữ nhân vật
- [ ] Mỗi dòng: tên, loại, revision mới nhất, trạng thái chạy được, lần chạy gần nhất
- [ ] Khối Workflow mẫu đã duyệt sẵn, nút Dùng mẫu
- [ ] Nút Nhập workflow mở luồng 2 bước

Không được có:

- [ ] Hai thư viện riêng cho ảnh và motion
- [ ] Nút xóa cứng workflow

## S15-import-check · Nhập workflow: Kiểm tra & Lưu

Bước 2/2 của nhập: kết quả kiểm bằng lời dễ hiểu, cảnh báo trùng trước khi lưu. Hành trình J03, J08 · giai đoạn 3 · sửa. Ảnh chuẩn: [1440](shots/1440-S15-import-check.png) · [768](shots/768-S15-import-check.png) · [390](shots/390-S15-import-check.png).

Phải có:

- [ ] Chỉ báo bước 2/2
- [ ] Kết quả: Có bản chạy được (API), Dependency, Loại workflow đoán được để người dùng xác nhận
- [ ] Cảnh báo Đã có workflow cùng nội dung kèm nút Mở bản đang có và Vẫn lưu thành bản riêng
- [ ] Nút Lưu tạo revision bất biến; nói rõ không tốn phí

Không được có:

- [ ] Tự gộp hoặc xóa bản trùng
- [ ] Đòi người mới nhập SHA hay node ID

## S16-workflow-run · Chạy có kiểm

Form chạy sinh từ input mapping của revision; checklist 4 cổng; ước tính phí; tùy chọn tự sửa. Hành trình J07, J12 · giai đoạn 3 · sửa. Ảnh chuẩn: [1440](shots/1440-S16-workflow-run.png) · [768](shots/768-S16-workflow-run.png) · [390](shots/390-S16-workflow-run.png).

Phải có:

- [ ] Checklist 4 cổng: có API, dependency đã duyệt, máy ready, máy có đủ node/model; cổng chưa đạt có một nút tới chỗ sửa
- [ ] Chỉ hiện ô nhập đã được mapping (ví dụ prompt, ảnh, seed, nhân vật từ Series)
- [ ] Chọn máy bằng tên, loại và giá
- [ ] Ước tính phí và thời gian trước khi chạy, ghi là ước tính
- [ ] Tùy chọn Tự kiểm và chạy lại: số lần tối đa, cách kiểm, trần chi phí
- [ ] Nút Chạy cần ô xác nhận phí

Không được có:

- [ ] Ô nhập JSON thô hay node ID ở chế độ cơ bản
- [ ] Nút Chạy bật khi một cổng chưa đạt

## S17-input-mapping · Khai input cho workflow

Người vận hành khai node/input nào thành ô nhập trên form; thay đổi tạo revision mới. Hành trình J07 · giai đoạn 3 · mới. Ảnh chuẩn: [1440](shots/1440-S17-input-mapping.png) · [768](shots/768-S17-input-mapping.png) · [390](shots/390-S17-input-mapping.png).

Phải có:

- [ ] Bảng: Tên ô, Kiểu (chữ, ảnh, số, giọng đọc, nhân vật), Node · input, Bắt buộc, Giới hạn
- [ ] Chọn node từ danh sách đọc được trong API JSON, không gõ tay
- [ ] Ghi rõ lưu mapping là revision mới và cần duyệt lại

Không được có:

- [ ] Sửa mapping tại chỗ trên revision đã duyệt

## S18-run-autofix · Run có tự kiểm và chạy lại

Theo dõi từng lượt: kết quả, đánh giá, lý do chạy lại, chi phí đã dùng so với trần. Hành trình J07, J09 · giai đoạn 3 · mới. Ảnh chuẩn: [1440](shots/1440-S18-run-autofix.png) · [768](shots/768-S18-run-autofix.png) · [390](shots/390-S18-run-autofix.png).

Phải có:

- [ ] Danh sách lượt, mỗi lượt: ảnh, seed, kết quả kiểm (đạt / lỗi gì), chi phí
- [ ] Thanh chi phí đã dùng / trần
- [ ] Dừng khi đạt, khi hết số lượt hoặc chạm trần; ghi lý do dừng
- [ ] Người dùng chọn lượt dùng làm kết quả; chỉ output đã kiểm SHA mới tải được

Không được có:

- [ ] Chạy lại sau trạng thái submit_unknown
- [ ] Vượt trần chi phí

## S19-run-audio · Kết quả giọng đọc

Output âm thanh (giọng đọc tiếng Việt) nghe được, tải được sau khi kiểm. Hành trình J07 · giai đoạn 3 · mới. Ảnh chuẩn: [1440](shots/1440-S19-run-audio.png) · [768](shots/768-S19-run-audio.png) · [390](shots/390-S19-run-audio.png).

Phải có:

- [ ] Trình phát audio
- [ ] Thông tin: thời lượng, định dạng, SHA-256 đã kiểm
- [ ] Văn bản đầu vào để so
- [ ] Nút Dùng cho scene (chuyển sang Storyboard) khi đã kiểm

Không được có:

- [ ] Cho tải khi chưa kiểm

## S20-missing-deps · Thiếu model trên máy

Run bị chặn vì máy thiếu node/model; đưa dependency đã duyệt vào gói cài đặt GPU. Hành trình J09 · giai đoạn 5 · mới. Ảnh chuẩn: [1440](shots/1440-S20-missing-deps.png) · [768](shots/768-S20-missing-deps.png) · [390](shots/390-S20-missing-deps.png).

Phải có:

- [ ] Liệt kê đúng node/model thiếu theo /object_info
- [ ] Máy thuê: nút Thêm vào gói cài đặt GPU (tạo provisioning manifest mới, chờ duyệt) và gợi ý thuê máy mới
- [ ] Máy cố định: hướng dẫn cài tay, không có nút cài tự động
- [ ] Cho biết dung lượng ước tính

Không được có:

- [ ] Tự tải model lên máy đang chạy
- [ ] Submit prompt khi còn thiếu

## S21-monitor · Monitor

Việc cần xử lý trước; Làm mới và Tự cập nhật là hai nút riêng (F10). Hành trình J05, J11 · giai đoạn 1 · sửa. Ảnh chuẩn: [1440](shots/1440-S21-monitor.png) · [768](shots/768-S21-monitor.png) · [390](shots/390-S21-monitor.png).

Phải có:

- [ ] Nút Làm mới bây giờ giữ nguyên chế độ tự cập nhật
- [ ] Công tắc Tự cập nhật Bật/Tắt riêng
- [ ] Thẻ Cần xử lý trước: mỗi thẻ một nút tới trang gốc và nói hậu quả về phí
- [ ] Cảnh báo máy sắp hết hạn idle

Không được có:

- [ ] Gọi provider khi GET

## S01-brief-builder · Tạo Brief

Tạo Brief bằng form, không cần JSON; lưu nháp không tốn phí; tạo task là bước riêng. Hành trình J01 · giai đoạn 6 · mới. Ảnh chuẩn: [1440](shots/1440-S01-brief-builder.png) · [768](shots/768-S01-brief-builder.png) · [390](shots/390-S01-brief-builder.png).

Phải có:

- [ ] Trường: tên video, hook, các beat (thêm/bớt), twist, thời lượng, nhân vật (tùy chọn)
- [ ] Giải thích ngắn dưới mỗi trường
- [ ] Nút Lưu Brief (không tốn phí) tách khỏi nút Tạo task (có thể tốn phí, cần xác nhận)
- [ ] Lỗi hiện ngay cạnh trường

Không được có:

- [ ] Ô JSON là đường chính
