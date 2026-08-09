# Báo cáo nghiệm thu hiệu năng Subtitle Studio

> Ngày đo: 2026-08-09  
> Môi trường: Windows, Chrome headless, Vite production-compatible build, API local  
> Dữ liệu gốc máy đọc được: `data/subtitle-ui-audit/audit.json`
> Phạm vi phát hành: giao diện desktop; nghiệm thu chính tại 1024, 1366 và 1440 px

## 1. Editor và timeline

| Hạng mục | Kết quả | Ngưỡng | Trạng thái |
| --- | ---: | ---: | --- |
| Số cue dự án hiệu năng | 500 | 500 | Đạt |
| DOM cue trên timeline | 101 | Không render toàn bộ 500 | Đạt |
| DOM cue trong sidebar | 4 | Virtualized | Đạt |
| FPS kéo cue | 144,5 FPS | >= 55 FPS | Đạt |
| Khoảng frame lớn nhất khi kéo | 7,5 ms | < 100 ms | Đạt |
| Long task khi kéo | 0 | Không task > 100 ms | Đạt |
| FPS idle sau khi media sẵn sàng | 144,7 FPS | >= 55 FPS | Đạt |
| Snap được quan sát | `Frame · 0 ms` | Có guide + delta ms | Đạt |

Sprite thumbnail được tải từ cache backend, giải mã trước khi gắn vào CSS và chỉ render các lát nằm trong viewport. Bài đo chờ font, video frame và sprite sẵn sàng để tách chi phí tải tài nguyên khỏi phép đo thao tác kéo.

## 2. Workflow trình duyệt

Luồng tự động đã thực hiện trên giao diện thật:

1. Dán fallback timestamp `00:00:00.001 --> 00:00:00.999`.
2. Bấm **Phân tích phụ đề**.
3. Xác nhận editor giữ nguyên hai mốc `00:00:00.001` và `00:00:00.999`.
4. Tách cue: 1 -> 2.
5. Undo: 2 -> 1.
6. Redo: 1 -> 2.
7. Gộp cue: 2 -> 1.

Không có lỗi hiển thị. Cây accessibility không có button, link, textbox hoặc combobox thiếu tên.

## 3. Desktop visual audit

Các viewport desktop bắt buộc đã chụp và kiểm tra:

- 1440 x 900
- 1366 x 768
- 1024 x 768

Ở cả ba kích thước, `scrollWidth == viewport width`, không có lỗi giao diện và video giữ đúng aspect ratio trong stage. Kiểm tra 120 node chữ/control có tương phản tối thiểu 4,5:1 và không có failure. Ảnh kiểm định nằm trong `data/subtitle-ui-audit/`.

Các viewport dưới đây đã được bài audit ghi nhận như fallback sẵn có, nhưng không thuộc phạm vi nghiệm thu hoặc tối ưu tiếp theo:

- 768 x 1024
- 414 x 896
- 390 x 844, gồm cả panel công cụ và workspace
- 375 x 812
- 320 x 800, gồm cả panel công cụ và workspace

## 4. Render thật qua API

Fixture: H.264/AAC, 960 x 540, 29,97 FPS, 12 giây, 4.948.180 byte.

| Encoder | Thời gian FFmpeg | Tốc độ | Output | Audio |
| --- | ---: | ---: | ---: | --- |
| `h264_nvenc` | 1,424 s | 8,424x realtime | 3.969.607 byte | stream-copy |
| `libx264` | 1,980 s | 6,062x realtime | 1.888.527 byte | stream-copy |

- Submit job NVENC: 69 ms.
- Tổng thời gian quan sát gồm polling: 3.287 ms.
- Gửi lại cùng input/config: 20 ms và gắn lại đúng job cũ.
- Smoke test runtime: `h264_nvenc=true`, `h264_qsv=true`, `libx264=true`.
- Output profile Nhanh bằng khoảng 0,80 lần input, dưới ngân sách 2,5 lần.

Không ghi số phần trăm CPU vì runner hiện chưa thu thập processor time của cây FFmpeg trên Windows. Đây là ngoại lệ đo lường, không được diễn giải thành tuyên bố “giảm CPU 50%”. Việc NVENC chạy thật, audio stream-copy và tốc độ 8,424x chứng minh đường tăng tốc đã hoạt động; muốn khóa KPI CPU cần thêm telemetry processor-time ở một benchmark video 1080p dài hơn.

## 5. Bộ kiểm thử cuối

- Backend: Ruff pass; 125 test pass.
- Frontend: ESLint pass; 16 test pass.
- Frontend production build: pass.
- CFR renderer: 23,976 / 24 / 25 / 29,97 / 30 / 50 / 59,94 / 60 FPS pass.
- VFR frame PTS và biên cue `[start_ms, end_ms)` pass.
- Codec/container: H.264 MP4, H.264/PCM MOV, HEVC MKV, VP9/Opus WebM pass.
- Golden alignment: combined median 35 ms, P95 70 ms; đạt mục tiêu 80/200 ms.
