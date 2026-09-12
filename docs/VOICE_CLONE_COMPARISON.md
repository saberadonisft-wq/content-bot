# So sánh giọng nhân bản

Ứng dụng hỗ trợ hai model local: VieNeu v3 Turbo (CPU/ONNX hoặc NVIDIA GPU/PyTorch, 48 kHz) và VieNeu v2 Turbo (NVIDIA GPU/PyTorch, codec ONNX trên CPU, 24 kHz). V2 là lựa chọn để nghe đối chiếu, không bảo đảm giống giọng mẫu hơn V3.

## Chuẩn bị engine

```powershell
# V3 trên CPU
.\scripts\setup-voiceover.ps1
# V3 trên GPU
.\scripts\setup-voiceover.ps1 -Device cuda
# V2 Turbo trên GPU
.\scripts\setup-voiceover.ps1 -Device cuda -Engine v2turbo
```

Model được tải theo revision cố định; tạo giọng bình thường chỉ đọc model đã có trên máy. Mỗi engine có trạng thái sẵn sàng và danh sách giọng riêng. Sau khi cài, bấm kiểm tra lại bộ tạo giọng.

## Cách thử

1. Trong **Tạo hồ sơ giọng từ mẫu**, chọn mốc bắt đầu và độ dài 3–8 giây, rồi chọn file. Có thể đổi mốc và bấm **Lấy lại đoạn mẫu** mà không chọn lại file.
2. Nghe đoạn đã cắt. Audio này chưa qua lọc nhiễu. Chọn một người nói rõ, ít khoảng lặng, nhạc nền và tiếng vang.
3. Với bản ghi đã sạch, bắt đầu bằng **Giữ âm gốc**. Với tiếng nền, thử **Lọc nhiễu**. Lưu hồ sơ.
4. Trong **Nghe thử chất giọng**, dùng cùng một câu để so sánh. Đổi **Xử lý mẫu giọng đang dùng** và tạo mẫu nghe mới để thử hai chế độ. Tùy chọn này lưu theo dự án; hồ sơ trong danh sách vẫn giữ cấu hình khi được tạo.
5. Đổi **Engine tạo giọng** sang V2 Turbo để dùng lại chính audio tham chiếu. Chọn NVIDIA GPU. Giọng có sẵn của hai engine có danh sách khác nhau.

Đổi engine hoặc chế độ lọc nhiễu khiến audio cũ cần tạo lại. Các hồ sơ V3 cũ giữ hành vi lọc nhiễu trước đây và vẫn dùng được audio đã lưu. Các tùy chọn đi theo cả gói Kaggle và mẫu nghe thử trong notebook.

V3 dùng temperature 0.8 và FP32; V2 dùng temperature 0.4 theo SDK, backbone BF16 trên GPU. Đây là cấu hình riêng của từng model, không phải thang đo độ giống. Việc nghe thử trên giọng thực tế vẫn cần thiết.

Nếu backend khởi động lại trong lúc tạo giọng, phần nghe thử hiện **Tiếp tục mẫu nghe**. Nút này tiếp tục tác vụ với nội dung/cấu hình của lần thử đó. Để thử cấu hình vừa chỉnh, dùng **Tạo mẫu nghe**.

## Nguồn và mã xử lý

- [VieNeu v3 Turbo](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Turbo)
- [VieNeu v2 Turbo](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v2-Turbo)
- [VieNeu Codec](https://huggingface.co/pnnbao-ump/VieNeu-Codec)
- Worker: `runtimes/voiceover/worker.py`; crop mẫu: `backend/app/services/voiceover/audio.py`.
