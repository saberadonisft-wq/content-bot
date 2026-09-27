# Kế hoạch sửa lỗi và tối ưu pipeline phụ đề, lồng tiếng, render

**Kế hoạch OCR hiện hành:** [Kế hoạch hợp nhất tăng tốc OCR](OCR_ACCELERATION_PLAN.md) — hợp nhất giảm công việc OCR dư thừa với CPU/GPU chạy đồng thời và batch nhỏ; ưu tiên tốc độ, giữ chất lượng, không đặt mức GPU cố định. Các tối ưu mới đang ở trạng thái kế hoạch.

**Phân công đợt OCR này:** Codex sửa code theo kế hoạch mới; người dùng tự kiểm tra, benchmark và đánh giá kết quả. Các yêu cầu kiểm tra lịch sử trong tài liệu không chặn việc sửa code của đợt này.

Trạng thái: **đã đóng đợt triển khai code để bàn giao nghiệm thu ngày 2026-09-15**; xem [tiến độ và bằng chứng](SUBTITLE_REMEDIATION_PROGRESS.md). Phạm vi gồm phần mở rộng OCR/ASR/dịch và các module bổ sung trong bản tổng hợp 11 tính năng. Các giới hạn nghiệm thu thực địa vẫn được ghi riêng, không coi module chưa có call site hoặc benchmark fixture là đã hoàn tất chất lượng sản phẩm.

Căn cứ: [review 18 vấn đề](../artifacts/reviews/ocr-asr-code-review.md), [rà soát tốc độ pipeline](PIPELINE_SPEED_REVIEW_2026-09-14.md), [ranh giới module](MODULE_BOUNDARIES.md), [nghiệm thu UI cũ](SUBTITLE_UI_PERFORMANCE.md). Các số đo lịch sử không đại diện cho bản mở rộng mới; phải đo lại. Kiểm tra code hiện tại trước mỗi hạng mục để không làm lại phần đã được sửa.

**Bổ sung ngày 2026-09-20:** [Kế hoạch triển khai tối ưu OCR theo benchmark CPU/GPU và tham khảo ViralCrawl](OCR_OPTIMIZATION_IMPLEMENTATION_PLAN.md). Đợt bổ sung đã tích hợp OCR-01 GPU runtime/CPU fallback và xây dựng OCR-00 blind-first review pack cùng validator. Ground truth thủ công và corpus holdout vẫn pending; chưa bắt đầu OCR-02 và chưa nghiệm thu chất lượng tự nhiên. Tài liệu bổ sung cụ thể hóa phần OCR ở đợt 2 và runtime ở đợt 7; không thay đổi trạng thái bàn giao lịch sử phía trên.

## 1. Mục tiêu và thứ tự

1. Không mất chỉnh sửa hoặc áp kết quả vào nhầm video.
2. Trích xuất đúng nội dung và timeline; người dùng sửa nguồn rồi dịch đúng bản nguồn đó.
3. Job có thể hủy, tiếp tục sau lỗi và dùng lại kết quả còn hợp lệ.
4. Giảm công việc lặp, thời gian chờ, RAM/VRAM và số request mà giữ chất lượng.
5. Nối từng tính năng phụ vào app sau khi có kiểm thử tích hợp và bằng chứng hoạt động.

“Tối ưu tối đa” ở đây là tìm cấu hình tốt nhất trong giới hạn chất lượng, bộ nhớ và phần cứng đã đo. Không cam kết hệ số tăng tốc hoặc tự giảm chất lượng để đạt số liệu đẹp.

## 2. Kiến trúc dữ liệu đích

Video → OCR hoặc ASR → tài liệu nguồn có thể sửa → dịch Gemini → tài liệu dịch → giọng đọc/render.

- Giữ lựa chọn Gemini phân tích video trực tiếp hiện có.
- Tài liệu nguồn sở hữu ID, nội dung nguồn, ngôn ngữ, mốc hiển thị/lời nói và bằng chứng trích xuất. Tài liệu dịch liên kết tới revision nguồn và giữ ID/timestamp do backend quản lý.
- Sửa nguồn làm bản dịch liên quan trở thành cần cập nhật. Sửa bản dịch không thay đổi nguồn.
- Khóa người dùng, undo/redo và phiên bản vẫn có hiệu lực. Không tự áp job nếu video, nguồn hoặc tài liệu đích đã đổi từ lúc submit.
- Phân biệt mốc sub hiển thị và mốc lời nói. OCR không tự trở thành bằng chứng căn lồng tiếng.
- Có metadata cấu hình/model/thuật toán và độ phân giải thời gian thực tế. Confidence mô hình không được nâng thành chứng nhận chính xác.
- Tái sử dụng schema/store hiện có khi đủ; nếu cần mở rộng phải có migration cho draft và phiên bản cũ.

## 3. Các đợt triển khai

### Đợt 0 — Cố định baseline và bộ kiểm chứng

Đầu ra:

- Ghi revision/diff đầu vào, dependency, phần cứng và cấu hình đo. Giữ nguyên thay đổi đang có của người dùng.
- Chuyển các probe tái hiện trong review thành regression test có assertion về hành vi mong muốn.
- Bộ media tổng hợp: CFR/VFR, PTS khác 0, rotation 90/180, portrait/landscape, không audio, khoảng im lặng, sub nhiều dòng, sub lặp, thay đổi một chữ/số/phủ định, nhạc stereo/dual-mono.
- Bộ video có nhãn thủ công theo loại nội dung, tách mẫu hiệu chỉnh và mẫu nghiệm thu. Dùng media được phép kiểm thử; ghi rõ mock, model thật và API thật.
- Đo thời gian từng bước, số lần OCR/FFmpeg/API, cache hit, peak RAM/VRAM, thời gian hủy và độ trễ thao tác UI.

Nghiệm thu: mỗi lỗi ưu tiên có test đỏ hoặc kịch bản browser tái hiện; không dùng riêng tổng số test pass làm tiêu chí chất lượng.

### Đợt 1 — Bảo vệ dữ liệu và hoàn thiện vòng đời Studio

Giải quyết review #1, #2, #5, #13.

- Tách hook điều phối extraction/translation khỏi SubtitleStudio.tsx; giữ một chủ sở hữu state chỉnh sửa.
- Mỗi request/job gắn video ID, source revision, document snapshot và cấu hình. Abort/ignore request cũ khi đổi video hoặc unmount; chặn submit lặp khi request chưa trả job ID.
- Khi job xong: tự áp nếu snapshot còn đúng; nếu có sửa mới, giữ kết quả thành phiên bản để người dùng đối chiếu. Không ghi đè âm thầm.
- Có bước “Kiểm tra phụ đề nguồn” với sửa text/timing, undo, lưu revision và xuất SRT nguồn. Hiển thị đúng trạng thái nguồn/bản dịch; cập nhật danh sách phiên bản sau OCR/ASR/dịch.
- Lưu draft của phương thức trích xuất, vùng OCR, ngôn ngữ, model và job đang chạy; phục hồi theo đúng video.
- Sửa resize đủ bốn góc, keyboard controls và ánh xạ vùng theo khung video thật. Kiểm tra letterbox, resize preview và rotation.
- Khai báo dependency bắt buộc; kiểm tra startup cài mới. Engine tùy chọn thiếu dependency/model phải có trạng thái cụ thể, không làm hỏng toàn API.
- Thay result:any bằng các kiểu kết quả job rõ ràng; bỏ khai báo enum/type trùng.

Nghiệm thu: đổi video, sửa nguồn khi đang dịch, reload và double-click không làm mất chỉnh sửa hoặc nhận kết quả nhầm; sửa chữ nguồn phải xuất hiện trong payload dịch và SRT nguồn.

### Đợt 2 — Sửa độ đúng OCR, sau đó giảm số lượt nhận diện

Giải quyết review #3, #4, #7, #8 và phần OCR của #12.

- Chuẩn hóa PTS theo timeline đã thống nhất với media probe/preview; giữ VFR, rotation và tỉ lệ điểm ảnh khi ánh xạ ROI.
- Auto-probe thực sự áp vùng đã dò. Người dùng xem/sửa vùng; nếu không đủ bằng chứng thì báo chưa xác định được, không giả định tìm đúng vùng.
- Phân biệt ba kết quả mỗi quan sát: đọc được chữ, xác nhận không có chữ, nhận diện thất bại/không chắc chắn. Không nuốt lỗi inference thành “không có sub”.
- Thay gộp đơn thuần theo similarity bằng trạng thái theo dõi lần hiển thị. Dùng tính ổn định qua nhiều quan sát và thay đổi hình ảnh để xác nhận đổi câu; bảo vệ số, phủ định, tên riêng và hai lần lặp có khoảng trống.
- Nhận diện nhiều dòng theo cụm hình học; không dùng một ngưỡng pixel cố định cho mọi độ phân giải.
- Theo dõi thay đổi nhẹ trong ROI rồi nhận diện khi cần. Cache kết quả vùng không đổi; kiểm tra lại định kỳ để tránh giữ chữ cũ.
- Khi phát hiện biên chuyển câu, khảo sát frame quanh biên để tinh chỉnh. Chỉ áp dụng đường recognition riêng sau khi xác minh engine hỗ trợ và ROI vẫn đủ nội dung; fallback detection khi hình học thay đổi.
- Giới hạn queue decode/inference và bộ nhớ. Không OCR full-resolution mọi frame theo mặc định, không giữ toàn video trong RAM.
- Cache/dedupe dùng cùng cấu hình hiệu lực: video, ROI, auto-probe, ngôn ngữ, model revision, sample policy, min_duration, max_gap, thuật toán. Ghi atomic, xử lý cache lỗi và quota.
- Công bố độ phân giải thời gian theo quan sát/refinement, không cố định 100 ms cho mọi sample_fps.

Nghiệm thu: không mất câu 100→200, phủ định hoặc lần lặp độc lập trong fixture; đúng thứ tự nhiều dòng; PTS khác 0 không làm rỗng sub; đổi cấu hình thực sự vô hiệu hóa cache. Bản tối ưu không làm xấu tỷ lệ bỏ/gộp câu và sai số biên so với bản đã sửa đúng.

### Đợt 3 — ASR ổn định trên CPU/GPU và video dài

Giải quyết review #6 và phần ASR của #12.

- Chuẩn hóa auto language thành None, validate trước khi decode/nạp model.
- Hiển thị model đã cài, đang tải, sẵn sàng hoặc lỗi; chọn model/device theo khả năng thật và cho người dùng thay đổi.
- Dùng luồng đọc/audio window có giới hạn bộ nhớ, giữ offset trên video gốc. Xử lý overlap và tiếng nói qua ranh giới để không mất/lặp từ.
- Giữ task=transcribe, VAD và timestamp lời nói; không nối im lặng rồi đánh lại timeline từ 0.
- Chuẩn hóa cue/word/speech bounds cùng nhau; không clamp cue nhưng để word nằm ngoài biên.
- Hủy ffmpeg/worker có deadline, thu hồi process và khóa. Exception hủy đi qua riêng, job kết thúc canceled.
- Cache model có giới hạn dung lượng/số model và vòng đời; không giữ vô hạn mọi model GPU từng chọn.
- Dùng quyền sở hữu GPU chung với tác vụ TTS/separation khi cần, không thêm lock riêng không nhìn thấy các worker khác. Chờ khóa có thể hủy.
- Thử batch và compute type theo cấu hình máy; OOM có fallback hữu hạn, hiển thị rõ lựa chọn thiết bị. Đánh giá lại chất lượng khi thay precision/batch.
- Cache ASR có model revision, cấu hình decoding/VAD và audio/timeline identity đầy đủ.

Nghiệm thu: auto language chạy được; khoảng im lặng/offset/word bounds đúng; hủy không báo failed hoặc bỏ process; RAM có giới hạn theo window, không tăng tuyến tính vì giữ bản PCM toàn video.

### Đợt 4 — Dịch có checkpoint, fallback và hợp đồng ID chặt chẽ

Giải quyết review #9, #10, #11, #14.

- Chia sẻ transport/dispatcher/budget hiện có cho request text và video; không duy trì một nhánh retry yếu hơn.
- Tôn trọng deadline còn lại trên mọi request; report quota/auth/permission đúng scope; retry có giới hạn và chuyển model theo policy sẵn có.
- Validator kiểm tra kiểu dữ liệu, ID thiếu/thừa/trùng, text rỗng trước khi áp bất cứ kết quả nào. Gemini không có quyền sửa timestamp nguồn.
- Lưu atomic từng batch thành công và trạng thái chưa hoàn thành; resume chỉ đọc checkpoint khớp nguồn/prompt/model/config. Hủy/restart không xóa phần đã hoàn thành hợp lệ.
- Trả partial result có trạng thái rõ ràng và cho UI tiếp tục. Không nhầm nguồn chưa dịch thành bản dịch đã xong.
- Chia batch theo token/độ dài và ngữ cảnh, không chỉ số cue. Ban đầu giữ tuần tự để ổn định tính nhất quán.
- Sau baseline, thử dịch đồng thời các batch độc lập bằng ngữ cảnh nguồn chồng lấn và thuật ngữ dùng chung; không phụ thuộc bản dịch batch trước nếu chạy song song. Chỉ bật khi nhất quán xưng hô/thuật ngữ không giảm.
- Tách cache nội dung dùng lại khỏi việc lưu phiên bản theo video. Sửa một cue chỉ làm mất cache của batch và ngữ cảnh bị ảnh hưởng.
- Có provenance model thực dùng, chi phí/token nếu API trả usage, cảnh báo và cấu hình song ngữ nhất quán.

Nghiệm thu: batch 1 xong/batch 2 lỗi thì resume không gọi lại batch 1; 503 thử fallback đúng; 429 chờ đúng policy; request hết deadline không được gửi; ID sai không được nhận; sửa nguồn mới không bị checkpoint cũ ghi đè.

### Đợt 5 — Lồng tiếng và âm nền

Giải quyết review #15, #16; hoàn thiện tính năng giọng và phân vai.

- Sửa normalizer theo nhóm số đúng, test tới nghìn tỷ trở lên. Xử lý số âm/thập phân/ngày/đơn vị theo ngữ cảnh, tránh đổi mã số/tên sản phẩm sai nghĩa. Từ điển phát âm có ưu tiên rõ ràng.
- Giữ lời đọc riêng với sub hiển thị, cho xem nội dung sau chuẩn hóa và nghe thử. Thêm cấu hình/phiên bản normalizer vào cache audio.
- Nối tách thoại thành job có cache, tiến độ, hủy và ngân sách GPU. Giữ các stem độc lập, cho nghe trước/sau, chọn âm nền và chỉnh mức mix/ducking.
- Thay fallback ngược pha gây mất mono bằng trạng thái/thuật toán có giới hạn rõ và kiểm chứng; không trả thành công tách nền nếu chỉ tạo audio hỏng. Test mono, dual-mono, stereo, clipping, thoại rò và mất SFX.
- Giữ VieNeu đang hoạt động làm engine hiện có. CapCut chỉ thành engine khả dụng sau khi xác minh contract, giọng và tạo audio thành công trong điều kiện hỗ trợ. Có timeout, cancellation, kiểm tra file và báo lỗi cụ thể. Không hiển thị endpoint ping hoặc placeholder như tính năng hoàn chỉnh.
- Phân vai dùng speaker_id và voice assignment. F0 chỉ là gợi ý đặc tính giọng, không đồng nhất với danh tính hoặc giới tính; không đủ bằng chứng thì unknown và cho sửa. Một người không bị đổi voice chỉ vì cao độ thay đổi.
- Giữ giới hạn tốc độ/căn giọng và khóa người dùng; đo đầu/cuối lời nói thực của WAV trước khi xác nhận khớp.

Nghiệm thu: audio có thể phát và downmix mono, không NaN/clipping ngoài ngưỡng; đúng giá trị số; sửa một câu chỉ tạo lại audio liên quan; luồng chọn giọng/tách nền hoạt động từ UI qua job tới render. Nghe đối chiếu bắt buộc cho đánh giá chất lượng giọng/nền.

### Đợt 6 — Render, chia cảnh, thumbnail và caption

Giải quyết review #17; tối ưu xử lý lặp theo báo cáo tốc độ hiện có.

- Tách cache phần hình, clip audio đã DSP, track mix và mux cuối. Chỉ thay gain/voice không được encode hình lại; thay sub/mask/cut phải vô hiệu hóa đúng phần hình.
- Cache PCM từng clip theo checksum, tempo/gain/fade/resample và phiên bản công cụ; chỉnh một clip chỉ chuyển đổi lại clip đó. Quota, pin khi dùng và atomic writes dùng chung quy tắc cache.
- Tái sử dụng metadata/checksum đã xác minh trong một export snapshot; không dựng waveform peaks lại khi chỉ cần xác minh asset.
- Giữ đúng thứ tự DSP, rounding sample và onset; so sánh PCM/biên giọng trước–sau. Không tự đổi encoder, bitrate hoặc model chỉ để lấy tốc độ.
- Hợp nhất masking mới với renderer hiện có, tránh hai cách render khác nhau giữa preview và export. Dùng chung tọa độ/timeline và tối ưu filter trong ROI.
- Scene splitter luôn giữ đầu/đuôi video và biến đổi cả cue/word/speech timestamps. Nếu một cảnh vượt độ dài shorts thì policy phải rõ: giữ cảnh dài hoặc cho chọn cắt trong cảnh; không đồng thời hứa giới hạn cứng và tuyệt đối không cắt cảnh.
- Shorts xuất video kèm SRT/JSON và manifest liên kết về nguồn; chống tên trùng và không ghi đè file gốc.
- Thumbnail trả nhiều ứng viên, điểm và preview cho chọn; cache khung hình đã giải mã, xử lý thiếu duration/rotation, đóng decoder khi lỗi. Đánh giá độ rõ/bố cục, không coi heuristic là bằng chứng tăng CTR.
- Caption giữ bản gốc và bản làm sạch, cho sửa trước áp dụng. Bảo vệ nội dung cần giữ; filename xử lý reserved names Windows, ký tự cấm, đường dẫn và trùng tên.

Nghiệm thu: đổi audio gọi 0 lần encode video hình nếu cache còn hợp lệ; đổi một clip không DSP lại các clip khác; shorts phủ toàn video, không mất đuôi hoặc lệch word timing; preview và render dùng cùng mapping.

### Đợt 7 — Tài nguyên Windows, tích hợp cuối và phát hành

Giải quyết review #18; kiểm chứng phần browser/runtime.

- Cleanup chỉ tác động process có ownership được app ghi nhận và session đã kết thúc; kiểm tra PID creation time/parent/session, tránh nhầm PID tái sử dụng. Có chế độ chỉ báo cáo để kiểm thử.
- Đổi priority/CPU budget trên worker nặng, không hạ toàn API hoặc UI. Chẩn đoán runtime dựa trên package/provider và smoke inference thật, không chỉ suy luận từ tên GPU.
- Đánh giá cookie reader như adapter có phạm vi profile/host rõ ràng, không tự quét mọi browser. Test cookie tổng hợp, lỗi giải mã, schema/encryption không hỗ trợ và tính toàn vẹn SQLite; không âm thầm dùng giá trị hỏng hoặc trộn cookie sai domain/path.
- Khi cơ chế cookie không hỗ trợ môi trường, giữ luồng đăng nhập browser đang hoạt động của app; không coi giải mã cookie là bảo đảm đăng nhập hoặc tránh phát hiện bot.
- Chạy browser E2E toàn luồng OCR/ASR → sửa nguồn → dịch → sửa dịch → giọng → mix → render/export/version/resume, cả failure paths.
- Kiểm thử cài mới, dữ liệu cũ, restart giữa job, disk full, cache bị hỏng, video dài, desktop 1024/1366/1440 px.
- Mỗi tính năng mới có trạng thái sẵn sàng/thiếu model/lỗi; chỉ bật mặc định phần đã nghiệm thu. Giữ đường quay lại bản ổn định và migration tương thích.

## 4. Bộ chỉ số nghiệm thu

Các ngưỡng sau là mục tiêu của kế hoạch, chưa phải số đo đạt được.

| Hạng mục | Tiêu chí |
| --- | --- |
| Dữ liệu | 0 lần áp nhầm video hoặc ghi đè chỉnh sửa mới trong bộ race-condition E2E |
| ID/timing dịch | Giữ nguyên 100% ID và timestamp nguồn hợp lệ; reject mọi fixture thiếu/trùng/thừa ID |
| OCR nội dung | 0 câu mất/gộp sai trong fixture kiểm soát; báo CER, tỷ lệ bỏ/gộp và lỗi số/phủ định trên video thật |
| OCR timing | Fixture sạch: biên trong 1 frame quan sát sau refinement; mẫu thật đo median/P95 riêng, không suy ra từ số chữ số timestamp |
| ASR | Báo WER/CER và median/P95 sai số lời nói theo ngôn ngữ; không giảm chất lượng ngoài ngưỡng đã chốt sau baseline |
| Resume | 0 request/inference lặp cho batch đã hoàn thành còn hợp lệ |
| Hủy | UI phản hồi mục tiêu <=1s; worker local mục tiêu thu hồi <=5s, có timeout hữu hạn cho model/HTTP không ngắt ngay được |
| RAM/VRAM | Có giới hạn queue/cache/model; video dài không tăng RAM tuyến tính do giữ mọi frame/PCM; không OOM trên cấu hình được hỗ trợ |
| UI | Mục tiêu >=55 FPS khi kéo vùng/timeline trên máy đo ở 500 cue; thêm stress test 5.000 cue, không render toàn danh sách |
| Render lại | Audio-only edit không encode lại hình; một clip đổi không DSP lại tất cả clip |
| Chất lượng âm | Kiểm tra mono/stereo, thiếu/lặp từ, clipping, đầu/cuối lời nói và nghe đối chiếu |

Đo cold run, warm run, đổi một cue và resume sau lỗi riêng. Mỗi cấu hình hiệu năng chạy nhiều lần có cùng đầu vào, ghi median/P95 và tài nguyên. Nếu thay batch/precision/model làm waveform khác, phải đánh giá chất lượng thay vì chỉ so checksum.

## 5. Quản lý phạm vi và các mốc bàn giao

- Mốc A: đợt 0–1, nền dữ liệu/job/editor an toàn và dependency cài mới đúng.
- Mốc B: đợt 2–4, hoàn chỉnh OCR/ASR → sửa nguồn → dịch có resume, đủ điều kiện dùng chính.
- Mốc C: đợt 5–6, nâng chất lượng giọng/nền và giảm chi phí render lại; các tiện ích có call site thực.
- Mốc D: đợt 7, ổn định runtime, E2E, benchmark và nghiệm thu cài mới.
- Mỗi đợt có commit/PR nhỏ theo hạng mục, test liên quan, bằng chứng đo và danh sách giới hạn còn lại. Không gộp sửa correctness với thay model/precision khiến khó xác định nguyên nhân hồi quy.
- Ưu tiên tối ưu cache, xử lý tăng dần và tài nguyên dùng chung trước worker TTS sống lâu. Chỉ đầu tư worker dài hạn nếu profiling sau cache vẫn cho thấy thời gian nạp model là nút thắt và có thiết kế reset voice/reference/cancel rõ ràng.
- Trong đợt 0, mới ước lượng thời gian triển khai từ số fixture, thiết bị và điều kiện API thực tế. Không đặt lịch chắc chắn dựa trên số file hoặc số unit test.

Bước bắt đầu khi triển khai: viết test tái hiện ghi đè video/chỉnh sửa và source_text không cập nhật, sửa vòng đời dữ liệu/job, rồi hoàn thiện OCR trước khi mở rộng các module phụ.

## 6. Thứ tự công việc còn lại sau đối chiếu code ngày 2026-09-15

Đây là kế hoạch tiếp tục từ code đang có, không yêu cầu viết lại các phần đã sửa. Trong lần đối chiếu này, 22 test dịch và regression chạy lại đều qua; chưa chạy lại toàn bộ ứng dụng. Các module bổ sung có file và unit test nhưng chưa có nơi gọi từ luồng app vẫn được tính là chưa tích hợp.

| Thứ tự | Công việc cụ thể | Khu vực code chính | Điều kiện bàn giao |
| --- | --- | --- | --- |
| 1 | Chốt an toàn dữ liệu: kiểm tra revision nguồn/dịch qua mọi thao tác, phục hồi draft/phiên bản cũ, job kết thúc sau đổi video hoặc sửa tài liệu; chạy E2E toàn Studio. | `SubtitleStudio.tsx`, `useExtractionJobs.ts`, `source-document.ts`, `draft.ts`, `api/subtitles.py` | Không ghi đè sửa mới; nguồn, bản dịch, SRT và phiên bản cùng tham chiếu đúng tài liệu. |
| 2 | Hoàn tất kiểm chứng dịch: API lưu bản dịch dở, UI tiếp tục, restart, phản hồi quá dài, cache hỏng/disk full, dọn deadline khi tạo HTTP client lỗi; chạy hồi quy nhánh Gemini video dùng chung transport. | `subtitle_translate.py`, `gemini_subtitles.py`, `api/subtitles.py`, `SubtitleVersionsPanel.tsx` | Không checkpoint dữ liệu không hợp lệ; batch đã xong được tái sử dụng; lỗi dịch không phá tài liệu đích hoặc luồng Gemini cũ. |
| 3 | Nghiệm thu OCR/ASR trên corpus có nhãn: bổ sung video thật đa ngôn ngữ, lệch audio/video, ROI/letterbox, video dài; đo CPU trước rồi GPU có sẵn. Hoàn thiện trạng thái model và giới hạn cache. | `subtitle_ocr.py`, `subtitle_asr.py`, worker/supervisor, `useAsrRuntime.ts`, `SubtitleOcrBox.tsx` | Có báo cáo CER/WER, lỗi số/phủ định, median/P95 timing, RAM/VRAM và hủy; công bố cấu hình được kiểm chứng. |
| 4 | Sửa normalizer và tách nền trước khi nối UI; thêm lời đọc riêng, preview, job tách stem, chọn nền/mix và speaker → voice assignment. Kiểm chứng khả dụng engine trước khi cho chọn. | `vietnamese_tts_normalizer.py`, `vocal_separator.py`, `speaker_pitch_diarization.py`, `capcut_tts.py`, pipeline voiceover | Đọc đúng giá trị số; audio downmix mono phát được; đổi một lời đọc chỉ tái tạo clip liên quan; luồng UI → job → render hoạt động. |
| 5 | Tối ưu render theo kết quả profiling: chia cache hình/DSP/mix/mux, cache có quota và giữ tài nguyên đang dùng; thống nhất mapping mask giữa preview/export. | Pipeline render/voiceover hiện có, `video_masking.py`, preview | Đổi âm lượng/voice không encode lại hình; sửa một clip không DSP lại các clip khác; đầu ra giữ chất lượng baseline. |
| 6 | Hoàn thiện shorts, thumbnail, caption: bảo toàn đầu/đuôi và word/speech timing, manifest, chọn ứng viên thumbnail, chỉnh caption trước áp dụng, tên file Windows an toàn. | `scene_splitter.py`, `thumbnail_selector.py`, `caption_cleaner.py`, API/UI export | Shorts phủ toàn thời lượng; có SRT/JSON tương ứng; không ghi đè nguồn; người dùng chọn và sửa được kết quả tiện ích. |
| 7 | Chốt runtime Windows và phát hành: cleanup theo ownership, cookie adapter giới hạn profile/host, smoke inference, cài mới/migration, restart/lỗi ổ đĩa và E2E toàn pipeline. | `system_hygiene.py`, `owned_process.py`, `chromium_cookie_reader.py`, cấu hình cài đặt và test E2E | Không tác động process ngoài app; tính năng thiếu dependency báo đúng trạng thái; vượt toàn bộ tiêu chí phát hành ở đợt 7. |

Thứ tự 1–3 đóng các phần còn thiếu của đợt 0–4; sau đó triển khai đợt 5, 6 và 7. Việc ưu tiên kiểm chứng dịch trước benchmark mở rộng OCR/ASR nhằm khép thay đổi transport đang có, không bỏ qua tiêu chí chất lượng extraction.

Mỗi hạng mục thực hiện theo vòng: tái hiện lỗi → sửa phạm vi nhỏ → kiểm thử hành vi → đo trước/sau nếu tối ưu → cập nhật bằng chứng và giới hạn. Chỉ đánh dấu hoàn tất khi vượt điều kiện bàn giao; số file mới hoặc tổng số test pass không thay cho kiểm thử tính năng từ UI.

### Chính sách tối ưu

- Đo riêng cold run, warm run, sửa một cue và tiếp tục sau lỗi. Chọn nút thắt từ thời gian thực đo, số lần inference/encode/request và peak bộ nhớ.
- Ưu tiên bỏ xử lý lặp, cache đúng khóa, cập nhật từng phần và giới hạn bộ nhớ trước tăng concurrency.
- Chỉ bật batch GPU, đổi precision hoặc song song dịch sau đối chiếu chất lượng cùng đầu vào; giữ cấu hình ổn định để quay lại khi hồi quy.
- Các tuyên bố tăng tốc 5×/10×, giữ toàn bộ SFX hoặc tăng CTR không phải tiêu chí đã đạt. Đánh giá âm nền bằng nghe đối chiếu; thumbnail cung cấp lựa chọn dựa trên chất lượng hình có thể đo.
- Không đưa CapCut ASR/TTS thành engine sẵn sàng chỉ dựa vào ping, tên giọng hoặc placeholder. Khi chưa kiểm chứng thành công, UI cần trạng thái chưa khả dụng và giữ engine hiện có.
