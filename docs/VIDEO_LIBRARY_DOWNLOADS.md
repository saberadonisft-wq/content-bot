# Tải và quản lý video

Tab **Nguồn & Video → Video** nhận link và lưu video về thư viện cục bộ.

## Sử dụng

- Dán link vào ô **Link video**: mặc định tự bắt đầu tải. Có thể tắt **Tự tải khi dán link** để nhập nhiều link rồi bấm **Tải video**.
- Nhận tối đa 20 link/lượt, kể cả nội dung chia sẻ chứa link và link Bilibili rút gọn `b23.tv`. Mỗi link tải một video; giữ tham số `p` cho phần video Bilibili đã chọn.
- Chọn tối đa 480p, 720p, 1080p hoặc chất lượng cao nhất nguồn/tài khoản cung cấp. Bộ tải ưu tiên H.264/AAC và ghép/remux sang MP4, không nâng độ phân giải.
- Có tiến độ, tốc độ, thời gian còn lại, hủy và thử lại. Tối đa 2 lượt chạy đồng thời, 32 lượt chờ/chạy; mỗi lượt giới hạn 2 giờ.
- Với video cần đăng nhập, mở **Video cần đăng nhập?** rồi chọn file cookies Netscape `.txt` (tối đa 1 MB). Bản tạm được dọn khi worker kết thúc/hủy; không ghi nội dung cookies vào lịch sử/API trả về. Sau khi đóng tab/app, cần chọn lại file nếu muốn thử lại bằng cookies.
- Video hoàn tất hiện trong thư viện với tiêu đề, nền tảng, thời lượng, dung lượng và link gốc. Có tìm kiếm, lọc, xem, lưu bản sao ra máy và xóa. Bài đăng đã quét cũng có nút tải về thư viện.
- Đánh dấu ô chọn trên từng video hoặc **Chọn tất cả** rồi bấm **Xóa đã chọn**. Chọn tất cả áp dụng cho toàn bộ kết quả của bộ lọc hiện tại, gồm cả video chưa hiện sau nút **Xem thêm**. Đổi từ khóa/bộ lọc sẽ bỏ lựa chọn cũ. Hộp xác nhận hiển thị số video; nếu có lỗi, những video chưa xóa được vẫn được chọn để thử lại.
- Trong ứng dụng desktop, **Phụ đề Video → Tải lên → Chọn video** mở hộp chọn file ngay tại `<data_dir>/videos/upload`. Video đã có trong thư mục này được dùng lại với ID hiện có, không tải lên bản sao. Vẫn có thể duyệt sang thư mục khác để nhập video. Trên trình duyệt web, hộp chọn file thông thường vẫn được dùng vì trang web không thể đặt đường dẫn thư mục hệ thống.

## Lưu trữ và vận hành

- Dependency: `yt-dlp[default]==2026.8.19`, cài cùng backend bằng `python -m pip install -e .` trong thư mục `backend`.
- FFmpeg dùng bản có sẵn từ `imageio-ffmpeg`. YouTube có thể cần Node.js, cookies hoặc yêu cầu bổ sung từ nền tảng.
- File hoàn tất: `<data_dir>/videos/upload/<id>.mp4` (cùng ID/type `original` mà API video và phụ đề hiện tại sử dụng).
- Lịch sử/metadata: `<data_dir>/videos/downloads/jobs/<id>.json`. API hiển thị các lượt đang chạy và 50 lượt kết thúc gần nhất; metadata của video cũ vẫn giữ.
- File đang tải nằm trong `<data_dir>/videos/downloads/work/`, chỉ chuyển vào thư viện khi worker trả về file hoàn chỉnh. Không hiển thị file đang tải dở.
- Cùng URL đã chuẩn hóa và cùng chất lượng được dùng lại nếu đang tải hoặc file hoàn tất vẫn tồn tại. Khác link rút gọn/tracking có thể vẫn tạo bản tải riêng.
- File tải dở/checkpoint được giữ ở `downloads/work/<job_id>/`. Khi backend khởi động lại, lượt đang tải/chờ tự xếp hàng chạy lại với cùng ID; yt-dlp lấy mới URL media và tiếp tục byte/fragment nếu nguồn hỗ trợ. Nếu nguồn không hỗ trợ Range hoặc định dạng thay đổi, phần đó có thể phải tải lại. Chuyển tab không ảnh hưởng worker.
- **Thử lại/Tiếp tục** dùng lại lượt cũ và thư mục tải dở; không thêm dòng lịch sử mới. Lượt đã hủy không tự chạy lại, chỉ tiếp tục khi người dùng yêu cầu. File dở được giữ khi lỗi/hủy và dọn sau khi tải thành công. File dở từng bị phiên bản cũ xóa không thể khôi phục.
- Khi khởi động lại, lượt có cookies chuyển sang chờ chọn lại cookies, không tự lưu cookies lâu dài. Lịch sử chỉ lưu cờ `uses_cookies`, không lưu nội dung cookies.
- Bilibili HTTP 412 là nền tảng chặn yêu cầu tự động, không đồng nghĩa chắc chắn thiếu đăng nhập. Lượt lỗi có **Mở video gốc** và **Chọn cookies** để hoàn tất xác minh trong trình duyệt nếu được yêu cầu. Cookies không đảm bảo giải quyết giới hạn mạng/IP. HTTP 403/429 có hướng dẫn riêng.
- API tuân theo middleware xác thực sẵn có. URL đầu vào giới hạn HTTP/HTTPS công khai; worker kiểm tra địa chỉ DNS để chặn mạng nội bộ và chạy độc lập với mạng của ứng dụng chính.

## API

- `GET /api/v1/videos/directory`: thư mục video cục bộ theo cấu hình backend, dùng làm `defaultPath` cho hộp chọn file desktop.
- `GET /api/v1/videos/downloads`: trạng thái/lịch sử.
- `POST /api/v1/videos/downloads`: `{ "urls": ["https://…"], "quality": "1080", "cookie_text": "…" }`; trả HTTP 202 và danh sách lượt tải. `cookie_text` tùy chọn.
- `POST /api/v1/videos/downloads/{id}/cancel`: hủy lượt tải và tiến trình con.
- `POST /api/v1/videos/downloads/{id}/retry`: `{ "cookie_text": "…" }` (tùy chọn), tiếp tục cùng lượt/file dở; HTTP 202. Lượt trước có dùng cookies cần cung cấp lại cookies.
- `GET /api/v1/videos`: thêm `title`, `source_url`, `platform`, `duration`, `downloaded`; giữ các trường và loại video cũ.

## Kiểm chứng

- `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_video_downloads.py backend/tests/test_video_thumbnails.py backend/tests/test_application_lifecycle.py backend/tests/test_api.py -q`: 31 test qua.
- Trong `frontend`: `npm run build`, `npm test -- src/video-library/model.test.ts`, ESLint các file thay đổi.
- Khi Vite đang chạy cổng 5173: `backend/.venv/Scripts/python.exe backend/scripts/check_video_library_ui.py`. Script dùng API giả lập, không sửa dữ liệu người dùng; kiểm tra dán link, nhập nhiều link, tiến độ, hủy/thử lại, tìm/lọc, tự cập nhật, xóa và các độ rộng 320/375/414/768 px. Ảnh kiểm tra ở `artifacts/video-library/`.
- Tải thật thành công link MP4 công khai `https://www.w3schools.com/html/mov_bbb.mp4` (788.493 byte), FFmpeg đọc/giải mã được file kết quả.
- Thử Bilibili `https://www.bilibili.com/video/BV1bK411W797?p=1`: nền tảng từ chối truy cập trong môi trường kiểm tra; đã xác minh trạng thái lỗi, chưa xác minh tải Bilibili thành công bằng cookies tài khoản. Không khẳng định mọi link/nền tảng đều tải được.
- Link người dùng `BV12eYW6AENR`: xác minh trả HTTP 412 ngay khi lấy trang, kể cả header trình duyệt thông thường; chưa có phiên đăng nhập/xác minh để kiểm tra tiếp.
- `test_video_download_resume.py` dùng yt-dlp thật và HTTP Range server cục bộ: file dở 4.096 byte được nối bằng `Range: bytes=4096-`, file cuối khớp từng byte với nguồn. Các test manager kiểm tra khởi động lại, ID cũ, file dở, chờ cookies và không tự chạy lại lượt đã hủy.
