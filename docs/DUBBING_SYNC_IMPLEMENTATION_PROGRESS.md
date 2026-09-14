# Tiến độ triển khai đồng bộ lời nói

Cập nhật 14-09-2026. Theo yêu cầu mới **“hoàn thành nốt phần code đi tôi sẽ kiểm chứng”**, đã nối code tự kiểm tra/sửa vào luồng tạo giọng; người dùng thực hiện nghiệm thu nghe/xem. [Hướng dẫn thử và giới hạn](DUBBING_SYNC_USER_CHECK.md). Chưa xác nhận độ khớp P95 ≤100 ms. Các mục bên dưới ghi tiến độ theo từng đợt; phần cuối cập nhật những mục trước đó còn thiếu.

## Bàn giao code vòng tự sửa — cập nhật cuối 14-09-2026

Theo yêu cầu **“hoàn thành nốt phần code đi tôi sẽ kiểm chứng”**, phần nghe/xem mẫu và nghiệm thu P95 do người dùng thực hiện. Các mục lịch sử bên dưới ghi “chưa nối”, “chưa tự mượn”, “còn xử lý nhóm” đã được bổ sung như sau; code/test đạt chưa chứng minh chất lượng trên video thật.

- Bộ đếm lưu trước HTTP/TTS, không hoàn lượt thất bại, tối đa hai TTS/cụm và giới hạn tổng theo N; tám request Gemini tính cả retry/đối chiếu/ngữ cảnh; mười phút pha sửa tính từ lần đầu. Hủy dừng request đang chờ. Không thử vòng qua chặn nội dung của nhà cung cấp.
- Chỉ sửa vùng còn nguyên nhân đủ bằng chứng. WAV thiếu/sai lời thử lại nguyên văn; lời quá dài nhận tối đa hai phương án rồi đối chiếu nghĩa riêng. Các cờ do lượt sinh tự khai không đủ để áp. Khóa chữ/mốc, chữ nguồn và checksum WAV cũ được kiểm lại khi áp.
- Một worker sinh batch bằng cấu hình máy hiện tại, mỗi ứng viên có ID riêng để không lấy nhầm cache lỗi hoặc đè WAV cũ. WAV mới được đo transcript/biên tiếng, fit và đo lại WAV xử lý. Checkpoint giữ ứng viên, số lượt và kết quả; tiếp tục đo ứng viên đã sinh thay vì tạo lại. Khôi phục cả khi worker đã ghi results nhưng pipeline chưa ghi checkpoint. Hủy/hết hạn/sửa dự án dừng cây worker; giữ bản trước.
- Mượn nghỉ: proxy ngữ cảnh vùng nguồn ≤12 giây/4 MiB, cache theo nguồn và nội dung. Không suy tên/quan hệ nhân vật. Chỉ mượn tối đa 250 ms khi ngữ cảnh không mất nhịp/ý, không chuyển cảnh/người, VAD không có lời và PCM yên dưới bảo vệ peak; guard 80 ms. Nếu đủ khớp thì không rút lời hoặc sinh TTS. Tối đa hai lần thử ngữ cảnh mượn nghỉ/lượt để dành budget sửa lời.
- Ghép vế: khoanh hai vế ngắn liền nhau sau dấu phẩy/chấm phẩy/hai chấm; chỉ sinh chung khi quan sát video xác nhận cùng lượt nói bị vụn. ASR Việt ánh xạ trọn từng vế, không có từ dư/từ vắt qua biên; khoảng giữa ≥160 ms và 20 ms quanh điểm cắt phải yên. WAV từng vế được đo lại, căn vào nguồn riêng; kiểm tra tổng vùng và áp cả nhóm. Một WAV nhóm tính lượt cho mọi thành viên.
- API tạo giọng mang phụ đề nguồn hiện tại. Sau lượt tạo, kể cả lỗi một phần, tự nối kiểm tra nguồn → mượn nghỉ/ghép vế có điều kiện → sửa chữ/TTS → đo lại → áp đoạn đạt trong cùng tác vụ. Source audit trần 10 phút riêng; pha sửa trần 10 phút riêng. Không khởi chạy khi mở dự án. Tạm dừng dừng worker ngay; resume giữ session/budget. Dùng lại asset đã căn ở 1×, không quay về WAV thô.
- Giao diện hiển thị tiến độ pha căn, có khóa lời đọc riêng, mở kết quả khi tác vụ kết thúc. Sửa chữ nguồn hoặc mốc/chữ giọng chưa lưu hủy lượt căn cũ. Kết quả áp tự động vào lịch sử hoàn tác trong phiên. Đoạn chưa có WAV đạt vẫn báo thiếu/thất bại.

**Kiểm tra kỹ thuật:** đợt tổng hợp 161 test backend đạt (44,44 giây), gồm queue/budget/network, lifecycle, source context, pause/group, worker thật và tiến trình con, apply/undo, timeline và render API. Sau bổ sung phục hồi checkpoint cuối, 9 test pipeline/TTS đạt (2,54 giây). Frontend 34 test/7 file đạt; TypeScript, Ruff và ESLint đạt. Harness Edge trên panel/hook thật đạt: gửi nguồn hiện tại khi tạo, tiến độ pha căn, hủy khi sửa chưa lưu, audio 1×, undo và bảo vệ chồng lời. API/ASR/TTS trong các kiểm tra được giả lập; WAV/FFmpeg, tiến trình và giao diện dùng thật. Không chạy thêm TTS/Gemini thật hoặc sửa dự án gốc trong đợt bàn giao này.

Hướng dẫn thử: [DUBBING_SYNC_USER_CHECK.md](DUBBING_SYNC_USER_CHECK.md). Người dùng kiểm chứng đủ nghĩa/sắc thái, không cắt âm/đọc dồn, ghép đúng lượt, độ khớp nguồn, tải máy và preview so với xuất.

## Lịch sử triển khai và kiểm chứng

### Nền dữ liệu và timeline — giai đoạn 0–1

- Snapshot revision 540, SHA-256 và manifest WAV gốc, 40 WAV mẫu cùng trang nghe đã lưu ở `artifacts/voiceover/sync-implementation/baseline-20260913`. Script tái lập: `backend/scripts/audit_dubbing_sync.py DOCUMENT NEW_OUTPUT_DIR`. Script không ghi lên tài liệu nguồn.
- Reader nhận v1/v2; lần ghi đầu có snapshot v1 nguyên byte. Metadata `sync` phân biệt chỉnh tay/tự động/không rõ nguồn, khóa mốc/chữ và trạng thái kiểm chứng. Writer không chấp nhận client v1 ghi đè metadata dự án đã nâng v2.
- Kiểm tra offset ở frontend, attach, save và export; quét chồng lồng nhau; tách trạng thái có WAV khỏi trạng thái đã xác minh nguồn.
- Fit tự động tối đa 1,15× cho đoạn tự động chưa khóa, giữ offset và đầu câu sau. Không kéo chậm để lấp ô. Nút đẩy dây chuyền đã bỏ khỏi giao diện tự sửa.
- Preview dừng trước vùng audio chồng, có thông báo; không âm thầm bỏ cuối câu trước. Đây chưa phải hỗ trợ phát chồng nhiều người nói.
- Kiểm tra Edge headless của panel/hook thật, khóa mốc, fit, undo và guard playback: `backend/scripts/check_voice_sync_ui.py`; artifact `stage1-ui.json` và `stage1-ui.png`.
- Trong lúc triển khai, dự án đang mở bổ sung 279 WAV và lên revision 622. So với snapshot, toàn bộ chữ/mốc/offset/rate/gain cũ và asset cũ được giữ; `stage1-preservation.json` ghi kiểm chứng. Không khôi phục revision cũ lên dữ liệu mới.
- Đo parse + migration trong bộ nhớ + audit 1.000 clip: trung vị 15,25 ms / 10 lần; không gồm xử lý audio/model. Artifact `stage1-measurement.json`.

### Nguồn mốc và phân tích rẻ — phần đầu giai đoạn 2

- `speech_evidence` ghi phương pháp, định danh audio, hash transcript, biên nói và phiên bản thuật toán. Word timings phân biệt `asr_observed`, `energy_estimated`, `interpolated`; không suy độ chính xác từ lưới 10 ms. Nhãn `forced_alignment` cũ được giữ để tương thích, nhưng không đủ làm bằng chứng căn âm học.
- `valid_speech_evidence` kiểm tra hash transcript/biên/audio; metadata nhập từ ngoài chỉ là khai báo nguồn gốc, không tự cấp quyền trim/mượn khoảng nghỉ. Mốc cũ thiếu bằng chứng không được nâng thành đã xác minh.
- Khi căn cue screen/mixed, giữ thời gian hiển thị chữ gốc và ghi riêng biên nói. API có `preserve_display` để caller yêu cầu điều tương tự cho cue audio. Schema/parser/draft giữ khoảng lời nói hợp lệ nằm ngoài khoảng hiển thị; manual edit/split xóa bằng chứng cũ. Cache alignment đã nâng phiên bản.
- API đọc `GET /api/v1/voiceover/assets/{id}/quiet-analysis` đo RMS hai ngưỡng, bảo vệ tín hiệu nhỏ bằng peak và cache theo checksum/thuật toán, có kiểm tra owner. Không nạp model; không tự cắt audio.
- `voiceover/source_activity.py` quan sát các cửa sổ ≤30 s với ngân sách tổng mặc định 180 s, dùng một Silero CPU model, cache theo audio identity/stat/window/model. Khoảng thiếu cue được phân biệt có lời nghi vấn, yên nghi vấn và chưa quan sát; không tự sinh phụ đề hoặc cho mượn khoảng nghỉ chỉ bằng VAD.

## Đo pilot thực trên máy

| Phép đo | Phạm vi | Kết quả | Giới hạn |
|---|---|---|---|
| Đệm WAV | 40 WAV mẫu | Trung vị tổng đầu/cuối: 390 ms ở −50 dBFS; 405 ms ở −40 dBFS | Phần yên theo năng lượng, không phải lượng chắc chắn được cắt |
| Chi phí đo đệm | 40 WAV, lần đầu | Wall 2,376 s; CPU process 0,234 s | Không bao gồm TTS; không GPU |
| Quan sát lời nguồn | 40 cửa sổ, tổng 100,52 s audio | Wall 4,382 s; CPU process 0,828 s; không bỏ cửa sổ do budget | Không đo nội dung, âm đầu/âm cuối chuẩn hoặc danh tính người nói |

Artifact: `stage2-quiet-measurements.json`, `stage2-source-activity.json`, cache trong `artifacts/voiceover/sync-implementation`. CPU process time chỉ của Python; thời gian CPU của FFmpeg con chưa được cộng vào. Không dùng các số này để dự báo toàn video hoặc hứa GPU luôn 0% của cả máy.

## Phần tiếp theo còn phải làm

### Bổ sung: job kiểm tra nguồn và sửa lỗi preview khi đổi video

- Đã nối job kiểm tra nguồn vào API và panel giọng: tối đa 40 đoạn/lượt, ngân sách cửa sổ nguồn 180 giây, lưu bản kết quả theo input và revision, kiểm tra owner, hỗ trợ yêu cầu hủy. ASR cấu hình CPU 3 luồng/int8, chỉ dùng model đã cài. Kết quả hiện là đề xuất, chưa tự áp mốc hoặc cắt audio.
- Phát hiện dự án bị sửa trong lúc job chạy; mapping nhiều clip dùng chung một cue không được tự suy mốc. API hiện chạy phân tích trong tiến trình CPU riêng, supervisor dừng cây tiến trình khi hủy/hết 10 phút và giữ checkpoint. Watcher dừng worker nếu tiến trình API thoát; đã kiểm tra bằng worker và tiến trình con thật bị treo. Dùng Windows process handle để tránh khóa stdin làm kẹt lúc nạp NumPy.
- Whisper offline tái sử dụng cache đã cài trong thư mục người dùng nếu cache dự án chưa có model; không sao chép/tải thêm. Pilot bốn cửa sổ trên bản sao revision 623 mất 83,408 giây, 0/4 cue đủ điều kiện xác minh nguồn, không đổi tài liệu/WAV gốc. Artifact `stage2-asr-pilot-20260913/measurement.json`. Kết quả này chưa đạt để áp dụng tự động; đang bổ sung quan sát ASR và lý do ghép không đủ để xác định bước cải thiện tiếp theo.
- Chẩn đoán cho thấy prompt ASR chứa chính câu cần đối chiếu. Đã bỏ gợi sẵn câu; trên cùng bốn mẫu, ASR nhận đủ ký tự nguồn nhưng điểm thấp nhất ở một số từ còn dưới 0,65 nên vẫn giữ trạng thái chưa xác minh. Không giảm ngưỡng chỉ để tăng số đoạn đạt. Artifact `stage2-asr-unprompted-four-20260913/measurement.json`. Lượt này mất 10,802 giây; chưa phải benchmark có kiểm soát tải nền để quy toàn bộ chênh lệch cho thay đổi prompt.
- Cache riêng quan sát ASR theo checksum PCM, cửa sổ, ngôn ngữ, model thực tế và cấu hình nhận dạng; đổi chữ/ID cue chỉ ghép lại transcript. Cache nhận dạng tách phiên bản khỏi thuật toán ghép mốc. Pilot bốn cửa sổ mới: 11,04 giây lần đầu, 1,703 giây khi đổi chữ hiển thị, 4/4 cache hit, kết quả từ giữ nguyên. Artifact `stage2-asr-cache-20260913/measurement.json`.
- Ghép chữ Trung không lấy biên của cả từ khi cue chỉ chiếm một phần từ, không nối chữ qua lời xen giữa và không tự chọn một trong nhiều lần lặp cùng câu. Panel hiển thị lời ASR, mức ghép chữ, lý do chưa đủ bằng chứng; phân loại khoảng trống sau cue bằng quan sát thực, không tự điền hoặc mượn thời gian.
- Thêm `dubbed_speech.py`: kiểm tra transcript Việt của WAV bằng ASR độc lập, đối chiếu RMS/peak và VAD trước khi đề xuất bỏ đệm, giữ biên 80 ms. Chỉ chạy trong worker được giám sát, trên đoạn tự động chưa khóa có nguồn hợp lệ; tối đa 4 WAV / 60 giây audio đầu vào trong lượt. Giữ WAV gốc; trim đủ bằng chứng được dùng để tạo ứng viên, chưa áp vào dự án. Đã kiểm tra logic mất phủ định, lặp âm, độ tin cậy thấp, âm nhỏ đầu câu và ngân sách bằng fixture; còn cần pilot audio thật và nghe đối chiếu trước bật tự sửa.
- Sửa lỗi video mới có 0 cue nhưng còn chữ cũ: xóa track ASS khi nhận nội dung rỗng, gỡ canvas khi đổi video, ẩn lớp vẽ khi không có cue và bỏ response preview đã hủy. Không thêm phân tích AI; renderer không khởi tạo khi không có phụ đề.
- Kiểm tra Edge với workspace và worker libass thật, có xác nhận chữ đã được vẽ trước khi đổi video; kiểm tra đổi video, xóa cue cuối và đổi trong lúc khởi tạo. Script `backend/scripts/check_subtitle_preview_reset.py`; ảnh và kết quả ở `artifacts/subtitle-preview-reset/`.
- Sửa lỗi 422 khi giãn chữ 1,5 px: schema preview/render và legacy nhận số thực, giữ nguyên thông số. Tái lập bằng bản sao draft, kiểm tra live preview HTTP 200 và đường submit render; frontend hiển thị trường validation thay vì chỉ mã HTTP. Artifact `artifacts/render-422/validation.json`.

1. Hoàn thiện giai đoạn 2: job/quan sát/cache đã nối; còn nâng độ phủ nguồn có bằng chứng, đo thực bước xác minh WAV và chốt vùng phát/khoảng nghỉ có thể dùng. Đo lại khi audio/nguồn chữ thay đổi.
2. Giai đoạn 3: đã có bộ tính phương án dùng riêng biên lời nói và biên file, giữ giới hạn 1,15×, chặn khóa/chồng/thiếu bằng chứng và nhận mức mượn đã được xác minh tối đa 250 ms. Bộ kiểm tra lân cận dùng mốc phát thực kể cả offset âm và cue trùng thời gian bắt đầu. Đã nối áp dụng các ứng viên đạt vào dự án: kiểm tra owner/revision/toàn bộ đầu vào, checksum WAV gốc và WAV mới, tính lại chồng của cả nhóm, lưu snapshot trước ghi. Áp lại không cộng offset, có hoàn tác lấy WAV cũ. Chưa tự mượn khoảng nghỉ; còn xử lý nhóm khi các mốc cũ cản nhau và nghiệm thu mẫu thực.
3. Giai đoạn 4: job audit đã nối tạo WAV trim/tempo bằng FFmpeg một luồng rồi kiểm tra lại transcript, VAD và biên tiếng trên WAV mới; mốc đặt được tính lại từ biên tiếng đã đo. Tối đa hai WAV ứng viên / 30 giây audio cho bước này, nằm trong trần thời gian worker. Panel tải audio theo yêu cầu qua API kiểm tra owner/checksum, nghe ở 1× và giải phóng khi đổi đoạn. Sau áp dụng, timeline và export dùng cùng asset WAV ở 1×; export không thêm tempo hoặc fade riêng cho asset này. WAV gốc được giữ. Đã có giới hạn cache và dừng tác vụ lỗi thời, đo chi phí xử lý mẫu nhỏ; còn nghiệm thu nội dung/biên tiếng và tải tổng pipeline trên mẫu thực.
4. Giai đoạn 5: vòng Gemini/TTS tự sửa có budget lưu qua restart, bảo vệ bản chữ/lời chỉnh tay, thử sinh cụm cùng lượt nói khi có mapping đáng tin.
5. Kiểm chứng trải nghiệm: mốc nghe/xem nguồn của 40 cụm vẫn chưa được gán/duyệt; chưa có phép nghe mù, tập kiểm tra bổ sung hoặc đo P95 độ khớp. Chưa đủ điều kiện bật mọi tự sửa trên toàn dự án.

## Kiểm tra hồi quy

Đợt timeline trước đã chạy 69 test backend và kiểm tra UI. Đợt bằng chứng mốc hiện chạy các suite `test_speech_evidence`, `test_voice_quiet_analysis`, `test_voice_source_activity`, `test_subtitle_alignment*`, `test_gemini_alignment_source`, `test_subtitles`, `test_subtitle_timing_v2`, `test_voice_timing`; frontend chạy `draft.test`, `model.test` và thư mục `voiceover`. Kiểm tra TypeScript và lint các file thay đổi. Kết quả cuối của mỗi lượt được báo theo output thực, không suy từ số lượng test rằng đã đạt chất lượng ngôn ngữ.

Đợt worker/cache/đối chiếu WAV chạy 40 test backend đạt; TypeScript và lint panel, module mới đã kiểm tra. Harness Edge của panel/hook thật bổ sung submit audit theo revision hiện tại, hiện lời ASR/lý do và giữ nguyên mốc/clip. Các kiểm tra này chưa thay thế nghiệm thu nội dung, nghe mù hoặc P95 biên tiếng.

Đợt bổ sung bộ tính phương án, tạo WAV xử lý và audit: 10 test đạt. Test audio chạy FFmpeg thật, kiểm tra cache hỏng được tạo lại và giữ nguyên checksum WAV gốc. Đã thêm retry có giới hạn khi Windows tạm khóa checkpoint lúc đọc/ghi đồng thời.

Pilot model `large-v3-turbo` trên bốn cửa sổ mất 36,381 giây, chỉ 1/4 đủ điều kiện xác minh nguồn (`stage2-asr-turbo-20260913/measurement.json`); chưa chọn làm mặc định. Lượt thử nguồn kèm kiểm tra WAV sau đó chạm trần 90 giây, worker đã bị dừng. Checkpoint `stage3-dubbed-pilot-20260913` ghi thất bại nhưng chưa lưu các hàng trung gian; lượt này chưa có báo cáo measurement hoàn chỉnh và không chứng minh bước xác minh WAV đạt chất lượng.

Sau lượt timeout, đã lưu checkpoint danh sách đoạn trước VAD, sau VAD và sau ASR; script pilot ghi measurement trạng thái thất bại trước khi trả lỗi. Kiểm tra WAV dùng lời đọc sau từ điển phát âm, đúng với đầu vào TTS. Đợt tích hợp ứng viên/audio API: 18 test backend đạt, gồm FFmpeg thật, biên đo lại vượt vùng/chồng hàng xóm, checksum hỏng, owner khác và deadline worker. TypeScript/lint đạt. Harness Edge ở frontend kiểm thử riêng xác nhận WAV 0,5 giây phát ở 1×, đổi đoạn gỡ player, không đổi clip hoặc sinh TTS. Đây là kiểm tra luồng với bằng chứng ASR giả lập; chưa chứng minh chất lượng giọng sau xử lý trên mẫu thực.

### Áp dụng kết quả và thống nhất waveform

- Metadata `sync.alignment` giữ biên nguồn/tiếng/file riêng cùng dấu kiểm chứng ở server. Backend bỏ kiểm chứng nếu client sửa metadata, lời đọc, mốc, asset, profile hoặc từ điển phát âm. Frontend bỏ kiểm chứng khi chữ nguồn đổi dù bản dịch và mốc hiển thị không đổi. Export video kiểm tra lại chữ nguồn trong request.
- Đoạn đã căn còn cùng nội dung/nguồn và checksum được giữ, không nạp lại ASR/VAD. Chưa bật tự áp lên toàn bộ dự án; nút áp dụng dùng các ứng viên đủ điều kiện của lượt kiểm tra đã chạy.
- 74 test backend đạt trong đợt tích hợp apply/timeline/export; 34 test frontend đạt. Fixture chung `test-fixtures/voiceover-alignment.json` kiểm tra signature và biên file đo được ngoài ô chữ. Test FFmpeg thật xác nhận PCM trong track ghép và audio xuất sau trim video giống từng mẫu với WAV ứng viên ở đúng mốc, không tăng tốc/fade lần hai. Harness Edge xác nhận áp asset mới ở 1×, câu sau giữ mốc và undo về clip cũ.
- Pilot `stage3-single-wave-20260913/measurement.json`: một WAV 1,36 giây, nguồn dùng cache ASR; model large-v3-turbo nhận đúng “Sau này muốn ăn gà.” và cho ứng viên bỏ 128 ms đệm cuối. Tổng wall 41,942 giây. Phương án bị chặn do các đoạn lân cận đang chiếm vùng nói; không tạo ứng viên xử lý hoặc áp vào dự án. Tài liệu/WAV nguồn giữ nguyên. Đây là bằng chứng đối chiếu tự động trên một mẫu, chưa phải đo biên chuẩn hoặc nghiệm thu chất lượng.
- Đối chiếu `stage3-small-wave-20260913/measurement.json`: cùng WAV và cache nguồn, model small mất 10,256 giây, nhận đúng cùng chữ và biên 50–1.010 ms nhưng từ đầu “Sau” có confidence 0,555, dưới ngưỡng pilot 0,65. Giữ trạng thái chưa xác minh; không kết luận TTS sai hoặc hạ ngưỡng để tăng số đoạn đạt. Chưa đủ mẫu/tải được kiểm soát để suy hệ số tăng tốc chung. Tách model kiểm tra WAV khỏi model nguồn, mặc định model nhỏ, và thêm chẩn đoán phân biệt đúng chữ nhưng thiếu độ chắc chắn với sai nội dung. Không tự gọi model lớn hàng loạt.

### Cache xử lý và dừng tác vụ lỗi thời

- Cache WAV ứng viên giới hạn 256 MiB cho WAV/manifest và 256 mục, dọn mục ít dùng trước. Có khóa encoder giữa các tiến trình, dự trù dung lượng trước FFmpeg và bảo vệ file đang kiểm tra/nghe/áp dụng. Lease bị bỏ do worker chết hết hạn sau 660 giây; khóa hệ điều hành được giải phóng ngay khi tiến trình chết. Chỉ dọn file tạm/cache theo mẫu tên trong thư mục được quản lý; WAV gốc và asset đã áp dụng ở thư mục riêng được giữ.
- Backend kiểm tra thay đổi file dự án bằng stat; chỉ đọc lại nội dung khi file đổi. Đổi đầu vào thật dừng cây worker đang chạy; lưu lại cùng đầu vào chỉ tăng revision không hủy. Frontend dừng audit khi sửa mốc hoặc nguồn chữ chưa lưu; không tạo WAV cho từng trạng thái kéo chỉnh. Kết quả cũ vẫn bị kiểm tra binding khi áp dụng.
- 25 test cache/worker/apply đạt. Có tiến trình thật giữ khóa rồi bị dừng, worker và tiến trình con bị hủy khi dự án đổi, và export sau khi cache ứng viên bị dọn. Harness Edge xác nhận chỉnh mốc/chữ nguồn chưa lưu gửi hủy audit, ngoài các kiểm tra nghe/áp dụng/undo đã có.
- `scripts/benchmark_sync_audio_cache.py` trên 8 WAV baseline: xử lý nối tiếp 1,255 giây, 8 lần FFmpeg; đọc lại 0,291 giây, 0 lần FFmpeg. Trung vị một lệnh FFmpeg (khởi tạo và xử lý gộp) 0,106 giây. Cache 1.404.402 byte. Peak RSS Python 39.161.856 byte; CPU Python lần đầu 0,3125 giây, lần đọc lại 0,171875 giây. Các số CPU/RSS **không gồm FFmpeg con**; không ASR/TTS và không suy chất lượng từ phép đo này. Artifact `stage4-cache-20260914/measurement.json` xác nhận WAV nguồn giữ nguyên.
