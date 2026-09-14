# Thử bản đồng bộ giọng sau khi hoàn thiện code

Cập nhật 14-09-2026. Theo yêu cầu của người dùng, bàn giao phần code và kiểm tra kỹ thuật; người dùng thực hiện nghe/xem nghiệm thu. Chưa công bố P95 ≤100 ms hoặc tỷ lệ đạt chất lượng trên video thực.

## Cách thử

1. Khởi động lại ứng dụng để nạp backend/frontend mới, mở video và phụ đề tương ứng.
2. Trong **Giọng đọc AI → Tạo giọng theo khoảng thời gian**, chọn khoảng ngắn có vài câu/vế rồi bấm tạo. Audio đang hợp lệ được dùng lại; sau lượt tạo, cùng tác vụ tự kiểm tra nguồn, thử phương án còn cần sửa và áp đoạn đạt. Không cần bấm bổ sung cho từng lượt thử.
3. Với đoạn cũ cần cho phép tự sửa, chọn đoạn và bỏ **Giữ mốc và tốc độ đã chỉnh**; chỉ bỏ **Giữ nguyên lời đọc đã chỉnh** nếu muốn cho phép hệ thống thử tạo lại/rút lời. Không cần mở khóa các đoạn muốn giữ nguyên.
4. Nghe đầu/cuối lời, đối chiếu với video và chữ nguồn. Chữ hiển thị phải giữ nguyên khi chỉ rút lời đọc. Câu tiếp theo phải giữ mốc. Xem kết quả trong **Kiểm tra độ khớp với lời nói nguồn**; có thể nghe WAV đã xử lý ở 1×.
5. Thử **Tạm dừng → Tiếp tục**, **Hoàn tác**, sau đó xuất một đoạn ngắn. Nếu sửa chữ/mốc khi đang căn, lượt cũ phải dừng; giọng hoặc nội dung bạn vừa chỉnh phải được giữ.

Nếu gặp lỗi, ghi tên video, khoảng thời gian, nội dung trước/sau và thông báo đang hiển thị. Các lượt chạy lưu tại `data/voiceover/<owner>/sync-generation/`, `repair-runs/` và `sync-audits/`; WAV cũ và snapshot `projects/snapshots/` được giữ để phục hồi.

## Hành vi đã có trong code

- Tách mốc phụ đề, vùng nói nguồn và biên tiếng trong WAV. Không đổi tốc độ video, không đẩy dây chuyền câu sau. Tempo tự động tối đa 1,15×; không kéo chậm chỉ để lấp ô chữ.
- Sau lượt tạo giọng hoặc lỗi tạo một phần, tác vụ tự tiếp tục kiểm tra/sửa trong ngân sách. Mở dự án hoặc xem timeline không tự chạy model/API.
- Đo nguồn theo vùng với ASR CPU 3 luồng. Giữ mốc chưa xác minh; việc ASR nhận đúng chữ nhưng confidence thấp không tự làm phát sinh lượt TTS.
- Chỉ rút lời khi vùng nguồn hợp lệ và thời lượng thực chưa vừa. Một lượt Gemini đề xuất, một lượt riêng đối chiếu nghĩa; thêm bảo vệ phủ định, điều kiện, số/đơn vị và xưng hô. Kiểm tra tự động không thay thế việc nghe/xem nghiệm thu.
- Mượn tối đa 250 ms khi vùng nguồn, VAD, mẫu PCM yên và kiểm tra ngữ cảnh video cùng cho phép; giữ biên bảo vệ 80 ms. Không tự coi khoảng thiếu phụ đề là khoảng được mượn.
- Có thể thử một WAV chung cho hai vế ngắn liền nhau sau dấu phẩy/chấm phẩy/hai chấm, khi Gemini quan sát vùng video xác nhận cùng lượt nói bị chia vụn. Không suy tên hay quan hệ nhân vật. Tách bằng từ Việt đã nhận dạng, khoảng nghỉ ≥160 ms và vùng cắt yên; đo từng vế lại, căn về từng mốc nguồn, áp cả nhóm hoặc giữ cả nhóm trước. Câu ngắn tự nhiên và đối đáp không tự ghép.
- WAV đã căn được dùng chung cho timeline/xuất ở 1×. Tiếp tục tác vụ không thay WAV đã căn bằng WAV thô trong cache. Có snapshot và giữ WAV cũ; hoàn tác trong phiên chỉnh sửa.
- Hủy/tạm dừng dừng worker cùng tiến trình con. Sửa đầu vào làm kết quả cũ mất hiệu lực. Checkpoint ghi số lượt trước khi gọi API/TTS để không cấp lại lượt qua restart.

## Chi phí và giới hạn đang áp dụng

- Pha kiểm tra nguồn có trần 10 phút; pha sửa có trần 10 phút riêng. Đây là giới hạn tối đa, không phải thời gian dự kiến. Thời gian chờ/tạm dừng vẫn nằm trong hạn đã lưu; hết hạn giữ kết quả đã có.
- Pha sửa tối đa 8 request Gemini **kể cả retry, đối chiếu nghĩa và kiểm tra ngữ cảnh**. Ngữ cảnh chỉ gửi proxy vùng ngắn ≤12 giây, ≤4 MiB; không gửi lại toàn video.
- TTS bổ sung tối đa 2 lượt mỗi cụm, toàn pha tối đa `min(40, max(2, ceil(0.1*N)))`, với N là số cụm thực sự được đưa vào kiểm tra. Nhóm hai vế tính ngân sách cho cả hai thành viên dù dùng một lượt sinh chung.
- TTS dùng cấu hình máy hiện tại: CPU 3 luồng hoặc GPU batch tối đa 4 với 2 luồng CPU hỗ trợ; worker TTS dùng khóa model chung. ASR mới chạy CPU, không chạy aligner GPU cạnh TTS.
- Cache WAV xử lý giới hạn 256 MiB/256 mục. Giới hạn này không bao gồm kho WAV gốc, WAV ứng viên TTS lưu để khôi phục, cache ASR hoặc file xuất.
- Hết ngân sách, thiếu API/model, ngữ cảnh chưa rõ hoặc không thể giữ nghĩa/giọng ở tốc độ cho phép: giữ bản trước và ghi lý do. Không đảm bảo mọi đoạn sẽ tự sửa xong trong một lượt.

## Phần người dùng kiểm chứng

Nghe/xem mẫu đầu–giữa–cuối, câu dài, vế ngắn, nhạc nền và đối đáp. Kiểm tra giữ nghĩa/sắc thái, không mất âm đầu/cuối, không đọc dồn, không ghép sai lượt nói, preview và file xuất nghe giống nhau. Nếu cần con số độ chính xác, đánh dấu biên nói nguồn và biên nói mới để tính sai số đầu/cuối riêng; nhãn `aligned` hiện là đạt kiểm tra tự động, không phải mốc chuẩn do người nghe xác nhận.
