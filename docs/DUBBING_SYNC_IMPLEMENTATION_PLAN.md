# Kế hoạch tối ưu độ khớp video, phụ đề và lồng tiếng

Ngày lập: 13-09-2026. Cập nhật 14-09-2026: đã nối phần code các giai đoạn vào luồng chạy và hoàn tất đợt kiểm tra kỹ thuật để bàn giao. Theo yêu cầu mới, người dùng tự nghe/xem nghiệm thu; xem [hướng dẫn thử và giới hạn](DUBBING_SYNC_USER_CHECK.md) và [tiến độ, bằng chứng](DUBBING_SYNC_IMPLEMENTATION_PROGRESS.md). Chưa xác nhận chất lượng thực hoặc P95 ≤100 ms.

Cơ sở: [nghiên cứu và phản biện đã đối chiếu](DUBBING_SYNC_DEEP_RESEARCH.md), [đo cân bằng phần cứng](VOICEOVER_HARDWARE_BALANCE.md). Phạm vi giữ nguyên video gốc trên i5-13420H / RTX 4050 Laptop 6 GB, giữ VieNeu hiện tại. Các ngưỡng dưới đây là cấu hình pilot để đo, không phải cam kết độ chính xác hoặc chất lượng.

## 1. Kết quả cần đạt

- Phụ đề dịch bám chữ gốc nếu video có phụ đề; mỗi cue là một câu hoặc vế, có thể tách ở dấu phẩy.
- Giọng mới bám nội dung và lượt nói nguồn, không dồn mốc câu sau để bù câu trước.
- Giữ riêng thời gian hiển thị chữ, vùng lời nói nguồn và vùng lời nói mới. Không kéo ba loại mốc thành một nếu chúng khác nhau hợp lệ.
- Giữ bản chữ hiển thị và lời đọc riêng; điều chỉnh diễn đạt không được làm mất nghĩa hoặc sắc thái thiết yếu.
- Dùng lại audio đạt, chỉ xử lý vùng cần sửa; không yêu cầu người dùng điền tên hay quan hệ nhân vật.
- Các vùng đủ bằng chứng được tự kiểm tra/sửa trong job đã khởi chạy. Vùng không chắc hoặc hết ngân sách được lưu lại cùng lý do; không sinh lời để lấp mọi khoảng trống.

Không đặt mục tiêu khớp khẩu hình tuyệt đối hoặc thay đổi tốc độ video. Mục tiêu đo thử là P95 sai số âm đầu và âm cuối riêng biệt ≤100 ms trên các vùng nguồn rõ; đồng thời công bố độ phủ vùng đã xác minh. Giữ đủ nghĩa và giọng tự nhiên là điều kiện đạt, không hy sinh để lấy một con số timing đẹp.

## 2. Hợp đồng dữ liệu và cách tính

Giữ `cue.text` làm chữ hiển thị. `clip.spoken_text` là lời đọc, `clip.source_text` là bản chữ đầu vào đã lưu để theo dõi thay đổi; trường này không mặc nhiên chứa nguyên văn tiếng Trung. Tái sử dụng `source_cue_ids` và các trường speech timing của phụ đề khi bằng chứng hợp lệ.

Thông tin mới cần thiết kế trong schema có phiên bản:

| Nhóm | Dữ liệu cần lưu | Quy tắc |
|---|---|---|
| Mốc nguồn | Khoảng nói, khoảng nghỉ, phương pháp, nguồn bằng chứng và phiên bản | Không biến `energy_estimated` hoặc `interpolated` thành mốc từ đã đo |
| Vùng được phát | Biên trái/phải, khoảng lặng được mượn, giới hạn ngữ cảnh | Độc lập với ô chữ; không vượt lời tiếp theo hoặc cuối video |
| Chỉnh sửa | Nguồn chỉnh `manual/automatic/legacy_unknown`, khóa mốc/chữ | Dữ liệu cũ không rõ nguồn được bảo toàn |
| Audio | Thời lượng file, biên tiếng, trim, tempo, thời lượng sau xử lý | Phân biệt ước lượng trước render với đo waveform thật |
| Độ khớp | `unverified`, `aligned`, `needs_review`, mã lỗi và bằng chứng | Tách khỏi trạng thái đã tạo WAV; `ready` không có nghĩa đã khớp nguồn |
| Sửa tự động | ID cụm, input hash, ứng viên, số lần thử, phiên bản kết quả | Không áp kết quả lên chữ/audio đã bị chỉnh trong lúc job chạy |

Đề xuất dùng schema v2 cho metadata mới; reader đọc được v1 và v2, writer mới ghi v2. Backend đang `extra="forbid"` và khóa `schema_version=1`, nên phải cập nhật model, API và frontend cùng đợt. Tạo snapshot v1 trước migration; rollback bằng snapshot và WAV gốc, không trông chờ bản ứng dụng cũ đọc được v2. Tách số phiên bản schema khỏi revision của mỗi lần chỉnh dự án.

Với file đã xử lý:

```text
file_start = mốc đặt file trên timeline
file_end = file_start + measured_output_duration
speech_start = file_start + measured_speech_head
speech_end = file_start + measured_speech_tail
```

Kiểm tra vùng tiếng với vùng được phép và kiểm tra khoảng file với khả năng phát/ghép audio hiện có. Hai file có phần đệm chồng nhau vẫn có thể làm preview hiện tại ngắt tiếng; không bỏ qua chỉ vì vùng tiếng không chồng. Nếu chưa đo biên tiếng, dùng khoảng file bảo thủ và giữ nhãn chưa xác minh.

Mọi phép kiểm tra phải tính offset; quét chồng bằng mốc kết thúc xa nhất đang có hoặc tập khoảng hoạt động, không chỉ so với đoạn ngay trước. Chồng lời thật cần trạng thái riêng; giai đoạn đầu không tự giải bằng đẩy câu hay cắt mất một giọng.

## 3. Các giai đoạn triển khai

### Giai đoạn 0 — chốt mẫu và khả năng phục hồi

1. Chụp revision/checksum mới khi triển khai, sao lưu tài liệu và liên kết asset. Không coi audit revision 540 là dữ liệu hiện tại mãi mãi.
2. Quét metadata toàn dự án, phân loại vượt ô chữ, vượt vùng nói đã xác minh, chồng file, chồng tiếng, offset chưa rõ và thiếu WAV.
3. Chọn khoảng 40 cụm đại diện ở đầu/giữa/cuối: câu dài, câu ngắn, dấu phẩy, chữ hiện lệch lời, khoảng nghỉ, nhạc, chồng tiếng và cảnh cắt. Bao gồm ví dụ 2,4 s trong ô 1,3 s.
4. Lưu mẫu nghe trước sửa và mốc kiểm chứng từ video/audio; biên mơ hồ có cờ riêng. Mốc tự động chưa thể thay thế bộ mẫu kiểm chứng.

Đầu ra: baseline có phiên bản, tập mẫu và danh sách lỗi. Chưa sinh lại giọng hàng loạt.

### Giai đoạn 1 — sửa tính đúng timeline và trạng thái

1. Thực hiện schema/migration ở mục 2. Giữ nguyên rate, offset và lời đọc cũ khi chưa có bằng chứng sửa.
2. Thống nhất phép tính thời gian backend/frontend bằng cùng bộ fixture; backend kiểm tra lại trước lưu/xuất.
3. Thay đường tự “smart resolve” bằng bộ kiểm tra có giới hạn; bỏ ripple đẩy dây chuyền khỏi quy trình khớp nguồn và bỏ auto-fit tới 2× khỏi mặc định tự động. Không reset toàn bộ offset hay âm thầm sửa tốc độ người dùng đặt.
4. Tính lại trạng thái khi đổi offset, rate, mốc, nội dung hoặc asset. Phân biệt đã có giọng với đã kiểm chứng khớp.
5. Hiển thị lỗi theo vùng: lệch đầu, vượt cuối, chồng, thiếu bằng chứng, cần rút gọn. Preview không được âm thầm chọn câu sau rồi làm mất câu trước; vùng chồng chưa hỗ trợ phải được báo rõ.

Mã chính: `frontend/src/voiceover/{planner,types,playbackIndex,playbackEngine}.ts`, `VoiceTrack.tsx`, `VoicePanel.tsx`; `backend/app/services/voiceover/{models,store,mix}.py`. Có thể tách module timing để tránh sao chép công thức rải rác.

Điều kiện hoàn tất: fixture offset +157 ms của ví dụ phải phát hiện vượt ô khoảng 154 ms; khoảng chồng lồng nhau không bị bỏ sót; không câu nào bị dịch do câu trước; chỉnh tay và lời đọc tùy biến giữ nguyên qua migration/undo.

### Giai đoạn 2 — xác định vùng lời nói và khoảng lặng được dùng

1. Kiểm tra nguồn gốc `speech_start_ms/speech_end_ms`; chỉ ưu tiên khi đủ bằng chứng. Giữ thời gian phụ đề gốc cho lớp chữ.
2. Dùng cache VAD phù hợp hoặc phân tích cửa sổ audio ngắn để xác định vùng nói/nghỉ. Khoảng thiếu phụ đề nhưng có lời thành ứng viên thiếu nội dung; khoảng im hợp lệ được giữ.
3. Chỉ chạy ASR/căn transcript trên cửa sổ nghi vấn. Căn transcript nguồn vào audio cùng ngôn ngữ; không căn chữ Việt trực tiếp vào tiếng Trung.
4. Đo khoảng yên đầu/cuối WAV TTS theo hai bước: năng lượng để khoanh, kiểm tra bổ sung khi có nguy cơ cắt phụ âm/hơi thở. Không cắt theo một ngưỡng năng lượng duy nhất rồi gọi đó là biên lời chắc chắn.
5. Tạo vùng phát được phép từ các mốc đã xác minh. Nếu chữ và tiếng xung đột, giữ bằng chứng cả hai và báo xung đột.

Mã chính: `backend/app/services/subtitle_alignment.py`, lớp timing mới và `voiceover/audio.py`; frontend giữ metadata nguồn thay vì tự ghi đè. Sửa nhãn phương pháp căn hiện có trước khi dùng làm điều kiện tự cắt.

Điều kiện hoàn tất: không đánh dấu khoảng trống chỉ vì thiếu cue; cache mất hiệu lực khi thay video/audio; vùng thiếu bằng chứng không được tự cắt hoặc gắn nhãn đạt 100 ms.

### Giai đoạn 3 — fit nhẹ trong vùng nguồn cố định

Thứ tự tìm phương án cho từng cụm:

1. Giữ WAV và mốc đã đạt.
2. Loại đệm đầu/cuối đã xác minh, giữ biên bảo vệ.
3. Tính các phương án tempo nhẹ và mượn khoảng nghỉ, chọn phương án giữ nhịp và sắc thái tốt nhất trong vùng cho phép. Đây là lựa chọn kết hợp, không bắt tăng tốc hết trần rồi mới được mượn khoảng nghỉ.
4. Giữ nguyên đầu câu tiếp theo. Kiểm tra lại audio sau xử lý và trạng thái toàn vùng lân cận.
5. Đánh dấu nguyên nhân chưa giải quyết nếu không có phương án đạt; chuyển riêng cụm đó sang giai đoạn 5.

| Thông số pilot | Giá trị thử | Điều kiện |
|---|---|---|
| Tempo tự động | 0,95–1,15× | Không làm chậm chỉ để lấp ô; ưu tiên gần 1× |
| Tempo mở rộng | >1,15–1,20× | Chỉ dùng sau đánh giá mẫu; không tự coi vừa ô là đạt giọng |
| Mượn khoảng lặng | Tối đa 250 ms | Lấy min của trần, khoảng nghỉ còn dùng được và giới hạn ngữ cảnh |
| Biên bảo vệ | Theo độ chắc chắn mốc | Không tự đặt 0; biên chưa chắc thì không mượn |

Ví dụ: câu nguồn kết thúc 5,0 s, câu kế bắt đầu 5,5 s; có thể cho giọng đầu kết thúc 5,2 s nếu khoảng nghỉ cho phép, trong khi câu kế vẫn bắt đầu 5,5 s. Không đổi thời gian phụ đề chỉ vì giọng dùng thêm khoảng nghỉ.

Điều kiện hoàn tất: thao tác lặp lại không cộng dồn offset; không vượt cuối video; không xóa khoảng nghỉ mang nghĩa; câu quá ngắn tự nhiên được giữ.

### Giai đoạn 4 — nghe kiểm tra và xuất thống nhất, cache có giới hạn

1. Giữ preview nhanh bằng trình duyệt. Thêm nghe kiểm tra bằng WAV đã trim/tempo, phát ở rate đoạn 1×; không xử lý tempo lần hai.
2. Tạo asset theo vùng cần nghe, gom các thay đổi kéo liên tiếp, hủy yêu cầu lỗi thời. Giới hạn một worker xử lý audio trong pilot để đo tải.
3. Cache theo WAV gốc, thông số xử lý, phiên bản filter và gain/fade nếu đã bake; giữ asset đang dùng cho export, loại asset ít dùng theo hạn mức. Di chuyển file trên timeline không cần tạo lại waveform nếu nội dung xử lý không đổi.
4. Export khóa revision, dùng lại cache hợp lệ, tạo phần thiếu và đo thời lượng thực. Cùng time map cho chữ/tiếng khi ứng dụng có phép dựng khác, dù phạm vi pilot giữ video gốc.
5. Đo thời gian khởi tạo FFmpeg hiện gọi từng clip. Chỉ chọn xử lý nhóm hoặc thư viện thường trực khi benchmark tốt hơn với cùng chất lượng; không mặc định lệnh 1.500 đầu vào là giải pháp.

Mã chính: `voiceover/mix.py`, `audio.py`, cache audio backend; `playbackEngine.ts`, `audioAssetCache.ts`, `useVoicePlayback.ts`.

Điều kiện hoàn tất: nghe kiểm tra và xuất dùng cùng waveform/cấu hình; seek/pause/resume không mất cuối câu; cache có hạn mức; thay nhanh nhiều lần không render mọi trạng thái trung gian.

### Giai đoạn 5 — tự sửa nội dung và câu ngắn có ngân sách

1. Chỉ đưa cụm còn lỗi vào hàng đợi sau các bước rẻ. Khi người dùng khởi chạy tạo/sửa giọng, các bước kiểm tra và sửa đủ bằng chứng tự nối tiếp; không cần bấm “tạo phần còn thiếu” cho mỗi lượt. Mở dự án hoặc chỉ xem timeline không tự phát sinh TTS/API nặng.
2. Lỗi sinh thiếu/lặp từ: thử lại cụm, giữ nguyên bản chữ. Lỗi diễn đạt quá dài: Gemini nhận chữ nguồn, bản dịch, ngữ cảnh lân cận và budget đã đo; trả tối đa hai ứng viên có mapping. Không cho model tự đổi timestamp để qua kiểm tra.
3. Gom các cụm độc lập vào request nhỏ có ID riêng; ước lượng độ dài theo giọng để loại lựa chọn rõ ràng không khả thi trước TTS. Không áp bản rút gọn nếu mất phủ định, số, ý, sắc thái hoặc làm sai xưng hô vốn có.
4. Giữ `cue.text`; lưu phiên bản `spoken_text` mới cùng audio khi đạt. Không sửa lời tùy biến/chỉnh tay tự động. Nếu người dùng sửa trong lúc chờ, loại kết quả cũ theo hash/revision.
5. Câu ngắn tự nhiên không phải lỗi. Với nhiều vế liên tục cùng lượt nói bị vụn, thử một WAV chung có mapping nhiều cue; căn tiếng Việt trước khi tự áp dụng, chỉ tách waveform tại biên an toàn. Không gộp các lượt đối đáp khác người.
6. Checkpoint lưu cả kết quả và ngân sách; pause/cancel dừng cây tiến trình. Lỗi API tạm thời thử lại có backoff và trần; không lặp vô hạn hoặc coi hết thời gian chờ là đã thành công.

Ngân sách pilot đề xuất: tối đa hai TTS bổ sung mỗi cụm; với N cụm được đánh giá trong job, tổng lượt TTS bổ sung không quá `min(40, max(2, ceil(0.1*N)))`. Giới hạn tám request Gemini kể cả retry, tối đa tám cụm/request và mười phút thời gian pha sửa. Mỗi ứng viên trong batch tính một lượt TTS; dừng khi chạm bất kỳ trần nào, kể cả đang còn cụm lỗi. Đây là điểm xuất phát để đo, có thể chưa xử lý hết lỗi; chỉ nâng sau khi biết hiệu quả và chi phí. Timeout phải hủy công việc đang chạy, không chỉ ngừng lấy job mới.

Mã chính: `voiceover/manager.py`, module sửa có giới hạn, các tiện ích Gemini hiện có và `runtimes/voiceover/worker.py` khi cần sinh cụm. Khóa GPU chung giữa TTS và aligner; profile TTS hiện có không tự kiểm soát được aligner mới.

Điều kiện hoàn tất: số lượt thực không vượt ngân sách qua restart; không ghi đè sửa tay; câu sau giữ mốc; thất bại giữ bản trước và có lý do; kết quả chỉ đủ thời lượng nhưng thiếu nghĩa không được chấp nhận.

## 4. Kiểm chứng và tải phần cứng

Giữ cấu hình đã đo: CPU 3 luồng; GPU batch 4 và 2 luồng CPU hỗ trợ. Không chạy TTS và aligner GPU đồng thời trong pilot. Số liệu cũ chỉ áp dụng cho TTS; phải đo chi phí pipeline mới riêng.

| Kiểm tra | Cách thực hiện | Điều kiện chấp nhận |
|---|---|---|
| Logic thời gian | Fixture backend/frontend cho offset ±, khoảng lồng nhau, cuối video, thiếu mốc | Kết quả nhất quán; không trôi dây chuyền |
| Tương thích dữ liệu | V1→V2, snapshot/rollback, sửa tay, source cue bị tách/xóa | Không mất chữ, giọng, liên kết hoặc chỉnh sửa |
| Đồng bộ nghe | Cùng khoảng 40 cụm trước/sau; thêm tập khác trước bật mặc định | Sai số đầu/cuối riêng, median/P95 và tỷ lệ chưa xác minh |
| Nội dung và giọng | Nghe so sánh ẩn tên phương án, kiểm tra vùng ASR nghi vấn | Không mất nghĩa/sắc thái, không cắt âm, không đọc dồn để vừa ô |
| Câu ngắn | So riêng câu tự nhiên, thiếu âm và nhiều vế liên tục | Không gộp sai lượt, mapping chữ/tiếng đúng |
| Preview/export | Mẫu tích hợp seek, đổi tốc độ, cache miss/hit, thời lượng thực | Nghe kiểm tra khớp audio xuất; preview nhanh được phân biệt rõ |
| Vòng đời | Pause/cancel/restart, API 503, sửa trong lúc chờ, hết budget | Không worker sót, không thử vô hạn, không áp kết quả lỗi thời |
| Hiệu năng | Wall time, CPU process time, RAM/VRAM đỉnh, I/O, cache hit, độ mượt UI | Công bố chi phí tăng thêm và số cụm sửa thành công; không chỉ % GPU |

Dùng các test sẵn có ở `frontend/src/voiceover/*.test.ts`, `backend/tests/test_voiceover*.py`, `test_voice_lifecycle.py`, `test_voice_resources.py`; bổ sung fixture cho hành vi mới. Chạy kiểm tra kiểu/lint và test tích hợp audio nhỏ khi triển khai, trước thử đoạn video dài. Không chạy benchmark TTS hoặc suite ứng dụng chỉ để xác nhận tài liệu kế hoạch.

## 5. Thứ tự bàn giao và quyết định sau pilot

**Bàn giao A: giai đoạn 0–1.** Sửa các bất nhất chắc chắn trong tính toán và bảo vệ dữ liệu. Chưa tuyên bố khớp nguồn khi chưa có mốc kiểm chứng. Đây là phần nên triển khai trước.

**Bàn giao B: giai đoạn 2–4.** Chốt vùng nói, mượn nghỉ có kiểm soát, tempo nhẹ, nghe kiểm tra/xuất thống nhất. Chạy tập mẫu và báo số cụm được cải thiện bằng xử lý rẻ.

**Bàn giao C: giai đoạn 5.** Bật thử tự sửa có giới hạn trên bản sao dự án; so phần tăng độ khớp với thời gian/API/TTS tăng thêm. Sinh cụm được đánh giá sớm trong tập câu ngắn, nhưng chỉ bật tự động sau khi mapping đạt.

Nếu phần lớn lỗi còn lại do mốc nguồn, ưu tiên cải thiện căn nguồn. Nếu do lời dịch quá dài, hiệu chỉnh bước dịch theo ngân sách nói và giữ nghĩa. Chỉ đánh giá engine điều khiển duration sau khi có bằng chứng VieNeu hiện tại vẫn không đạt với pipeline đã sửa; không đổi engine chỉ vì WAV ban đầu không vừa ô chữ.

Mỗi đợt giữ snapshot, cấu hình và số đo để có thể khôi phục. Chỉ áp lên toàn dự án sau khi pilot đạt tiêu chí dữ liệu, nội dung và tài nguyên. Không hứa “khớp tuyệt đối”; bàn giao phải chỉ rõ phần đã đo đạt và phần còn cần xem lại.
