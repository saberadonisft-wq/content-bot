# Kế hoạch hợp nhất sửa code tăng tốc OCR

Ngày: **2026-09-20**. Trạng thái: **đang sửa code; phần kiểm tra do người dùng thực hiện**.

**Phân công đã chốt:** Codex sửa code theo các bước bên dưới; người dùng tự kiểm tra, benchmark và đánh giá chất lượng. Không đưa việc viết/chạy test, chạy video mẫu, xây dựng corpus, gán nhãn hay lập báo cáo nghiệm thu vào phạm vi thực hiện của Codex. Việc sửa code không phải chờ nhãn hoặc kết quả kiểm tra.

Tài liệu này là kế hoạch chính, thay thế các lộ trình ở mục 1.1, 5 và 11 của [kế hoạch OCR trước](OCR_OPTIMIZATION_IMPLEMENTATION_PLAN.md). Tài liệu trước giữ bằng chứng và quy trình tham khảo; các yêu cầu kiểm tra ở đó không phải công việc hoặc điều kiện chặn Codex trong đợt này. Phạm vi chung xem [kế hoạch phụ đề tổng thể](SUBTITLE_REMEDIATION_AND_OPTIMIZATION_PLAN.md).

## 1. Mục tiêu và phạm vi

Ưu tiên OCR nhanh hơn bằng hai hướng cùng nhau: **giảm công việc nhận dạng dư thừa** và **cho GPU nhận dữ liệu đều hơn**. Không ép GPU đạt một tỷ lệ sử dụng cố định.

Phạm vi sửa code gồm:

- Giảm chuyển đổi, cấp phát và sao chép ảnh.
- Tận dụng ROI/vùng phụ đề và hộp dòng chữ.
- Dò biên có chọn lọc thay vì OCR dày cả khoảng giữa hai mẫu.
- Tái sử dụng kết quả khi chữ chưa đổi.
- Cho CPU chuẩn bị ảnh đồng thời với GPU inference.
- Batch nhỏ cho nhận dạng chữ nếu interface/model hiện có hỗ trợ.
- Nối cấu hình, cache, lifecycle và trạng thái vào ứng dụng.

Giữ model, precision và lịch lấy mẫu FPS hiện tại trong các bước đầu. Duy trì thời gian theo hình ảnh/PTS, thứ tự phụ đề, bảo vệ video/revision và đường CPU fallback. Giữ đường xử lý cũ để người dùng có thể tắt từng tối ưu khi cần. Các điểm này là yêu cầu triển khai code; phần xác nhận kết quả do người dùng thực hiện.

## 2. Căn cứ chọn thứ tự

Số đo tác vụ đã có `ea6b22de9d3848be98ec`: khoảng 103,4 giây worker cho video khoảng 144 giây; decode 10,9 giây; chuẩn bị ảnh/crop 18,4 giây; gọi OCR 61,8 giây. Trong đó dò biên chiếm 1.577/1.865 calls và 46,8 giây, đã nằm trong thời gian OCR. Frame cache hit bằng 0; thời gian chờ inference lock rất nhỏ.

Code hiện tại gọi `engine(crop_bgr)` từng crop, xử lý decode/chuẩn bị ảnh/OCR chủ yếu nối tiếp và so ảnh bằng `np.array_equal` trên cả crop. Vì vậy ưu tiên giảm copy, đọc lại và khoảng GPU chờ ảnh. Không tăng số engine GPU hoặc bỏ khóa để tăng phần trăm sử dụng.

Đây là căn cứ từ lượt chạy trước, không phải kết quả của kế hoạch mới và không tạo yêu cầu chạy lại benchmark trong phạm vi sửa code.

## 3. Thứ tự sửa code

| Bước | Nội dung | Phụ thuộc code | Kết quả bàn giao |
| --- | --- | --- | --- |
| ACC-00 | Chuẩn hóa cấu hình và ranh giới module | Không | Một cấu hình hiệu lực dùng chung cho API, worker, cache và dedupe; các tối ưu có thể tắt riêng. |
| ACC-01 | Giảm copy/cấp phát ảnh và chuẩn hóa ROI | ACC-00 | Luồng crop rõ ownership, hạn chế giữ cả frame lớn; giữ đúng rotation/PTS. |
| ACC-02 | Tracker và dò biên có chọn lọc | ACC-01 | Theo dõi thay đổi chữ và chỉ gọi OCR bổ sung tại cửa sổ cần thiết; giữ đường dò đầy đủ. |
| ACC-03 | Cache text theo vùng chữ | ACC-02 | Tái sử dụng chữ trong track ổn định, có vô hiệu cache và xác minh định kỳ. |
| ACC-04 | Tái sử dụng hộp dòng, giảm DET | ACC-02/03 | Adapter trả hộp dòng và hỗ trợ rec-only; quay lại DET đầy đủ khi cần. |
| ACC-05 | Producer CPU, consumer GPU | ACC-01 và interface engine/tracker đã ổn định | Queue có giới hạn, inference nối tiếp đúng PTS, hủy và kết thúc sạch. |
| ACC-06 | Batch REC nhỏ | ACC-04/05; interface/model hỗ trợ | Gom crop có sẵn, ánh xạ về đúng frame/dòng; batch 1 vẫn dùng được. |
| ACC-07 | Tích hợp ứng dụng và bàn giao | Các thay đổi code phía trên | API/job/cache/version/UI dùng cấu hình thống nhất; hướng dẫn bật/tắt cho người dùng. |

Đây là phụ thuộc giữa các phần code, không phải các cổng kiểm tra. Tích hợp lần lượt khi cùng sửa `subtitle_ocr.py`; không chờ benchmark hoặc corpus để chuyển bước. Nếu model hiện có không hỗ trợ batch thì giữ batch 1, ghi rõ giới hạn khi bàn giao và tiếp tục phần còn lại.

Mã ACC trong tài liệu này là thứ tự mới thống nhất; không dùng lại bảng ACC hoặc OCR trong các bản kế hoạch trước để suy tiến độ.

## 4. Chi tiết triển khai

### ACC-00 — Cấu hình và tách trách nhiệm

Chuẩn hóa một cấu hình hiệu lực tại backend và truyền xuyên suốt API → job → supervisor → worker → thuật toán. Các tùy chọn nội bộ dự kiến: `selective_refinement`, `glyph_cache`, `recognition_reuse`, `decode_prefetch`, `recognition_batch_size`, giới hạn queue và chu kỳ xác minh chữ/hộp.

Cấu hình baseline cho phép tắt các tối ưu mới và đặt batch 1. Nối phiên bản thuật toán, ROI, sampling, recognition/precision policy và các tùy chọn có thể ảnh hưởng output vào cache/dedupe fingerprint. Trong đợt đầu tách namespace theo cấu hình thử nghiệm, tránh dùng nhầm kết quả cũ.

Tách helper/adapter khi cần để giữ entrypoint `extract_subtitles_ocr` và protocol worker hiện tại. Tái sử dụng bộ đếm/thời gian sẵn có; bổ sung bộ đếm cần thiết ngay trong luồng xử lý, không xây thêm hệ thống benchmark hoặc collector GPU trong đợt này.

### ACC-01 — Giảm copy và tận dụng vùng quét

**Chuẩn bị ảnh:** xử lý `_display_image`, BGR conversion, crop và buffer để tránh chuyển đổi/copy lặp lại cùng frame. Vùng nhớ đưa vào ring/queue phải có ownership rõ; không dùng lại buffer khi consumer còn đọc. Tránh giữ NumPy view làm cả frame lớn tồn tại theo crop. Giữ rotation, tọa độ hiển thị và timestamp nguồn.

**ROI:** chuẩn hóa vùng người dùng chọn một lần; tái sử dụng tọa độ pixel theo video, không tính lại không cần thiết ở mỗi frame. Chừa lề hợp lý khi tạo crop nội bộ cho hộp dòng để không cắt dấu hoặc dòng thứ hai. Không tự thu hẹp ROI gốc của người dùng; tối ưu hộp dòng nằm trong ROI đó và có đường quay lại quét toàn ROI.

Ở bước này giữ cách decode/chuyển màu hiện có. Crop trực tiếp trên YUV, NVDEC hoặc thay resize là phần mở rộng sau, tránh làm phát sinh thay đổi định dạng ảnh trong cùng lần giảm copy.

### ACC-02 — Dò biên theo thay đổi vùng chữ

Thêm tracker dòng/vùng với trạng thái `unknown`, `candidate`, `stable`, `changing`, `blank`. Dùng đặc trưng nét, vị trí và hình học quanh chữ để phát hiện thay đổi; không coi toàn bộ chuyển động nền là chữ đổi. Giữ lịch mẫu FPS hiện tại.

Theo dõi các frame trung gian để không bỏ qua A → B → A, câu ngắn, fade hoặc hai dòng đổi lệch nhau. Tạo cửa sổ nghi ngờ chuyển chữ rồi chọn frame để nhận dạng và xác định biên; không dùng tìm kiếm nhị phân nếu chưa xác định được cửa sổ chỉ có một lần chuyển.

Khi tín hiệu không chắc chắn, quay lại refinement đầy đủ của cửa sổ. Buffer thiếu frame cần thiết thì đọc lại cửa sổ có giới hạn theo PTS hoặc ghi uncertainty. Lỗi inference không được biến thành xác nhận frame trống. Chữ biến mất rồi xuất hiện lại phải tạo occurrence khác.

### ACC-03 — Cache chữ trong track ổn định

Cache text đã nhận dạng theo track/dòng và bằng chứng vùng chữ. Hình nền thay đổi không tự làm mất cache nếu chữ vẫn ổn định; thay nét, dấu, số hoặc hình học phải làm mất hiệu lực kết quả cũ.

Có giới hạn số entry/bytes và chu kỳ xác minh lại bằng OCR. Vô hiệu khi scene cut, thay ROI/rotation, chữ chạm mép, dòng mới, mất track, confidence thấp hoặc tới hạn xác minh. Các ngưỡng đặt trong cấu hình để người dùng có thể điều chỉnh sau khi tự kiểm tra.

Tách cache text khỏi bằng chứng timing: không kéo dài cue qua khoảng trống chỉ vì lần sau cùng text. Giữ quét xác minh toàn ROI để phát hiện chữ ngoài các hộp đang theo dõi. Ghi hit/miss và lý do vô hiệu bằng diagnostics sẵn có.

### ACC-04 — Tái sử dụng hộp dòng

Adapter OCR trả text, hộp từng dòng, thứ tự đọc và confidence. Khi hộp còn hợp lệ nhưng text cần nhận dạng mới, đưa crop dòng vào REC thay vì chạy lại DET toàn ROI. Giữ CLS theo baseline; bỏ CLS không nằm trong đợt này.

Chạy đầy đủ DET/CLS/REC khi hộp đổi, crop có dấu hiệu cắt chữ, REC rỗng/điểm thấp, tới lịch xác minh hoặc xuất hiện dòng mới. Giữ quét toàn ROI định kỳ để không chỉ nhìn các hộp cũ. Tái sử dụng logic sort/merge dòng hiện tại khi tạo observation.

Đọc implementation RapidOCR và shape/model metadata để nối đúng interface; không sửa trực tiếp `site-packages`. Nếu một đường rec-only không được hỗ trợ thì giữ full OCR cho đường đó và ghi rõ giới hạn.

### ACC-05 — CPU chuẩn bị ảnh đồng thời với GPU

Một producer sở hữu decoder và chuẩn bị crop; một consumer sở hữu engine/tracker, lựa chọn observation và cập nhật kết quả. Queue truyền `sequence, pts_ms, crop, metadata`, giữ thứ tự và không drop frame khi đầy.

- Cấu hình ban đầu: queue tối đa **4 crop / 16 MiB**; ring refinement giữ **32 frame / 24 MiB** như giới hạn hiện có. Cả số phần tử và bytes đều có giới hạn.
- Producer chờ khi queue đầy và kiểm tra cancel. Truyền EOF/lỗi sang phía kia; tránh kẹt khi consumer lỗi, đang dò lại cửa sổ hoặc producer không thể ghi terminal event vào queue đầy.
- Hủy đánh thức cả hai phía và đóng decoder. Supervisor vẫn thu hồi worker nếu native inference không trả về trong deadline.
- Tiến độ dựa trên PTS đã xử lý; không tính frame mới decode là hoàn tất. Không đổi việc chọn frame mẫu sang chia frame index.
- Giữ một engine GPU và GPU slot hiện có; không mở nhiều job/engine cùng lúc. Đường tuần tự vẫn dùng được khi tắt `decode_prefetch`.

### ACC-06 — Batch nhận dạng chữ

Gom các crop REC đã có sẵn, trước hết trong cùng observation. Cấu hình cho phép batch 1/2/4/8 khi model/interface hỗ trợ; batch 1 là đường cơ sở. Gom theo hình dạng tương thích và giữ ánh xạ `sequence/pts/line_id` để phục hồi đúng thứ tự.

Không chờ đủ batch nếu tracker cần kết quả hiện tại để chọn frame sau. Flush khi hết nhóm độc lập hoặc EOF. Nếu có chờ gom, giới hạn thời gian qua cấu hình; không để queue/batch làm treo tiến độ hoặc hủy.

Giữ model và precision hiện có. Không gộp batch DET, FP16, TensorRT, NVDEC hay đổi model vào bước này. I/O Binding là phần mở rộng sau nếu người dùng chỉ ra chi phí truyền dữ liệu cần xử lý; đợt này ưu tiên sửa luồng công việc và batch có sẵn.

### ACC-07 — Nối vào ứng dụng và bàn giao code

Nối các tùy chọn vào supervisor/worker, config, cache key và job dedupe. Duy trì publication theo đúng video/revision, source document, lưu phiên bản và export SRT. Không đổi cấu hình giữa một job đang chạy.

Giữ đường CPU-only, `auto` fallback hữu hạn và lỗi rõ ràng khi bắt buộc CUDA. Khi fallback phải thu hồi GPU worker rồi chạy lại theo quy trình hiện có; không trộn kết quả dở giữa hai runtime. Hủy không kích hoạt fallback.

Frontend chỉ sửa khi cần để giữ ROI preview/progress/cancel đúng với luồng mới; không thêm nút chỉnh phần trăm GPU. Nếu chỉnh nhãn FPS, giải thích đây là tần suất lấy mẫu ban đầu và vẫn có dò biên bổ sung; tránh mô tả FPS cao là bảo đảm đọc đúng chữ hơn.

Bàn giao danh sách file thay đổi, cấu hình bật/tắt từng tối ưu, trạng thái mặc định, giới hạn kỹ thuật và phần chưa triển khai. Người dùng tự thực hiện mọi kiểm tra; không ghi “đã đạt”, “đã tăng tốc” hoặc con số cải thiện khi chưa có kết quả do người dùng xác nhận.

## 5. Phạm vi file dự kiến

| Khu vực | File/module | Nội dung sửa |
| --- | --- | --- |
| Thuật toán | `backend/app/services/subtitle_ocr.py`; helper/adapter mới nếu cần | Crop, tracker, refinement, cache vùng, hộp dòng, REC và queue. |
| Runtime | `backend/app/services/subtitle_ocr_worker.py`, `subtitle_ocr_supervisor.py` | Truyền cấu hình, lifecycle producer/consumer, cancel/fallback. Tái sử dụng `model_resources.py` và `owned_process.py`. |
| Config/API/job | `backend/app/config.py`, `backend/app/api/subtitles.py`, `backend/app/schemas.py`, `backend/app/services/subtitle_jobs.py` | Cấu hình hiệu lực, validation, fingerprint/dedupe và publication; chỉ sửa contract khi cần. |
| Frontend khi cần | `frontend/src/SubtitleStudio.tsx`, `frontend/src/subtitles/SubtitleOcrBox.tsx`, `useExtractionJobs.ts`, `frontend/src/api/types.ts` | Đồng bộ contract, ROI/progress/cancel và giải thích FPS. |
| Tài liệu bàn giao | Kế hoạch này và hướng dẫn cấu hình liên quan | Ghi phần đã sửa, cách bật/tắt và giới hạn; không tạo báo cáo kiểm thử. |

Các file benchmark, test và công cụ gán nhãn không nằm trong phạm vi sửa theo yêu cầu hiện tại. Backend chốt contract trước khi frontend cập nhật. Các sửa đổi cùng module được tích hợp tuần tự để không ghi đè công việc khác trong workspace.

## 6. Cách triển khai và trạng thái hiện tại

Đọc code để xác định dependency là một phần triển khai; không chạy test, lint, build, benchmark hoặc thử video thay người dùng.

Các tối ưu mới có công tắc để người dùng bật/tắt riêng. Khi quay về đường cũ, áp dụng cho job mới và dùng cache namespace đúng cấu hình. Không yêu cầu người dùng hoàn thành kiểm tra giữa các bước sửa code.

**Phiên bản hiện tại:** `rapidocr-v7-caption-continuity` (2026-09-25). Bổ sung ổn định câu trước bước lọc thời lượng để tránh một câu bị tách thành đoạn lớn và các đoạn nhỏ do kết quả OCR dao động. Các tối ưu v6 bên dưới được giữ nguyên.

- Chỉ xem xét khác khoảng trắng hoặc một ký tự Hán trong câu từ 8 ký tự trở lên; không nối khác số, phủ định, dấu tiếng Việt, thêm/bớt ký tự. Đồng thời yêu cầu hình học dòng và nét chữ khớp mốc cố định, kiểm tra cục bộ để không bỏ qua một dấu chấm nhỏ. Bộ so hiện hỗ trợ chữ sáng có viền tối; thiếu hộp/ảnh không rõ/kiểu chữ khác giữ hành vi tách cũ. Đây vẫn là heuristic, không phải bảo đảm chất lượng cho mọi nguồn.
- Chọn text theo tổng thời lượng thực nhân confidence; các frame refinement dày hoặc một lần đọc sai confidence cao không tự chiếm ưu thế. Giữ tối đa 16 biến thể và chỉ giữ mask cho câu đang mở; giải phóng khi đóng câu. Đoạn đã nối có `needs_review=true`; chỉ số `stabilized_observations` cho biết số quan sát được ổn định.
- Khoảng trống OCR xác nhận vẫn đóng câu. Việc nối thực hiện trước lọc đoạn ngắn, không kéo dài hay xóa hàng loạt đoạn nhỏ. Không sửa các phiên bản nguồn/bản dịch đã lưu.
- Kiểm tra cục bộ: các test giải mã video lossless và hồi quy OCR; trên 7 giây đầu video `dc2dbd79ba124f84a4e0e91ff325b649`, CPU/2 FPS/vùng OCR cũ cho một câu `03.167–06.533` thay cho chuỗi biến thể `大`/`天`/`犬`. Artifact: `artifacts/ocr-fragment-diagnosis-20260925/v7-first-7s.json`. Chưa chạy toàn video, worker GPU hoặc benchmark hiệu năng v7.
- Khởi động lại backend rồi tạo job OCR mới và dịch từ nguồn mới. Namespace v7 tách khỏi cache/dedupe v6; bản dịch 38 đoạn cũ vẫn giữ nguyên cho tới khi người dùng áp dụng kết quả mới.

**Nền tối ưu v6:** Cấu hình bất biến `OcrAcceleration` được chụp tại API, truyền qua supervisor/worker/CPU retry và đưa vào fingerprint cache/dedupe. Các thay đổi sau đang bật mặc định cho job mới:

- Giữ mẫu OCR định kỳ; so đặc trưng nét sáng/tối ở độ phân giải gốc để tái sử dụng kết quả trong cửa sổ refinement. So thay đổi cục bộ thay vì chỉ trung bình toàn dòng; xét thay đổi độ sáng để tránh giữ chữ xuyên fade. Cache hit không làm mới thời hạn xác minh. Hai mốc cho kết quả text khác nhau nhưng cùng khớp đặc trưng sẽ buộc OCR lại.
- Có thêm kiểm tra frame trung gian khi endpoint cùng text và mốc trước có chữ đủ tin cậy, để tìm thay đổi A → B → A hoặc khoảng trống. Trường hợp mốc không có đặc trưng đủ tin cậy vẫn giữ điều kiện mở cửa sổ của đường cũ; chưa thể khẳng định phát hiện mọi câu ngắn giữa hai mẫu trống.
- REC-only trong cửa sổ ngắn khi không thấy dấu hiệu dòng chữ mới ngoài hộp và nét không chạm mép crop. Giữ CLS. REC rỗng/điểm thấp quay về full OCR; REC-only không tự tạo hạn xác minh mới. Mẫu định kỳ vẫn chạy detector toàn ROI.
- Gom crop từ nhiều frame độc lập trong cửa sổ để batch CLS/REC. Có ánh xạ frame/dòng, kết quả đưa vào tracker đúng PTS và nhóm cuối được xử lý ngay. DET vẫn chạy riêng mỗi frame cần detection. Đường adapter dùng RapidOCR 1.2.3; interface khác quay về full OCR.
- Dùng FFmpeg crop qua PyAV trước khi chuyển vùng ảnh sang BGR, với viền phụ cho chuyển màu, giữ gốc chroma chẵn và ánh xạ rotation 0/90/180/270. Định dạng/filter không hỗ trợ tự chuyển sang BGR toàn frame rồi crop. Crop trả mảng sở hữu bộ nhớ; bỏ copy thứ hai trong đường tuần tự. Không thay FPS hoặc độ phân giải chữ.
- Decode `AUTO` giới hạn 2 luồng; giữ prefetch một producer/một consumer hiện có. Các bộ đệm không tăng theo độ dài video: ring 32 frame/24 MiB, queue 4 crop/16 MiB, nhóm frame/bộ crop REC 16 MiB (một frame quá lớn vẫn dùng đường riêng), tối đa 4 mốc đặc trưng giữa các nhóm.

**Trạng thái bàn giao v6 trước đây: chưa chạy test/lint/build/video/benchmark**, theo phân công. Kiểm tra riêng bản sửa v7 được ghi ở đầu mục này; chưa phải kết quả tăng tốc hoặc chứng nhận chất lượng toàn pipeline. Đặc trưng nét là heuristic: nền nhiều chi tiết, chữ sát mép, mờ, confidence thấp có thể làm giảm hit và tăng OCR bổ sung. Chuyển màu sau crop và batch cũng có thể làm output khác; mọi cấu hình có namespace riêng. Không có cam kết video 2 giờ xử lý trong một thời gian cụ thể.

### 6.1. Cấu hình trong `backend/.env`

Khởi động lại backend sau khi đổi cấu hình; áp dụng cho job mới. Không cần chỉnh request/frontend.

| Biến môi trường | Mặc định | Tác dụng |
| --- | --- | --- |
| `CONTENT_BOT_SUBTITLE_OCR_SELECTIVE_REFINEMENT` | `true` | Tái sử dụng text theo đặc trưng nét trong refinement và kiểm tra thay đổi trung gian. |
| `CONTENT_BOT_SUBTITLE_OCR_RECOGNITION_REUSE` | `true` | Thử CLS/REC dùng hộp cũ; lỗi chất lượng quay về detector. |
| `CONTENT_BOT_SUBTITLE_OCR_REFINEMENT_BATCH_SIZE` | `4` | Giới hạn frame mỗi nhóm và crop mỗi batch CLS/REC; hợp lệ 1–8. |
| `CONTENT_BOT_SUBTITLE_OCR_VERIFY_INTERVAL_MS` | `500` | Tuổi tối đa của mốc đã OCR để tái sử dụng; hợp lệ 50–1000. Mẫu định kỳ vẫn giữ nguyên. |
| `CONTENT_BOT_SUBTITLE_OCR_MIN_REUSE_CONFIDENCE` | `0.85` | Ngưỡng cho đặc trưng nét/hộp; hợp lệ 0.7–1.0. |
| `CONTENT_BOT_SUBTITLE_OCR_DECODE_THREADS` | `2` | 1–8 bật AUTO decode; 0 giữ chế độ decode cũ. |
| `CONTENT_BOT_SUBTITLE_OCR_CROP_BEFORE_BGR` | `true` | Crop ảnh native trước chuyển BGR; false dùng đường toàn frame. |
| `CONTENT_BOT_SUBTITLE_OCR_PREFETCH_FRAMES` | `true` | Producer chuẩn bị crop song song với OCR; cờ đã có từ v4. |
| `CONTENT_BOT_SUBTITLE_OCR_GLYPH_CACHE` | `true` | Cache CLS/REC pixel-identical của mẫu định kỳ; độc lập với cache refinement mới. |

Để tắt các tối ưu tốc độ sau v4 trong code hiện tại: tắt `SELECTIVE_REFINEMENT`, `RECOGNITION_REUSE`, `CROP_BEFORE_BGR`; đặt `REFINEMENT_BATCH_SIZE=1`, `DECODE_THREADS=0`. Giữ hai cờ v4 theo cấu hình muốn so sánh. Muốn đường tuần tự không cache dòng thì tắt thêm `PREFETCH_FRAMES` và `GLYPH_CACHE`. Bước ổn định câu v7 vẫn hoạt động trên cả hai đường; các cờ này không khôi phục toàn bộ thuật toán v4. Namespace v7 tách khỏi cache các bản cũ; không cần xóa cache hoặc phiên bản người dùng.

### 6.2. Chỉ số bàn giao

Job result có `acceleration` chứa cấu hình hiệu lực. Trong `metrics` bổ sung:

- `visual_cache_hits`, `visual_refinement_calls`: frame refinement tái sử dụng và frame vẫn cần nhận dạng.
- `refinement_duplicate_frames`: frame trùng pixel trong cùng nhóm được ánh xạ về kết quả đã nhận dạng; vẫn phát observation riêng theo PTS.
- `visual_feature_builds`, `visual_feature_cache_hits`: lượt tính đặc trưng và lượt lấy lại từ cache giới hạn trong cửa sổ. Cache-hit toàn job trả các bộ đếm này bằng 0.
- `recognition_reuse_attempts`, `recognition_reuse_hits`, `recognition_reuse_fallbacks`: số lần thử/bỏ DET thành công/quay về full OCR do confidence.
- `refinement_detector_calls`, `refinement_rec_batches`, `refinement_rec_crops`, `refinement_full_retries`: đường batch/refinement; retry full được cộng vào `ocr_calls`/`refinement_calls`. `refinement_rec_batches` đếm lượt gọi adapter, thư viện có thể tách nhỏ tiếp.
- `native_crop_frames`, `bgr_crop_frames`: đường chuẩn bị ảnh thực dùng.

Giữ các thời gian decode, chuẩn bị ảnh, tracking, DET/CLS/REC, queue wait và worker wall đã có. Các thời gian có thể chồng lấn khi prefetch bật, không cộng chúng thành tổng wall-time. Job cache-hit trả bộ đếm mới bằng 0 và giữ số đo cũ trong `cached_metrics`.

Tham khảo interface: [FFmpeg crop](https://ffmpeg.org/ffmpeg-filters.html#crop), [PyAV filter graph](https://pyav.org/docs/stable/api/filter.html), cùng implementation RapidOCR 1.2.3 và PyAV 18.0.0 đang cài trong backend.

### 6.3. Sửa sau review v5

1. **Tách nền chuyển động:** bỏ điều kiện mọi thay đổi pixel ngoài hộp đều vô hiệu cache. Dùng cụm nét có chiều cao, độ rộng và mật độ giống vùng chữ để tìm dòng mới trên toàn ROI; chỉ phần nét mới nằm ngoài hộp mới là tín hiệu cần OCR. Cảnh có quá nhiều cụm hợp lệ được xem là không chắc chắn và quay về OCR. Đây vẫn là heuristic, chưa chứng minh chất lượng trên video thật; chữ mới quá nhỏ/khác cỡ có thể chỉ được phát hiện ở mẫu full OCR tiếp theo.
2. **Cache REC-only:** lưu mốc text mới với `timestamp_ms` riêng và kế thừa `verified_timestamp_ms` của detector. Hạn dùng phải thỏa cả hai mốc. Full OCR fallback trả `full_detection=True` để cập nhật mốc xác minh; cache hit không kéo dài hạn và hộp không được cộng padding lặp.
3. **Loại frame trùng:** so pixel trong nhóm tối đa 8 frame trước khi gọi model. Các frame trùng chia sẻ kết quả nhưng giữ riêng PTS/observation, kể cả A → blank → A. Bộ đếm gọi OCR chỉ tính frame đại diện và retry thực sự.
4. **Tái sử dụng đặc trưng:** exact-match chạy trước morphology; kiểm tra thay đổi trung gian bị bỏ qua khi endpoint đã khác text hoặc trigger cũ đã yêu cầu refinement. `FeatureCache` dùng chung cho lần kiểm tra, refinement và tạo mốc; tối đa 32 entry/24 MiB cho các mảng giữ trong cache, xóa sau cửa sổ. Metadata cụm chữ được lưu theo tối đa 8 chiều cao tham chiếu, mỗi nhóm tối đa 256 cụm, tách khỏi ngân sách mảng. Mốc đang dùng có thể giữ thêm đặc trưng sau khi cache bị eviction; đây không phải trần tổng RAM worker. Đặc trưng có thể phải tính lại khi entry vượt ngân sách hoặc bị eviction.
5. **Batch theo hình dạng:** sắp crop theo tỷ lệ rộng/cao, tách nhóm khi tỷ lệ lớn nhất vượt 1,5 lần nhỏ nhất; giữ giới hạn số crop của cấu hình. Ước tính tensor float32 sau padding theo shape recognizer, giới hạn 8 MiB mỗi nhóm (không phải tổng VRAM). Một crop đơn vượt giới hạn sẽ trả lỗi rõ ràng, không âm thầm thu nhỏ chữ. Kết quả CLS/REC được ánh xạ về index crop gốc trước khi ghép dòng/frame.

File sửa chính: `backend/app/services/subtitle_ocr.py`, `backend/app/services/subtitle_ocr_tracking.py`. Không thêm dependency, không đổi request UI. Khởi động lại backend và tạo job mới để dùng v6. Chưa chạy test, lint, build hoặc benchmark trong lượt sửa này theo phân công; không lấy số liệu v4 làm kết quả v6.
