# Màn hình chuẩn UX (IGN-UX)

Đây là bộ UX/UI chính thức dạng hình của Igniterra. Thiết kế và lý do nằm ở [10 · UX cho người mới và ComfyUI](../../src/design/10-ux-beginner.md); thư mục này giữ hình mẫu để người và AI so với sản phẩm thật.

| File | Vai trò |
| --- | --- |
| `screens.json` | Danh sách màn hình: hành trình, giai đoạn, mục đích, điều phải có và không được có |
| `fragments/*.html` | Nội dung từng màn, viết bằng class có sẵn trong `studio/web/static/style.css` |
| `spec.css` | Vài class chưa có trong app; app sẽ phải thêm khi làm màn đó |
| `build.cjs` | Dựng `pages/`, `index.html`, `CHECKLIST.md` và chụp `shots/` |
| `shots/WIDTH-ID.png` | Ảnh chuẩn ở 1440, 768, 390 px |
| `CHECKLIST.md` | Checklist sinh từ `screens.json` |

Mọi file trong `pages/`, `shots/`, `index.html` và `CHECKLIST.md` là đầu ra của `build.cjs`, không sửa tay.

## Dựng lại

```sh
node docs/ux/spec/build.cjs           # dựng và chụp; thoát mã 1 nếu tràn ngang, thiếu nhãn hay trùng id
node docs/ux/spec/build.cjs --serve   # chỉ mở server, in địa chỉ index.html để xem trên trình duyệt
```

Cần Node 22 và Playwright có Chromium. Trang chuẩn dùng CSS thật của app, nên khi `style.css` đổi thì chạy lại để ảnh chuẩn khớp giao diện hiện tại. Đổi nội dung màn hình thì sửa `fragments/` hoặc `screens.json`, không sửa ảnh.

## Review sản phẩm bằng AI

Mỗi PR có thay đổi giao diện làm theo các bước sau:

1. Chụp màn thật bằng fixture giả lập: `tests/ui/fixture_server.py` cùng `tests/ui/verify.cjs`. Không thuê GPU thật, không gọi API có phí.
2. Với mỗi màn PR chạm tới, đưa cho AI ba thứ: ảnh thật, ảnh chuẩn cùng độ rộng, và mục của màn đó trong `CHECKLIST.md`.
3. AI trả lời từng dòng của checklist là đạt, không đạt hoặc không áp dụng, kèm vị trí trên ảnh. Riêng mục "Không được có", nếu thấy thì là lỗi chặn.
4. Khác biệt về bố cục hay chữ không làm sai checklist thì ghi là "lệch chấp nhận được". Đây không phải lỗi. Nếu muốn giữ khác biệt đó, sửa `fragments/` trong cùng PR.
5. Màn có `state` là "mới" mà PR chưa làm thì bỏ qua.

Gợi ý prompt cho AI review:

```text
Bạn review UX của Igniterra. Ảnh A là màn hình thật, ảnh B là màn hình chuẩn cùng độ rộng.
Với từng dòng checklist dưới đây, trả lời: ĐẠT / KHÔNG ĐẠT / KHÔNG ÁP DỤNG, một câu lý do, vị trí trên ảnh A.
Mục "Không được có" mà xuất hiện trên ảnh A là lỗi chặn.
Cuối cùng liệt kê khác biệt giữa A và B không thuộc checklist, chia hai nhóm: ảnh hưởng người mới / chỉ là trình bày.
```
