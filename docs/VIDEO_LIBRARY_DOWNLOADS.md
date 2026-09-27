# Tải và quản lý video

Tab **Nguồn & Video → Video** nhận link và lưu video về thư viện cục bộ.

## Sử dụng

- Dán link vào ô **Link video**: mặc định tự bắt đầu tải. Có thể tắt **Tự tải khi dán link** để nhập nhiều link rồi bấm **Tải video**.
- Nhận tối đa 20 link/lượt, kể cả nội dung chia sẻ chứa link và link Bilibili rút gọn `b23.tv`. Mỗi link tải một video; giữ tham số `p` cho phần video Bilibili đã chọn.
- Chọn tối đa 480p, 720p, 1080p hoặc chất lượng cao nhất nguồn/tài khoản cung cấp. Bộ tải ưu tiên H.264/AAC và ghép/remux sang MP4, không nâng độ phân giải.
- Có tiến độ, tốc độ, thời gian còn lại, hủy và thử lại. Tối đa 2 lượt chạy đồng thời, 32 lượt chờ/chạy; mỗi lượt giới hạn 2 giờ.
- Với video cần đăng nhập, mở **Video cần đăng nhập?** rồi chọn file cookies Netscape `.txt` (tối đa 1 MB). Bản tạm được dọn khi worker kết thúc/hủy; không ghi nội dung cookies vào lịch sử/API trả về. Sau khi đóng tab/app, cần chọn lại file nếu muốn thử lại bằng cookies.
- Video hoàn tất hiện trong thư viện với tiêu đề, nền tảng, thời lượng, dung lượng và link gốc. Với luồng acquisition, thư viện còn giữ provenance an toàn gồm `source_id`, `provider_id`, `external_id`, `media_id`, `part_index` và `creator_id`; không lưu cookie/signed URL. Có tìm kiếm, lọc, xem, lưu bản sao ra máy và xóa. Bài đăng đã quét cũng có nút tải về thư viện.
- Đánh dấu ô chọn trên từng video hoặc **Chọn tất cả** rồi bấm **Xóa đã chọn**. Chọn tất cả áp dụng cho toàn bộ kết quả của bộ lọc hiện tại, gồm cả video chưa hiện sau nút **Xem thêm**. Đổi từ khóa/bộ lọc sẽ bỏ lựa chọn cũ. Hộp xác nhận hiển thị số video; nếu có lỗi, những video chưa xóa được vẫn được chọn để thử lại.
- Trong ứng dụng desktop, **Phụ đề Video → Tải lên → Chọn video** mở hộp chọn file ngay tại `<data_dir>/videos/upload`. Video đã có trong thư mục này được dùng lại với ID hiện có, không tải lên bản sao. Vẫn có thể duyệt sang thư mục khác để nhập video. Trên trình duyệt web, hộp chọn file thông thường vẫn được dùng vì trang web không thể đặt đường dẫn thư mục hệ thống.

## Lưu trữ và vận hành

- Trong tab **Cào video**, kênh đã lưu hiển thị trạng thái theo dõi, lỗi, lần quét gần nhất và lịch tiếp theo; tự cập nhật mỗi 15 giây khi tab đang mở. **Xem lượt quét hiện tại/gần nhất** mở đúng run đã lưu. **Xem thêm kênh** hiển thị các kênh sau sáu mục đầu; khi mất kết nối, UI giữ dữ liệu cũ và báo đang thử lại.
- Chế độ **Kênh / danh sách kênh** hoặc playlist nhận tối đa 20 link, mỗi dòng một link. Nhóm dùng chung ngân sách metadata/thời gian và chia phần cho từng kênh; UI hiển thị trạng thái riêng, giữ kết quả khi một kênh lỗi. **Thử lại kênh chưa hoàn tất** giữ kênh đã xong. **Đưa kênh chưa quét vào lượt mới** chỉ điền form, cần bấm **Cào và xem trước** để chạy. Lưu kênh hiện nhận một link mỗi lần.
- Bilibili nhận creator qua CBCE khi profile/browser đã qua gate và nhận playlist công khai dạng `medialist/detail/ml…`, `medialist/play/ml…`, `list/ml…` hoặc `space…/favlist?fid=…`; các alias playlist được chuẩn hóa về cùng channel identity. Creator chưa có live session vẫn giữ `AUTH_REQUIRED`/`waiting_capability`, không fallback sang yt-dlp.
- Với một creator/playlist YouTube, sau khi lượt đầu hoàn tất có thể bấm **Cào thêm video**. Hệ thống giữ candidate cũ, dùng cửa sổ vị trí tiếp theo và dedupe theo ID; request lặp với generation cũ không tạo lượt thứ hai. Cursor dừng khi hết nguồn, đạt ngân sách hoặc gặp hai cửa sổ liên tiếp không có ID mới; danh sách nguồn có thể đổi thứ tự giữa các lượt.
- Lease hoàn tất lượt theo dõi dùng token riêng mỗi lần nhận; chủ lease cũ hoặc hết hạn không được commit trạng thái hoàn tất. Khi nâng cấp từ bản chưa có token, dừng worker cũ trước khi chạy bản mới trên cùng storage. SQLite có test nhiều process thật; Mongo server thật vẫn cần nghiệm thu riêng.

- Dependency: `yt-dlp[default]==2026.8.19`, cài cùng backend bằng `python -m pip install -e .` trong thư mục `backend`.
- FFmpeg dùng bản có sẵn từ `imageio-ffmpeg`. YouTube có thể cần Node.js, cookies hoặc yêu cầu bổ sung từ nền tảng.
- File hoàn tất: `<data_dir>/videos/upload/<id>.mp4` (cùng ID/type `original` mà API video và phụ đề hiện tại sử dụng).
- Lịch sử/metadata: `<data_dir>/videos/downloads/jobs/<id>.json`. API hiển thị các lượt đang chạy và 50 lượt kết thúc gần nhất; metadata của video cũ vẫn giữ.
- File đang tải nằm trong `<data_dir>/videos/downloads/work/`, chỉ chuyển vào thư viện khi worker trả về file hoàn chỉnh và FFmpeg giải mã được ít nhất một video frame trong thời hạn 30 giây. HTML, file rỗng, audio-only hoặc file không có video stream không được publish. Không hiển thị file đang tải dở.
- Cùng URL đã chuẩn hóa và cùng chất lượng được dùng lại nếu đang tải hoặc file hoàn tất vẫn tồn tại. Khác link rút gọn/tracking có thể vẫn tạo bản tải riêng.
- Luồng acquisition truyền `intent_key` ổn định vào job JSON trước khi worker chạy. Nếu process dừng sau khi tạo job nhưng trước khi ghi `job_id` về acquisition store, lượt kế tiếp tra lại theo intent key và dùng đúng job cũ; intent key không được dùng cho một media/quality/profile khác.
- Index job JSON được bảo vệ bằng lock cấp process và ghi atomic; hai process submit cùng intent key chỉ giữ một job. Job cũ không có `schema_version` vẫn đọc được. Publication manifest phục hồi trường hợp file đã chuyển vào thư viện nhưng process dừng trước khi ghi trạng thái `succeeded`.
- TikTok Display trong luồng acquisition hiện chỉ trả metadata cho video detail hoặc creator đã cấp quyền. Candidate được hiển thị là **Chỉ metadata**, không cho chọn tải và backend cũng từ chối tạo downloader job; global search, playlist và media download vẫn chưa được mở.
- Douyin search chỉ chạy qua `cbce_douyin` khi reviewed DOM contract được bật và vẫn ở trạng thái `unverified`; kết quả metadata không có media download contract nên không được chuyển thành job tải. Detail/creator Douyin và toàn bộ XHS vẫn bị khóa.
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
- `POST /api/v1/videos/downloads/{id}/pause`: dừng lượt tải, giữ file dở/checkpoint và không tạo job mới.
- `POST /api/v1/videos/downloads/{id}/resume`: tiếp tục cùng lượt tải; nếu dùng cookies cần gửi lại cookies, profile Bilibili có thể gửi lại `connection_id`.
- `POST /api/v1/videos/downloads/{id}/retry`: `{ "cookie_text": "…" }` (tùy chọn), tiếp tục cùng lượt/file dở; HTTP 202. Lượt trước có dùng cookies cần cung cấp lại cookies.
- `GET /api/v1/videos`: thêm `title`, `source_url`, `source_id`, `provider_id`, `external_id`, `media_id`, `part_index`, `creator_id`, `platform`, `duration`, `downloaded`; giữ các trường và loại video cũ.

## Kiểm chứng

- Nhóm acquisition/download/process/batch/storage: `149 passed`. Test process nằm ở `backend/tests/test_acquisition_storage_process.py`, dùng SQLite tạm và hai interpreter độc lập, gồm tình huống process mất lease vẫn cố commit. `test_acquisition_batch.py` kiểm tra mixed provider, partial/retry, giới hạn chung, cào tiếp theo generation/cursor lặp, pause/cancel, API child control và phục hồi parent sau child publication.
- Canary metadata nhóm thật: từ `backend`, chạy `.venv/Scripts/python.exe scripts/acquisition_batch_canary.py --target https://www.youtube.com/@GoogleDevelopers/videos --target https://www.youtube.com/@YouTube/videos --max-candidates 40 --deadline-seconds 120`. Lượt ngày 2026-09-23 đạt 20 metadata/kênh, tổng 40 trong 6,68 giây; dữ liệu nằm trong SQLite tạm và không cấp lượt tải. Một lần chạy không thay thế live gate đầy đủ.
- Canary cào tiếp nhóm thật: từ `backend`, chạy `.venv/Scripts/python.exe scripts/acquisition_batch_canary.py --target https://www.youtube.com/@GoogleDevelopers/videos --max-candidates 20 --deadline-seconds 120 --continue-once`. Lượt ngày 2026-09-23 đạt 20 candidate ban đầu → 40 sau một lần cào tiếp, dừng với `candidate_budget` trong 6,29 giây, không cấp lượt tải.
- Canary Bilibili có connection profile dùng `--connection-id <connection-ref>` (chỉ target Bilibili, không in ref/cookie, không cấp download); phải chạy lại sau khi đăng nhập từng profile để đạt live gate creator/connection. Public playlist rerun ngày 2026-09-23 đạt 2 candidate metadata trong 2,62 giây với `connection_profile_used=false`.
- Canary playlist Bilibili: thêm `--mode playlist --target https://www.bilibili.com/medialist/detail/ml213003412` đạt 7 candidate có title/canonical URL; playlist `ml1103317812` chạy continuation `10 → 20` candidate với `candidate_budget`, không cấp lượt tải.
- Chạy Vite trên cổng 5187, sau đó `backend/.venv/Scripts/python.exe backend/scripts/acquisition_subscription_ui_smoke.py --base-url http://127.0.0.1:5187`. Smoke dùng API fixture, kiểm tra trạng thái kênh, lịch sử ngoài danh sách gần đây, polling mất/kết nối lại, timezone bộ lọc, selection giữ qua hai trang candidate và layout desktop/mobile; ảnh ở `artifacts/acquisition-ui/`.

- `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_video_downloads.py backend/tests/test_video_thumbnails.py backend/tests/test_application_lifecycle.py backend/tests/test_api.py -q`: 31 test qua.
- Trong `frontend`: `npm run build`, `npm test -- src/video-library/model.test.ts`, ESLint các file thay đổi.
- Khi Vite đang chạy cổng 5173: `backend/.venv/Scripts/python.exe backend/scripts/check_video_library_ui.py`. Script dùng API giả lập, không sửa dữ liệu người dùng; kiểm tra dán link, nhập nhiều link, tiến độ, hủy/thử lại, tìm/lọc, tự cập nhật, xóa và các độ rộng 320/375/414/768 px. Ảnh kiểm tra ở `artifacts/video-library/`.
- Tải thật thành công link MP4 công khai `https://www.w3schools.com/html/mov_bbb.mp4` (788.493 byte), FFmpeg đọc/giải mã được file kết quả.
- Thử Bilibili `https://www.bilibili.com/video/BV1bK411W797?p=1`: nền tảng từ chối truy cập trong môi trường kiểm tra; đã xác minh trạng thái lỗi, chưa xác minh tải Bilibili thành công bằng cookies tài khoản. Không khẳng định mọi link/nền tảng đều tải được.
- Link người dùng `BV12eYW6AENR`: xác minh trả HTTP 412 ngay khi lấy trang, kể cả header trình duyệt thông thường; chưa có phiên đăng nhập/xác minh để kiểm tra tiếp.
- `test_video_download_resume.py` dùng yt-dlp thật và HTTP Range server cục bộ: file dở 4.096 byte được nối bằng `Range: bytes=4096-`, file cuối khớp từng byte với nguồn. Các test manager kiểm tra khởi động lại, ID cũ, file dở, chờ cookies và không tự chạy lại lượt đã hủy.
