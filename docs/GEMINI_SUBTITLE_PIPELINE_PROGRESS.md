# Tiến độ triển khai Gemini Subtitle Pipeline

Kế hoạch: [GEMINI_SUBTITLE_PIPELINE_PLAN.md](GEMINI_SUBTITLE_PIPELINE_PLAN.md).

## Cập nhật sau bàn giao — dùng toàn bộ key (13/09/2026)

Theo yêu cầu mới, bỏ điều phối theo nhóm project và trần 4 đoạn. Mỗi key đang bật có một suất xử lý, key được chọn luân phiên; cooldown/fallback tách theo key/model. Giao diện bỏ nhóm và số đoạn đồng thời thủ công. Trường cấu hình cũ vẫn được đọc để tương thích nhưng không giới hạn số key. Dữ liệu nhóm cũ không còn được sử dụng và được bỏ khi lưu danh sách lần tiếp theo. Kiểm thử mô phỏng xác nhận cả dispatcher lẫn pipeline có 12 key thực sự giữ 12 lượt xử lý đồng thời; một key bị quota không khóa các key còn lại. Các mô tả nhóm/trần 4 trong lịch sử bên dưới thuộc phiên bản trước thay đổi này.

Ngày chốt triển khai: **13/09/2026**. Theo yêu cầu mới nhất, đã dừng gọi Gemini thật; người dùng sẽ test nhiều key sau. P1–P7 đã triển khai và kiểm thử cục bộ. P8 đã xong phần kiểm thử tự động; nghiệm thu API thật của bản cuối, toàn video và chất lượng còn chờ người dùng chạy. Xem [hướng dẫn nghiệm thu](GEMINI_SUBTITLE_PIPELINE_ACCEPTANCE.md).

## P0 — Baseline và hợp đồng

- Baseline mã nguồn: `4cc8758`; trước triển khai chỉ có tài liệu kế hoạch chưa được git theo dõi.
- 12/09/2026: `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_credential_vault.py backend/tests/test_gemini_subtitles.py backend/tests/test_subtitle_jobs.py -q`: **30 passed, 1.09s**.
- Giữ document schema v2, thêm trường tùy chọn tương thích; manifest/checkpoint/review có version riêng. Revision tài liệu và ID cue được lưu xuyên suốt.
- VAD pin Silero v6.2.1 ONNX CPU; checksum, đóng gói asset và đối chiếu wrapper đã được xác minh.
- Người dùng đã cung cấp video tiếng Trung trong Downloads và xác nhận **chưa có phụ đề chuẩn, cứ tạo kết quả để người dùng duyệt**. Không chặn triển khai để chờ nhãn.
- Media mẫu: 8.957.420 ms (~2h29m17s), 642.580.778 byte, 1280×720, 30 FPS, AV1/AAC. Fingerprint `6fedc66cd078776f99fe5e202762e35d6ac0a969010c20211e14b75f28f3d7c3`.

## Trạng thái

| Giai đoạn | Trạng thái |
| --- | --- |
| P0 | Baseline tự động và baseline ba phút đầu đã đo; chưa có nhãn chuẩn |
| P1 | Đã triển khai vault/API/UI nhiều key; kiểm thử migration/concurrency/che key đạt |
| P2 | Đã tích hợp VAD/manifest/proxy; đối chiếu wrapper và timeline FFmpeg đạt |
| P3 | Đã tích hợp dispatcher và fallback; kiểm thử giới hạn key/nhóm/model, quota, 503 đạt |
| P4 | Đã tích hợp source metadata, ghép bảo thủ, checkpoint/resume/cancel, giữ tối đa 20.000 cue; còn cần nghiệm thu toàn video |
| P5 | Đã sửa căn theo lời gốc/ngôn ngữ, khóa/phạm vi, giới hạn dịch chuyển; chạy mẫu thật và kiểm thử; chưa có bằng chứng độ chính xác cao hơn |
| P6 | Đã triển khai review media, snapshot/proposal/apply/skip/undo, khóa/xung đột; đã thử API thật và kiểm thử trình duyệt |
| P7 | Đã triển khai và kiểm thử tạo lại, lưu/chọn/so sánh phiên bản, tiếp tục checkpoint qua restart, tiến độ đoạn, phạm vi căn, thiết lập nâng cao và hướng dẫn |
| P8 | Chưa nghiệm thu toàn video/bộ đa ngôn ngữ; chưa đo sai số với nhãn chuẩn |

## Bằng chứng chạy thực tế và artifact

- `artifacts/gemini-pipeline/baseline/result.json`, `subtitles.srt`: 36 cue trên ba phút đầu, model `gemini-3.6-flash`, 90,118 giây xử lý generation (không gồm bước tạo clip mẫu).
- `artifacts/gemini-pipeline/new-sample/result.json`, `subtitles.srt`: 59 cue, 2 đoạn, cùng model, 140,561 giây xử lý pipeline, 3 cảnh báo và 4 cue cần duyệt. **Số cue nhiều hơn không phải bằng chứng chất lượng tốt hơn**; số lượt gọi và công việc hai luồng khác nhau.
- `artifacts/gemini-pipeline/preview.html`: trang offline xem clip, lời gốc/bản dịch, chuyển bản baseline/mới và nhảy theo cue. Đã chạy Playwright trên Edge: video đọc được metadata, seek hoạt động, đủ 36/59 hàng, không lỗi JavaScript. Ảnh: `preview.png`.
- VAD toàn video: `manifest.json` có 73 đoạn, đoạn dài nhất 163,68 giây, 4 ranh giới mở rộng cần xem lại. Đã bắt đầu hai lượt chạy toàn video nhưng **chưa hoàn tất 73 đoạn**; chi tiết ở mục P8 bên dưới.
- `vad-reference.json`: 9 đối chiếu xác suất ONNX với wrapper Silero v6.2.1 (audio thật, im lặng, nhiễu; đọc 113 mẫu/lượt, 16.000 mẫu/lượt và cả buffer; phần dư/reset nhiều video). Sai khác lớn nhất 0.0 trong fixture này. Đây là parity wrapper, không phải sai số nhận dạng/timing.
- `new-sample/alignment-result.json`: faster-whisper small có sẵn, CPU int8, chọn 4 cue bị gắn cờ. 3,875 giây, **0 cue được đổi timing**, 4 cảnh báo chưa khớp đủ/bị chia sẻ biên từ. Giữ timing để tránh sửa thiếu bằng chứng.

## Kiểm thử và chính sách hiện tại

- Regression toàn backend: **1.032 passed, 2 skipped, 88,60 giây**; báo cáo `artifacts/gemini-pipeline/backend-tests.xml`. Frontend: **90 passed**; lint/build đạt. Ruff toàn backend và `pip check` đạt. Wheel dựng thành công, có model ONNX/license/reference; NumPy chọn phiên bản theo Python 3.11 hoặc >=3.12. Môi trường kiểm thử thực tế là Python 3.13, chưa chạy runtime Python 3.11 tại máy này.
- Browser dùng React thật và API fixture/store cục bộ: quản lý 35 key giả (nhập, trùng, phân trang, đổi tên/nhóm, bật/tắt/xóa, che secret); review chọn nhiều cue, chặn nhóm rỗng, xem/áp dụng/hoàn tác/xung đột; Studio tạo lại/phiên bản/khóa và giữ chỉnh sửa khi tác vụ hoàn tất. Các script không gọi Gemini.
- Kiểm thử FFmpeg tạo audio bắt đầu trễ, format start offset 5 giây, VFR và seek lẻ 1,733 giây. Sai lệch pulse do phép quy đổi trong fixture nằm trong 30 ms; không suy rộng thành độ chính xác Gemini.
- Test hợp nhất gồm một–nhiều, lời thật lặp lại, OCR chồng audio, giản thể/phồn thể khác nhau và timing mơ hồ; trường hợp không chắc giữ cue và cảnh báo.
- Cache generation và VAD được quét trước một phiên pipeline mới khi không có phiên cùng root đang chạy: mặc định giữ 7 ngày/tối đa 2 GiB, có cấu hình. Cache đang được sử dụng không bị quét. Checkpoint còn hiệu lực cho phép chạy lại chỉ đoạn thiếu/hỏng.
- Model default vẫn 3.6. Chuỗi 3.8→3.7→3.6 chỉ bắt đầu từ model chọn; 503 generation đổi model ngay. File API không đổi model. Project chưa biết được điều phối chung một cách thận trọng; không tự suy ra project thực.

## Phần nghiệm thu còn lại do người dùng thực hiện

- P6 có kiểm thử backend và Playwright. Mẫu API thật `review-sample/result.json` có một đề xuất xóa câu dịch sai vốn đã có bản đúng trùng thời gian; **bỏ sót lỗi thiếu câu đã cài**. Prompt review đã nâng lên phiên bản 2 để đối chiếu hai chiều, nhưng chưa chạy lại API thật. Đây là lỗi cài trên bản Gemini, chưa phải nhãn chuẩn do người đọc xác minh.
- P7 đã có kiểm thử Playwright trên Studio thật (`backend/scripts/check_gemini_studio_ui.py`): tạo lại giữ chỉnh sửa trong lúc chạy, chọn bản mới lưu lại bản đang chỉnh, căn timing không ghi đè chỉnh sửa mới, giữ lời gốc và khóa AI. Ảnh: `studio-versions-ui.png`. Backend có test tạo hai run khác nhau và tiếp tục đúng options/run qua restart manager.
- P8 cần chạy bản cuối trên mẫu ba phút trước, rồi toàn video với nhiều key và báo cáo tài nguyên/chất lượng. Đánh giá dịch/timing vẫn cần người dùng duyệt vì không có bản chuẩn. Chưa có số đo sai số timing trung vị/p95 thực tế; không thay thế bằng số đo FFmpeg hay VAD parity.
- Giới hạn ghép 80% giao thời gian/300 ms chỉ là hàng rào bảo thủ dùng kèm nguồn/chuỗi lời gốc chính xác, chưa phải ngưỡng được tối ưu trên bộ benchmark.
- Chưa commit; tài liệu kế hoạch gốc được giữ nguyên. Phần triển khai bàn giao không đồng nghĩa đã đạt nghiệm thu chất lượng P8.

## P8 — Các phát hiện khi chạy toàn video

- `full/`: model ban đầu 3.6, lưu được 3 checkpoint rồi gặp 503; không còn model thấp hơn trong chuỗi. Phát hiện coordinator vẫn chuẩn bị các đoạn tiếp theo khi không còn key/model khả dụng. Đã bổ sung dừng cấp việc mới trong trường hợp này, giữ các checkpoint hoàn tất.
- `full-v2/`: đã xác minh key thấy 3.8/3.7/3.6 qua Models API; chạy từ 3.8, quan sát chuyển ngay 3.8→3.7→3.6 khi 503. Lưu được 2 checkpoint; nhiều phản hồi HTTP 200 không có danh sách `segments`, sau đó có 429. Dừng lượt cũ để sửa yêu cầu cấu trúc. Tại lúc dừng quan sát đủ **15 upload / 15 DELETE thành công**, chưa có kết quả toàn video. Log/metrics/resources/interrupted.json được giữ để đối chiếu.
- Prompt phiên bản 8 sửa chỉ dẫn cũ mâu thuẫn: audio và chữ khác nội dung phải được giữ riêng; cho phép chồng thời gian có bằng chứng; ví dụ JSON dùng ngoặc đúng. Bản mẫu cũ `new-sample` thuộc prompt trước sửa, không phải nghiệm thu phiên bản cuối.
- Bản cuối dùng `generationConfig.responseSchema` với bộ chuyển JSON Schema sang cấu trúc Schema của Gemini, ghép các text part đầu ra và bỏ thought part; có test hợp đồng riêng. Bản thử `responseJsonSchema` gặp HTTP 400; chưa xác định chắc nguyên nhân. Chưa kết luận các phản hồi sai cấu trúc cũ do thought part vì lượt đó không ghi toàn bộ response.
- Bổ sung ghi response đã che key trong script benchmark, thông tin quota/Retry-After, usage do API cung cấp nếu có, file STOP/SIGINT để hủy mẫu. Không suy ra chi phí từ số cue; usage chỉ bao gồm dữ liệu API thực sự trả về.
- `schema-sample/`: gặp 429 daily quota model 3.6, API báo giới hạn 20 request/ngày/project/model ở lượt này; không coi đó là quota chung cho mọi tài khoản. `schema-sample-38/`: HTTP 400 với cấu trúc trước sửa. `schema-sample-wire/`: bản cuối gặp 503 rồi được hủy theo yêu cầu người dùng, ghi nhận 1 upload/1 DELETE thành công. **Chưa có lượt API thành công với schema đầu ra cuối cùng**; đây là mục đầu tiên cần kiểm tra khi người dùng test key.
- Đã bổ sung chặn model đang cooldown trước mỗi fallback, dừng cấp đoạn mới khi mọi key/model không thể chạy trong deadline, định danh cache bằng JSON ổn định, giới hạn cửa sổ căn và nâng version cache căn để bỏ kết quả cũ. Script benchmark đọc toàn bộ keyring hiện tại.
- Một lệnh gồm bước xóa workspace tạm `full-v2/jobs/run-*` bị cơ chế duyệt tự động chặn. Giữ workspace đó; không thử xóa bằng công cụ khác. Checkpoint và log vẫn còn nguyên.

## Nguồn xác minh

- [Danh mục model Gemini](https://ai.google.dev/gemini-api/docs/models), [Gemini 3.8](https://ai.google.dev/gemini-api/docs/latest-model): xác minh chuỗi ID đầy đủ khi triển khai fallback; quyền thực tế phụ thuộc key/model.
- [Silero releases](https://github.com/snakers4/silero-vad/releases): chọn phiên bản cố định, không tải `master` trong runtime.
