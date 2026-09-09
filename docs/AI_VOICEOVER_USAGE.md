# Dùng giọng đọc AI

Tính năng đang được kiểm chứng theo [kế hoạch](AI_VOICEOVER_PLAN.md). CPU và GPU đã tạo được lời đọc thật. Track WAV hai giờ với 160 đoạn đã qua kiểm tra thời lượng và khoảng nghỉ; chưa xem toàn bộ luồng phim dài hoặc Kaggle là đã nghiệm thu. Xem [bằng chứng hiện tại](AI_VOICEOVER_IMPLEMENTATION_STATUS.md).

Đã ghép track này vào MP4 hai giờ có AAC, kiểm tra giải mã ở đầu/giữa/cuối đạt. Video dùng trong phép thử là hình đen tổng hợp; kết quả này không thay thế kiểm tra xuất toàn bộ dự án trong studio hoặc đánh giá chất giọng bằng nghe.

## Chuẩn bị trên Windows

Chạy PowerShell ở thư mục dự án:

```powershell
.\scripts\setup-voiceover.ps1
```

Lệnh tạo môi trường riêng ở `runtimes/voiceover/.venv`, cài SDK rồi tải model khóa phiên bản và tạo mẫu kiểm tra. Lần chuẩn bị đầu cần Internet. Worker sau đó dùng model đã tải trên máy.

Để chuẩn bị NVIDIA GPU:

```powershell
.\scripts\setup-voiceover.ps1 -Device cuda
```

Gói CUDA có dung lượng lớn. Chỉ chọn GPU trong ứng dụng sau khi bước chuẩn bị thành công. Khi báo thiếu môi trường hoặc trạng thái hỏng, chạy lại lệnh chuẩn bị rồi bấm kiểm tra lại trong panel giọng.

CPU dùng `.venv`, GPU dùng `.venv-gpu` trong `runtimes/voiceover`. Hai môi trường tách riêng để cài GPU không thay thư viện của tác vụ CPU đang chạy.

## Tạo lời đọc

1. Mở video và nhập hoặc tạo phụ đề.
2. Mở tab **Giọng đọc AI**, chọn **Tạo đoạn từ phụ đề**.
3. Chọn giọng, tạo mẫu nghe ngắn. Preset Ngọc Huyền ở đây thuộc VieNeu, không phải bản giọng Vbee.
4. Muốn dùng mẫu riêng, chọn file sạch một người nói. Ứng dụng lấy tối đa 8 giây đầu, yêu cầu tối thiểu 3 giây. Nghe mẫu gốc và mẫu đã xử lý trước khi bấm **Lưu và dùng mẫu giọng này**.
5. Sửa **Lời đọc** nếu cần cách phát âm khác chữ phụ đề. Từ điển phát âm áp dụng cho dự án.
6. Tạo phần còn thiếu, một đoạn hoặc các đoạn trong khoảng thời gian. Các câu giao khoảng chọn được tạo nguyên câu.

Theo dõi hàng giọng ngay trên phụ đề. Chọn đoạn để chỉnh tốc độ, độ lệch và âm lượng. Có thể kéo đoạn, dùng phím mũi tên để dịch 10 ms (Shift: 1 giây), hoàn tác hoặc bấm **Về mốc phụ đề**. Đoạn vượt khung cần sửa lời hoặc thời gian; hệ thống không tự cắt mất lời. Chờ trạng thái **Đã lưu lời đọc** trước khi đóng ứng dụng.

Mỗi phụ đề được tạo thành một đoạn giọng riêng để lời đọc bắt đầu đúng mốc của câu đó. Audio có thể kết thúc trước khi phụ đề hết thời gian hiển thị. Với dự án cũ có nhiều câu gộp vào một đoạn, bấm **Tách theo từng phụ đề**, rồi **Tạo phần còn thiếu**. Audio của các đoạn gộp cần tạo lại; các đoạn riêng đã có vẫn được giữ. Có thể **Hoàn tác** thao tác tách. Đoạn gộp có lời đọc đã sửa riêng hoặc liên kết phụ đề không còn hợp lệ được giữ để bạn duyệt lại.

Nếu cùng một mục được sửa ở hai nơi, mở **Xem hai phiên bản** để đối chiếu, rồi chọn **Giữ phần sửa của tôi** hoặc **Dùng phần sửa từ máy chủ**. Lựa chọn chỉ quyết định các mục xung đột; thay đổi độc lập vẫn được giữ. Autosave tiếp tục sau khi chọn. Khi đổi video, ứng dụng lưu lời đọc trước; nếu lưu lỗi, video hiện tại được giữ để bạn xử lý.

## Tạm dừng và tiếp tục

Tạm dừng hoàn thành đoạn đang xử lý rồi ngừng nhận đoạn mới. Hủy có thể bỏ đoạn đang tạo nhưng giữ audio đã lưu. Tiếp tục kiểm tra cache và tạo phần thiếu. Thay text hoặc giọng khiến audio cũ cần tạo lại; thay vị trí hoặc gain dùng lại audio phù hợp.

## Kaggle

Xuất **gói Kaggle**, mở notebook `notebooks/voiceover_kaggle.ipynb` và làm theo các cell. Gói chứa lời đọc, mẫu cần thiết và cấu hình; không chứa video gốc. Sau chạy, tải ZIP kết quả về và nhập vào đúng dự án. Lưu output/checkpoint về máy hoặc thành phiên bản có output trước khi kết thúc phiên Kaggle để tiếp tục được.

Luồng notebook chưa được xác minh trên tài khoản Kaggle thật. Kiểm tra quyền GPU và Internet trong phiên sử dụng.

Notebook dùng môi trường Python riêng cho TTS để tránh xung đột thư viện Kaggle. Sửa `INPUT` thành đường dẫn ZIP đầu vào. Để tiếp tục đúng tác vụ, đặt `PREVIOUS_RESULTS` là ZIP kết quả trước đó của cùng gói; giữ nguyên manifest. `MODEL_CACHE` trỏ đến cache model, `ALLOW_DOWNLOAD=False` chỉ dùng khi đã chuẩn bị đủ model. File `environment.txt` trong thư mục tác vụ ghi phiên bản thư viện đã cài để phục vụ chẩn đoán.

## Xuất

Chọn chỉ giọng AI, trộn âm gốc hoặc giảm âm gốc khi có giọng. Xuất video qua chức năng render của studio. Tải audio riêng bằng WAV, FLAC hoặc MP3; audio riêng chứa giọng AI và áp điểm cắt, tốc độ video hiện tại cùng âm lượng giọng. Cắt xuyên lời đọc sẽ bị chặn để bạn chỉnh lại.

Đoạn chưa tạo giọng nằm hoàn toàn trong phần video đã bỏ không chặn xuất. Nếu còn giao với phần hình giữ lại, cần tạo giọng trước. Lỗi checksum hoặc thời lượng không khớp yêu cầu tải lại hoặc tạo lại đoạn; không dùng file lỗi để xuất.

Giảm âm gốc cũng giảm nhạc/hiệu ứng trong cùng track. Chế độ này giảm âm gốc còn 25% mức đã chọn trong khoảng lời đọc, chuyển xuống trong 20 ms và phục hồi trong 250 ms. Preview và xuất dùng cùng đường chuyển mức theo timeline; khoảng nghỉ bên trong một đoạn lời đọc vẫn giữ mức giảm. Đoạn có gain bằng 0 không làm giảm âm gốc.
