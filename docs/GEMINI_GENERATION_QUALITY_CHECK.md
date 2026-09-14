# Kiểm tra và tự sửa kết quả Gemini

Prompt tạo, kiểm tra và review dùng tiếng Anh; `text` vẫn là tiếng Việt có dấu.
Phụ đề gốc đọc được luôn có mapping 1:1 với từng cue dịch và giữ mốc hiển thị.

Mỗi chunk chạy theo thứ tự:

1. Tạo bản dịch, kiểm tra schema và giới hạn timestamp của clip đã tải lên.
2. Gửi cùng video vào một lượt đối chiếu riêng. Gemini phải quan sát toàn clip,
   kể cả vùng trống, liệt kê từng đoạn nguồn, thời gian và các cue dịch tương ứng.
3. Code so sánh các quan sát: nội dung nguồn, mapping 1:1, thiếu/thừa/gộp/tách,
   tiếng Việt, thứ tự và sai lệch đầu/cuối. Ngưỡng timing ban đầu là 500 ms;
   hai khoảng không giao nhau vẫn bị loại dù sai lệch nhỏ hơn ngưỡng này.
4. Nếu chưa đạt, hiển thị lỗi và tự yêu cầu sửa ngay chunk đó, gửi kèm bản cũ và
   bằng chứng lỗi. Bản mới phải qua lượt đối chiếu mới. Tối đa hai lượt sửa,
   còn chịu giới hạn retry API và deadline chung của chunk.
5. Chỉ checkpoint kết quả qua đối chiếu. Sau khi hết lượt sửa, job báo lỗi,
   lưu phản hồi bị loại trong `checkpoints/<cache-id>/rejections/`, giữ các chunk
   đã đạt. Chạy tiếp cùng đầu vào sẽ tái sử dụng những chunk đã đạt.

Cache kiểm tra được gắn với hash của đúng phản hồi tạo, phiên bản bộ kiểm tra và
ngưỡng timing. Cache cũ chưa qua đối chiếu không được coi là kết quả hoàn tất.
Pha căn audio tùy chọn giữ mốc hiển thị đã kiểm tra, chỉ bổ sung dữ liệu lời nói.
Lượt dịch, đối chiếu và sửa dùng chung cơ chế key/quota, hủy và giới hạn request;
thống kê token của chunk bao gồm các phản hồi thành công của các pha này.

Đây là đối chiếu bằng Gemini, chưa phải OCR hoặc bộ mốc chuẩn đo độc lập.
Gemini có thể cùng nhận định sai ở hai lượt; trạng thái qua kiểm tra không bảo đảm
độ chính xác 500 ms trên video thật. Mỗi chunk có thêm ít nhất một lời gọi Gemini;
ba bản ứng viên với ba lượt đối chiếu cần sáu lời gọi nếu không có retry mạng.

Theo yêu cầu người dùng, đợt thay đổi này chưa chạy test hoặc gọi Gemini thực tế.
Các ca hồi quy đã được bổ sung/cập nhật để người dùng chạy sau.
