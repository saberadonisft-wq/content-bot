# Benchmark căn chỉnh phụ đề tiếng Việt

> Bộ golden synthetic dùng Microsoft An TTS với biên lời được dựng có kiểm soát. Bộ này phát hiện regression của VAD/timing; không thay thế đánh giá audio người thật cho ASR.

## Kết quả

| Chỉ số | Start | End |
| --- | ---: | ---: |
| MAE | 21.43 ms | 45.71 ms |
| Median | 20 ms | 40 ms |
| P95 | 40 ms | 70 ms |

Ngưỡng chấp nhận: combined median ≤ 80 ms và combined P95 ≤ 200 ms. Kết quả: median 35.0 ms; P95 70 ms.

## Chi tiết fixture

| Trường hợp | Nhóm | Sai số start | Sai số end | Cần duyệt |
| --- | --- | ---: | ---: | --- |
| fast_speech | nói nhanh | 40 ms | 40 ms | Không |
| quiet_speech | nói nhỏ | 30 ms | 40 ms | Không |
| filler_words | từ đệm | 20 ms | 60 ms | Không |
| background_music | nhạc nền | 20 ms | 20 ms | Không |
| background_noise | tiếng ồn | 20 ms | 50 ms | Không |
| long_pause | khoảng im lặng | 10 ms | 40 ms | Không |
| two_speakers | hai người nói mô phỏng | 10 ms | 70 ms | Không |

Chạy lại:

```powershell
.\backend\.venv\Scripts\python.exe backend\scripts\benchmark_subtitle_alignment.py --check
```
