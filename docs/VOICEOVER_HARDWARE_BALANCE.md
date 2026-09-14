# Cân bằng tạo giọng trên i5-13420H / RTX 4050

Ngày đo: 2026-09-13. Máy: Intel Core i5-13420H (8 nhân / 12 luồng), NVIDIA RTX 4050 Laptop 6 GB, Windows. VieNeu SDK 3.6.4, model v3 Turbo revision `8b7e9cffb4b41918cb638b9f62f0a751184d14a6`, giọng preset Ngọc Huyền.

## Cấu hình đã chọn cho máy này

- CPU: ONNX FP32, 3 luồng mỗi pool, 1 luồng inter-op, tắt spinning. Không import PyTorch.
- GPU: 2 luồng CPU hỗ trợ, batch tối đa 4. Mỗi phụ đề vẫn có WAV, hash và checkpoint riêng. Batch lỗi tự chia nhỏ; giới hạn thấp hơn được giữ tới hết tác vụ để tránh thử lại batch quá lớn liên tục.
- Một worker nạp model tại một thời điểm; các tác vụ dự án thật vẫn tạm dừng.
- Giữ model, sample rate, temperature 0.8 và cơ chế phạt lặp mặc định. Không bật INT8 hoặc CUDA Graph để đổi lấy tốc độ.

Cấu hình cục bộ nằm trong `runtimes/voiceover/performance-profile.json` (không commit). Worker chỉ sử dụng khi SDK/model revision trùng. File này chọn thông số cho thiết bị người dùng đã chọn, không tự đổi CPU/GPU của tác vụ hoặc vô hiệu hóa cache cũ. Chọn CPU cho nghe thử/vài đoạn; chọn NVIDIA GPU khi tạo hàng loạt.

## Lượt sàng lọc: cùng 6 câu

Thời gian dưới đây không gồm nạp model và một câu warm-up. Tổng audio khoảng 13 giây; có khác biệt nhỏ về thời lượng do lấy mẫu giữa CPU/GPU/batch.

| Thiết bị | Luồng CPU | Batch | Thời gian sinh và lưu (s) | CPU process time (s) | VRAM đỉnh quan sát (MiB) |
|---|---:|---:|---:|---:|---:|
| CPU | 2 | 1 | 8.49 | 13.19 | 0 |
| CPU | 3 | 1 | 7.00 | 14.42 | 0 |
| CPU | 4 | 1 | 7.18 | 17.22 | 0 |
| GPU | 2 | 1 | 10.40 | 9.55 | 733 |
| GPU | 2 | 2 | 7.84 | 7.30 | 733 |
| GPU | 2 | 4 | 6.24 | 5.69 | 735 |

CPU 3 luồng cân bằng hơn 4 trong mẫu này: thời gian tương đương, ít CPU process time hơn. Cả 6 WAV CPU giống hệt byte khi so cấu hình 2/3/4 luồng với seed cố định. GPU batch 4 giảm cả thời gian hoàn thành và CPU process time so với batch 1, VRAM quan sát tăng rất ít. Các chênh lệch nhỏ giữa CPU 3/4 không phải kết luận thống kê từ nhiều lần lặp.

## Lượt xác nhận: cùng 18 câu

| Thiết bị | Thời gian sinh và lưu (s) | Tổng audio (s) | CPU process time (s) | VRAM đỉnh quan sát (MiB) |
|---|---:|---:|---:|---:|
| CPU 3 luồng | 19.69 | 36.40 | 39.95 | 0 |
| GPU batch 1 / 2 luồng CPU | 28.83 | 36.64 | 27.16 | 759 |
| GPU batch 4 / 2 luồng CPU | 13.35 | 36.88 | 12.02 | 737 |

Batch 4 giảm khoảng 54% thời gian hoàn thành và 56% CPU process time so với batch 1 trong lượt này. VRAM đỉnh quan sát khoảng 0.72 GiB; chênh lệch vài chục MiB không chứng minh batch lớn luôn dùng ít VRAM hơn. Thời gian nạp model riêng: CPU 4.18 s, GPU batch 1 11.61 s, GPU batch 4 9.68 s. Tổng audio khác nhau nhẹ do lấy mẫu; đây là so sánh cùng văn bản, không phải cùng waveform.

Dữ liệu chi tiết: `artifacts/voiceover/balance/confirm-*/result.json`. Mở [bảng nghe đối chiếu](../artifacts/voiceover/balance/comparison.html) trong trình duyệt để nghe CPU, GPU batch 1 và GPU batch 4 theo từng câu. Kết quả tốc độ không tự chứng minh chất lượng giọng tương đương.

## Phương pháp và giới hạn

Script `backend/scripts/benchmark_voice_balance.py` lấy các câu cố định theo phân vị độ dài từ manifest dự án, 12–100 ký tự. Mỗi cấu hình chạy trong một process riêng, lần lượt, cùng model/preset và seed; có một câu warm-up trước phần đo. Batch đầu tiên vẫn có thể có chi phí khởi tạo riêng. Các mẫu, metadata và `result.json` được lưu tại `artifacts/voiceover/balance/`, không ghi vào audio của dự án.

`cpu_seconds` là tổng thời gian CPU của process, không phải phần trăm CPU toàn máy hay điện năng CPU. Telemetry GPU lấy mẫu khoảng mỗi giây qua `nvidia-smi`; VRAM/công suất/nhiệt độ là quan sát ngắn, không phải giới hạn cứng. Không suy ra mức tiết kiệm điện toàn máy từ các mẫu này; không đo CPU package power, RAM peak hoặc độ trễ giao diện. Thời gian nạp model thay đổi theo cache hệ điều hành.

WAV được kiểm tra có tín hiệu hữu hạn, lưu PCM16 mono và checksum. Giữ nguyên thông số không bảo đảm các waveform CPU/GPU/batch giống nhau. Mẫu cần nghe đối chiếu để đánh giá phát âm, thiếu/lặp từ, ngữ điệu và mức khớp thời gian; kiểm tra tín hiệu không thay thế đánh giá này. Chưa xác nhận chất lượng chủ quan hoặc độ ổn định nhiệt trên tác vụ 1.000 đoạn.

## Chạy lại có giới hạn

Ví dụ (từ thư mục repo, thay `MANIFEST` bằng đường dẫn manifest):

```powershell
runtimes/voiceover/.venv/Scripts/python.exe backend/scripts/benchmark_voice_balance.py --manifest MANIFEST --output artifacts/voiceover/balance/recheck-cpu --device cpu --threads 3 --count 18
runtimes/voiceover/.venv-gpu/Scripts/python.exe backend/scripts/benchmark_voice_balance.py --manifest MANIFEST --output artifacts/voiceover/balance/recheck-gpu --device cuda --threads 2 --batch 4 --count 18
```

Không chạy hai lệnh đồng thời. Script sử dụng khóa worker dùng chung để không tranh model với tác vụ local. Các biến `CONTENT_BOT_VOICE_THREADS` và `CONTENT_BOT_VOICE_BATCH_SIZE` cho phép ghi đè cấu hình khi cần so sánh.
