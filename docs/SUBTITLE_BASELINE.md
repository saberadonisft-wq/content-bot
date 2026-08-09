# Subtitle pipeline baseline

> Ngày đo: 2026-08-09  
> Máy: Intel Core i5-13420H (8C/12T), NVIDIA RTX 4050 Laptop, Intel UHD  
> Mục đích: mốc so sánh trước khi triển khai Subtitle V2 và render job

## Trạng thái kiểm thử trước thay đổi

| Hạng mục | Kết quả |
| --- | --- |
| Backend | `69 passed` trong 8,07 giây |
| Frontend build | Thành công, JS 298,25 kB; gzip 86,90 kB |
| Frontend lint | Thành công, không có lỗi |

## Parser

Đầu vào benchmark gồm 500 cue theo định dạng timestamp inline có phần mili-giây, chạy 100 lần.

| Chỉ số | Baseline |
| --- | ---: |
| Mean | 2,099 ms |
| P95 | 2,926 ms |
| Số cue parse được | 500 |

Kết luận: parser hiện tại không phải nút thắt hiệu năng. Công việc V2 tập trung vào tính đúng, validation và round-trip integer milliseconds.

## Render hiện tại

Fixture `data/test_demo.mp4`: 5 giây, 150 frame, 79.327 byte.

| Chỉ số | Baseline |
| --- | ---: |
| Thời gian render | 0,277 giây |
| Kích thước output | 244.783 byte |
| Tỷ lệ output/input | 3,09× |
| Encoder | `libx264`, preset `ultrafast` |
| Audio | Luôn encode AAC, kể cả khi không thay đổi |

Mẫu thực tế 80,78 giây đã có trong workspace tăng từ khoảng 16,9 MB lên 111,6 MB, tương đương khoảng 6,6×. Đây là baseline bắt buộc cho tiêu chí kích thước output.

## Khả năng phần cứng

Capability encode ngắn đã chạy thực tế, không chỉ kiểm tra danh sách encoder:

| Encoder | Exit code | Kết quả |
| --- | ---: | --- |
| `h264_nvenc` | 0 | Dùng được |
| `h264_qsv` | 0 | Dùng được |
| `libx264` | 0 | Dùng được |

Thứ tự mục tiêu: NVENC → QSV → libx264. Runtime vẫn phải tự probe và fallback vì driver hoặc phiên làm việc GPU có thể thay đổi.

## Các lỗi baseline cần khóa bằng test

- Parser chưa hiểu JSON V2 dạng object có `segments` và `start_ms/end_ms`.
- Dòng dạng `[timestamp --> timestamp]` có thể rơi nhầm vào nhánh SRT và phát sinh lỗi parse.
- Nguồn timing là float seconds; thao tác timeline đang làm tròn 100 ms.
- Renderer hiệu ứng dùng ASS centisecond và đang là renderer duy nhất.
- Fade-out audio dùng `areverse`; audio luôn bị tái mã hóa.
- Render chạy đồng bộ trong request; progress parse từ stderr và không có cancel/cache.
- Preview fallback về cue đầu tiên trong khoảng không có cue.
- Click seek tự động phát video.
- Timeline dùng phần trăm toàn video nên cue ngắn bị nén/chồng lấn.
- Thumbnail được seek nhiều lần trong trình duyệt và giữ dưới dạng base64.

## Cổng so sánh sau triển khai

- Parser V2 500 cue P95 dưới 20 ms và round-trip giữ nguyên từng ms.
- Render nhanh ít nhất 2× realtime trên video 1080p thông thường với hardware encoder.
- CPU hardware encode giảm ít nhất 50% so với libx264 baseline trên cùng fixture lớn.
- Output profile Nhanh không lớn hơn 2,5× input, trừ ngoại lệ codec đầu vào được ghi rõ.
- Editor 500 cue đạt ít nhất 55 FPS khi scrub/drag và không có long task trên 100 ms.
- Cancel/fail không để lại process FFmpeg hoặc file `.part`/subtitle tạm.
