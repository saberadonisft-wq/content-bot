# Bàn giao Gemini Subtitle Pipeline

Chốt ngày 13/09/2026 theo yêu cầu: hoàn thành các hạng mục triển khai và kiểm thử cục bộ; người dùng test nhiều key sau. Không có tác vụ benchmark Gemini nào tiếp tục chạy nền. Kế hoạch gốc: [GEMINI_SUBTITLE_PIPELINE_PLAN.md](GEMINI_SUBTITLE_PIPELINE_PLAN.md); kết quả và lịch sử chạy: [GEMINI_SUBTITLE_PIPELINE_PROGRESS.md](GEMINI_SUBTITLE_PIPELINE_PROGRESS.md).

## Phần đã hoàn thành

| Giai đoạn | Kết quả bàn giao |
| --- | --- |
| P0 | Baseline ba phút, hợp đồng document/revision và artifact để đối chiếu. |
| P1 | Vault nhiều key, migration, nhập hàng loạt/loại trùng, bật/tắt/xóa, đặt tên, kiểm tra model, phân trang và che secret. Không cần nhóm project. |
| P2 | Silero v6.2.1 ONNX cố định, đọc audio theo luồng, manifest theo khoảng nghỉ, proxy và quy đổi timeline nguồn. |
| P3 | Tự động một đoạn đồng thời cho mỗi key đang bật, luân phiên key, chờ quota riêng theo key/model, fallback, deadline và cleanup upload đúng chủ sở hữu. |
| P4 | Metadata lời gốc/ngôn ngữ/nguồn, ID ổn định, ghép vùng nối bảo thủ, checkpoint, hủy/tiếp tục và giới hạn 20.000 cue. |
| P5 | Căn theo lời gốc, phạm vi/cue khóa, giới hạn cửa sổ và độ dịch chuyển; giữ nguyên khi bằng chứng không đủ. |
| P6 | Review media khi người dùng bấm; chọn toàn video/khoảng/nhóm cue, đề xuất trước/sau, áp dụng/bỏ qua/hoàn tác, bảo vệ revision và bản chỉnh tay. |
| P7 | Tạo lại thành phiên bản mới, lưu/chọn/so sánh phiên bản, phục hồi job qua restart, tiến độ đoạn, tùy chọn nâng cao và hướng dẫn. |
| P8 | Kiểm thử tự động và công cụ thu thập kết quả; nghiệm thu API thật của bản cuối, nhiều key và chất lượng toàn video còn chờ. |

Kiểm thử: toàn backend **1.032 passed, 2 skipped**; frontend **90 passed**; Ruff, ESLint, TypeScript/Vite build và `pip check` đạt. Playwright kiểm tra quản lý key, review và Studio với dữ liệu/API giả cùng store thật; không chứng minh chất lượng Gemini. Báo cáo backend ở `artifacts/gemini-pipeline/backend-tests.xml`, ảnh giao diện ở cùng thư mục. Các kiểm thử căn liên quan được chạy lại sau khi nâng version cache.

ONNX và wrapper tham chiếu đã đối chiếu 9 fixture, sai khác xác suất lớn nhất 0.0; fixture FFmpeg kiểm tra start offset, audio trễ và VFR. Đây là kiểm tra triển khai, không phải bằng chứng sai số timing trên video thực. Wheel đóng gói model/license/reference; môi trường chạy kiểm thử là Python 3.13. Nhánh dependency Python 3.11 chưa được chạy tại máy này.

## Kiểm tra qua giao diện khi có key

Prompt **tạo phụ đề mới** phiên bản 11 cho phép mỗi cue là một câu hoặc một vế câu, không cần hoàn chỉnh; được tách ở dấu phẩy khi bám sát nội dung, ngữ cảnh, thứ tự và thời gian phụ đề gốc. Không ghép cue gốc để chờ đủ câu, không tách máy móc ở mọi dấu phẩy. Prompt yêu cầu tách cả khối phụ đề gốc chứa nhiều câu, tối đa 84 ký tự/2 dòng/6 giây theo ranh giới thực tế. Nếu video có phụ đề gốc đọc được, bắt buộc dịch theo nội dung, ngữ cảnh và mốc xuất hiện/biến mất của phụ đề đó; audio dùng đối chiếu, không tạo bản dịch thứ hai cho cùng lượt thoại. Sai khác lời/mốc hoặc ranh giới chưa rõ phải đánh dấu cần kiểm tra, không chia đều thời gian hay bịa mốc. Prompt yêu cầu tự rà từng câu, mọi khoảng chồng/trống và đối chiếu đủ lời trước khi trả JSON. Ngữ cảnh lấy từ phụ đề trước/sau và audio, không yêu cầu người dùng điền tên/quan hệ và không có bước lập hồ sơ nhân vật riêng. Ghi chú dịch là tùy chọn. Prompt sao chép trên giao diện đã đồng bộ. Phiên bản prompt tham gia khóa cache kết quả và checkpoint từng đoạn; test giả lập xác nhận đổi phiên bản sẽ gọi tạo lại các đoạn. Chưa đo chất lượng prompt tạo mới phiên bản 11 trên Gemini thật.

**Kiểm tra sửa chữa bằng AI** có một nút **Sửa bằng AI**. Chọn **Toàn video**, **Khoảng thời gian** hoặc **Chọn nhóm cue** rồi bấm một lần: backend tự quét cue dài, dấu hiệu timing và yêu cầu Gemini kiểm tra cả nội dung, độ dài, thời gian trong từng clip. Không cần chuyển tab hay bấm quét riêng. Toàn bộ phạm vi được xem xét, kể cả vùng chưa bị bộ quét đánh dấu. Các key đang bật xử lý clip song song; có checkpoint để tiếp tục khi bị gián đoạn.

Ngưỡng gợi ý: cue chứa từ hai câu (kể cả chưa dài), trên 84 ký tự, 6 giây hoặc 2 dòng; mọi khoảng chồng thời gian lớn hơn 0 ms, ngắn hơn 700 ms hoặc trên 25 ký tự/giây (ít nhất 12 ký tự), từ 3 cue bắt đầu trong 1,5 giây và mọi khoảng trống lớn hơn 0 ms. Bộ đếm câu dựa trên dấu kết câu, có ngoại lệ số thập phân, viết tắt phổ biến và dấu ba chấm. Quét timeline theo hợp các khoảng có phụ đề để không tạo khoảng trống giả khi cue lồng nhau. Gửi Gemini số câu và mốc chồng/trống chính xác, gồm đầu/cuối video, toàn bộ khoảng trống dài và clip chưa có cue. Mỗi clip có thêm 10 giây ngữ cảnh trước/sau, nhưng đề xuất chỉ sửa trong phạm vi chọn. Khoảng trống/chồng cue có thể hợp lệ, cần đối chiếu media; chỉ thêm lời khi nghe/đọc được bằng chứng.

Kết quả là một danh sách đề xuất chung để duyệt. Cue vừa dài vừa sai timing được tách và đặt lại mốc trong cùng đề xuất; mỗi cue chỉ thuộc một đề xuất trong clip. Sửa thuần timing giữ nguyên lời; tách readability bảo toàn lời dịch/lời gốc và giới hạn cue con tối đa một câu, 84 ký tự, 6 giây, 2 dòng. Cue khóa giữ nguyên. Kiểm thử đã phủ sửa nội dung + tách câu + đổi timing trong cùng một lượt, toàn video/khoảng chọn, timestamp tương đối, giới hạn phạm vi, trùng đề xuất, áp dụng nhiều nhóm/hoàn tác, xung đột và resume. Prompt kết hợp phiên bản 5; review cũ vẫn có thể xem/áp dụng theo snapshot cũ, cần tạo lượt mới để dùng quy tắc mới. Bộ quét mới đã qua 67 test backend liên quan và 12 test frontend, gồm cue nhiều câu nhưng ngắn, chồng 1 ms, khoảng trống ngắn/đầu/cuối, cue lồng nhau và thêm lời ở giữa clip trống qua Gemini giả lập. Build frontend đạt; chưa chạy Gemini thật với prompt phiên bản 5.

Bản sửa lỗi review: đề xuất vượt phạm vi, sai ID hoặc không hợp lệ được loại riêng kèm cảnh báo; giữ các đề xuất độc lập hợp lệ, không gửi lại cả clip vì một đề xuất lỗi. Nhóm có cue trái phép bị loại nguyên nhóm. Schema giới hạn cue_ids theo targets của clip. Tiến trình báo số vùng đã xong/đang chạy/lỗi, thời gian đã chờ và bước tải video/phân tích/thử lại. Lỗi vùng được báo ngay, kết quả vùng khác được lưu và có thể xem/duyệt sau khi lượt thất bại. Tiếp tục chỉ chạy vùng chưa hoàn tất. Ba vùng lỗi liên tiếp dừng cấp thêm vùng mới; chờ tối đa 120 giây mỗi phản hồi generation trong ngân sách 300 giây/clip. Kiểm thử phủ kết quả lẫn đúng/sai, không gọi lại vì đề xuất sai, giữ nhóm nguyên vẹn, lỗi sớm và resume, cùng giao diện duyệt kết quả khi job thất bại.

Đã chạy thử API thật ngày 13/09/2026 với model yêu cầu gemini-3.8-flash, phạm vi 32:30–32:50 và ngữ cảnh trước/sau: hoàn tất trong 69,8 giây, giữ 1 đề xuất hợp lệ, loại riêng 4 đề xuất sai quan hệ số cue trước/sau. Không gọi lại chỉ vì các đề xuất lỗi, không áp dụng vào document. Kết quả lưu tại `artifacts/gemini-pipeline/review-fix-live-result.json`; đây là nghiệm thu luồng xử lý, chưa xác nhận độ đúng nội dung/timing. Kiểm thử hồi quy: 83 test backend; giao diện đã phủ xem/áp dụng/hoàn tác phần kết quả khi job thất bại, lint và build đạt.

1. Khởi động backend/frontend bằng luồng thường dùng. Vào **Settings → Gemini AI**, mở khóa Vault, nhập các key mỗi dòng một key. Tất cả key đang bật được dùng: 12 key cho phép 12 đoạn đồng thời, mỗi key một đoạn. Không khai báo nhóm; trần cũ 4 đoạn không còn áp dụng. Key nhận 429 sẽ chờ riêng theo phản hồi, các key khác vẫn được nhận việc. Số worker được lấy ở đầu lượt chạy; nén proxy vẫn tối đa hai tiến trình.
2. Kiểm tra model cho từng key. Kiểm tra này chỉ đọc thông tin model; vẫn cần một lần generation để xác minh quyền/quota và cấu trúc đầu ra. Model mặc định giữ 3.6; chọn 3.8 nếu muốn kiểm tra chuỗi fallback 3.8→3.7→3.6.
3. Chạy mẫu ba phút trước. **Bản cuối dùng `responseSchema` chưa có lượt API thật thành công**: lượt gần nhất gặp 503 rồi đã hủy. Xác nhận tạo được `segments`, có lời gốc/ngôn ngữ, thời gian nằm trong media và SRT xem được, trước khi chạy toàn video.
4. Chạy video dài trong Subtitle Studio, quan sát tiến độ đoạn/model thực tế. Khi đoạn lỗi tạm thời (503/5xx/mất kết nối), backend tự bổ sung đoạn thiếu trong cùng job, tối đa 3 lượt phục hồi, chờ mặc định 5/10/20 giây và hiển thị đếm ngược. Đoạn đã xong không được gửi lại. Hết chuỗi model vì 503 phải cho phép thử lại sau chờ; model không hỗ trợ, key/quyền lỗi và quota ngày không được mở khóa. Hủy được cả trong lúc chờ phục hồi và không tự chạy lại sau hủy. Khởi động lại backend hoặc đóng/mở ứng dụng: tác vụ tạo phụ đề đang chạy/đang chờ phải tự tiếp tục khi API sẵn sàng, giữ nguyên ID, model, options và run_id đã lưu, không cần mở Studio hay bấm Tiếp tục. Chỉ khôi phục lần thử mới nhất của cùng đầu vào; tác vụ đã hủy hoặc lỗi xử lý thông thường không được tự chạy lại. Các checkpoint hợp lệ phải được dùng lại, chỉ xử lý đoạn còn thiếu. Khi video, cấu hình/schema khác bản đã lưu, hoặc thiếu hồ sơ tác vụ cũ, báo lỗi và không gọi Gemini. Kiểm thử `test_gemini_generation_recovery.py` phủ shutdown, crash/legacy interrupted, hàng đợi giới hạn, hủy, startup thật và chỉ gọi lại đoạn chưa xong với pipeline thật/Gemini giả lập. `check_gemini_studio_ui.py` kiểm tra mất kết nối, phục hồi và tải lại trang mà không gửi yêu cầu resume.
5. Chỉnh một câu rồi **Tạo lại**. Tiếp tục chỉnh khi đang chạy: kết quả mới phải nằm ở danh sách phiên bản, không ghi đè bản đang chỉnh. So sánh và chọn bản mới; bản trước vẫn có thể mở lại.
6. Khóa một cue; chọn nhóm cue/khoảng thời gian để **Căn lại thời gian**. Cue ngoài phạm vi hoặc bị khóa giữ nguyên. Trường hợp căn không đủ bằng chứng có cảnh báo và có thể không đổi timing.
7. Cài một lỗi dịch, một câu thiếu và một câu trùng trong bản thử. Chọn nhóm cue hoặc vùng có lỗi rồi bấm **Sửa bằng AI**. Xem video/bằng chứng/trước-sau rồi áp dụng từng đề xuất; thử bỏ qua, áp dụng tất cả và hoàn tác. Chỉnh cue sau khi review xong rồi áp dụng: hệ thống phải báo xung đột và giữ chỉnh sửa mới.
8. Xuất SRT và document JSON để duyệt. Giữ JSON vì SRT không chứa đầy đủ lời gốc, provenance, khóa và revision.

## Lệnh tạo artifact riêng

Ưu tiên giao diện nếu key được mở khóa trong tiến trình backend. Script benchmark là tiến trình riêng và **không thừa hưởng trạng thái mở khóa Vault trong bộ nhớ backend**; trên Windows, Vault có thể tự mở bằng khóa thiết bị đã lưu cho cùng tài khoản OS. Nếu cơ chế đó không khả dụng, hãy dùng giao diện đã mở khóa. Không chép key vào lệnh/log hay tài liệu. Danh sách Gemini đã lưu là nguồn có thẩm quyền: khi đang khóa, rỗng hoặc toàn bộ bị tắt thì không tự quay về key `.env`.

Từ PowerShell tại `D:\content-bot`, khi credential của tiến trình đã sẵn sàng:

```powershell
$sampleVideo = Get-ChildItem -LiteralPath 'C:\Users\vhc\Downloads' -Filter '新番速递夫人十年不孕改嫁后一胎三宝*.mp4' | Select-Object -First 1
if (-not $sampleVideo) { throw 'Không tìm thấy video mẫu' }
.\backend\.venv\Scripts\python.exe backend/scripts/run_gemini_pipeline_sample.py $sampleVideo.FullName --seconds 180 --model gemini-3.8-flash --output artifacts/gemini-pipeline/user-sample
```

Sau khi duyệt mẫu đạt, đổi `--seconds 180` thành `--seconds 0` và dùng thư mục output mới `artifacts/gemini-pipeline/user-full`. Không chạy các script benchmark song song với nhau hoặc với Studio trên cùng bộ key vì mỗi tiến trình có dispatcher riêng.

Script ghi `metrics.json`, `progress.json`, response đã che key, checkpoint và khi thành công có `result.json`/`subtitles.srt`. Usage chỉ tổng hợp dữ liệu API trả về, không phải hóa đơn và có thể không gồm lượt lỗi. Để hủy, nhấn Ctrl+C hoặc tạo file `STOP` trong đúng thư mục output. Trước khi tiếp tục bằng cùng output, bỏ file `STOP` do bạn tạo; giữ nguyên video/options để dùng checkpoint.

Đo tài nguyên từ một PowerShell khác, sau khi `metrics.json` đã xuất hiện:

```powershell
$sampleMetrics = Get-Content -Raw artifacts/gemini-pipeline/user-full/metrics.json | ConvertFrom-Json
.\backend\scripts\monitor_gemini_resources.ps1 -TargetProcessId $sampleMetrics.pid -OutputDirectory artifacts/gemini-pipeline/user-full
```

Monitor ghi `resources.json` theo mẫu 5 giây: CPU quan sát được, working set của cây tiến trình và dung lượng proxy. Có thể bỏ sót tiến trình ngắn và đếm lặp trang RAM chia sẻ; chưa đo GPU. Khi nhận 429, xem `last_quota` và chờ quota khả dụng; benchmark có tùy chọn `--cooldown model=thời-gian-ISO-có-múi-giờ` để giữ mốc chờ đã quan sát khi đổi tiến trình.

## Kết quả cần duyệt và giới hạn còn lại

- Video đã cung cấp dài khoảng 2 giờ 29 phút, VAD lập 73 đoạn. Chưa có kết quả hoàn chỉnh 73 đoạn; hai lượt cũ dừng vì 503/429 và đầu ra sai cấu trúc.
- `artifacts/gemini-pipeline/preview.html` cho phép duyệt mẫu ba phút: baseline 36 cue, bản pipeline trước 59 cue. Mẫu này dùng prompt trước phiên bản cuối, không phải kết quả nghiệm thu bản bàn giao. Nhiều cue hơn không tự chứng minh tốt hơn.
- Review API thật trước đây tìm được một đề xuất nhưng bỏ sót lỗi thiếu câu đã cài. Prompt review đã sửa để đối chiếu hai chiều; cần chạy lại và đếm lỗi sửa đúng, bỏ sót, đề xuất sai.
- Chưa có phụ đề chuẩn. Khi duyệt, lấy mốc ở đầu/giữa/cuối, hai phía ranh giới đoạn, lời lặp thật và OCR chồng audio. Ghi số câu thiếu/thừa/lặp, sai tên/xưng hô và sai thời gian so với mốc đã nghe/xem. Chỉ tính trung vị/p95 khi có đủ nhãn đối chiếu; chưa công bố ngưỡng chất lượng hoặc mức cải thiện.
- Cấu trúc cuối `responseSchema`, quyền model, quota và hiệu quả nhiều key phải được xác minh bằng các lượt chạy sắp tới. Các test mô phỏng đạt không thay thế bước này.

Giữ kết quả chưa tốt cùng `metrics.json`, model, options và thời điểm lỗi để đối chiếu. Không cần xóa checkpoint hay workspace cũ trước khi test bằng output mới.
