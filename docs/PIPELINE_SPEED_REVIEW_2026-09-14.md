# Rà soát tốc độ dịch, lồng tiếng và xuất video

Ngày: 14-09-2026. Phạm vi: code hiện tại, gồm bộ đối chiếu/tự sửa Gemini vừa thêm,
cùng các số liệu cũ đã có. Không chạy benchmark, test, API hoặc thay cấu hình.

Mục tiêu là giảm xử lý lặp và thời gian chờ, giữ model, nội dung, mapping/timing,
giọng, thông số lấy mẫu, DSP, bộ lọc video và cấu hình encoder hiện tại.
Các mức tác động bên dưới là đánh giá từ đường chạy, không phải tốc độ đã đo sau tối ưu.

## Các phát hiện và thứ tự đề xuất

### 1. Tách cache hình và âm thanh khi xuất lại — ưu tiên cao

`backend/app/api/subtitles.py:948` đưa toàn bộ `voice_document` vào `options_data`.
`subtitle_render.py:60` đưa nguyên options vào khóa cache; dòng 902 dùng khóa này
cho video hình trước khi `finalize_voiced_render` ghép giọng.

Hệ quả: thay WAV, âm lượng hoặc metadata/revision giọng có thể làm cache hình mất
hiệu lực dù phụ đề, hình, mask, overlay và thiết lập encoder không đổi. Video dài
phải mã hóa hình lại trước khi ghép âm thanh mới.

Đề xuất: khóa cache hình chỉ chứa các đầu vào tác động tới video trung gian;
khóa đầu ra cuối chứa cả khóa hình và bản phối âm. Giữ kiểm tra revision/nguồn
cho request cuối, không bỏ dependency ảnh hưởng filter, font, mask, cut, speed,
phụ đề hoặc audio gốc trong video trung gian. Tái sử dụng đúng luồng hình đã mã hóa.

Tác động dự kiến lớn nhất khi chỉ sửa giọng rồi xuất lại; không giúp lần xuất hình
đầu tiên. Chất lượng hình giữ nguyên vì dùng lại dữ liệu đã mã hóa.

### 2. Cache WAV đã chuyển đổi theo từng clip khi ghép giọng — ưu tiên cao

`voiceover/mix.py:272` chạy FFmpeg tuần tự cho từng clip, luôn tạo `clip.wav` tạm,
gồm tempo, volume, fade, đổi sample rate và PCM16. Cache hiện tại trong
`compose_voice` là cả track; thay một clip làm mất cache track và chuyển đổi lại
tất cả clip còn lại. Cache trim/tempo trong `processed_audio.py` không thay thế
được bước này: đó là một công thức khác, chưa bao gồm toàn bộ gain/fade/resample.

Đề xuất: cache sản phẩm chuyển đổi theo checksum WAV + rate + gain + fade + sample
rate + định dạng + phiên bản FFmpeg/DSP. Khi sửa một clip chỉ chuyển đổi clip đó,
sau đó ghép lại bằng chính các PCM đã có. Đặt quota, pin file trong lúc xuất và
ghi atomic; không để hai job tạo trùng cùng cache entry.

Có thể thêm hàng đợi chuyển đổi nhỏ trước bước nối WAV, nhưng giữ nguyên từng
filter chain, thứ tự nối, rounding sample và kiểm tra chồng. Không gộp các đoạn
thành một bộ lọc tempo chung vì có thể thay đổi hành vi ở ranh giới clip.

Số liệu tham khảo, không phải benchmark của giải pháp đề xuất:
`artifacts/voiceover/sync-implementation/stage4-cache-20260914/measurement.json`
đã đo 8 chuyển đổi trim/tempo: lần tạo 1,255 s, dùng lại cache 0,291 s và không gọi
FFmpeg. Không suy ra mức tăng tốc tương tự cho toàn bộ export hoặc hàng nghìn cue.

### 3. Giữ upload Gemini qua các lượt sửa và lưu cache proxy — ưu tiên cao

`gemini_pipeline.py:183` upload trong vòng retry; sau kết quả chưa đạt, dòng 245
chạy tiếp vòng lặp nhưng `finally` ở dòng 278 xóa upload. Lượt sửa lại tải cùng
file lên. Một lượt dịch và lượt đối chiếu kế tiếp đã dùng chung upload; phần còn
lặp là giữa các lượt sửa. Proxy chỉ tồn tại trong workspace và bị xóa ở dòng 285;
resume chunk chưa đạt còn có thể phải nén lại.

Đề xuất:

- Giữ cùng upload cho dịch → đối chiếu → sửa → đối chiếu, khi vẫn dùng đúng key
  sở hữu file. Chỉ tải lại nếu đổi key, file không còn dùng được hoặc video đổi.
- Cache proxy theo fingerprint nguồn, khoảng cắt, quy tắc timeline và toàn bộ
  thông số nén/phiên bản công cụ. Thay prompt không cần nén lại cùng video.
- Lưu checkpoint riêng cho ứng viên đã tạo nhưng chưa đối chiếu xong. Sau lỗi
  mạng/restart có thể kiểm tra tiếp ứng viên đó; tuyệt đối không coi nó đã đạt.

Giữ nguyên mọi lượt kiểm tra chất lượng và ngân sách tự sửa; không giảm độ phân
giải, FPS, bitrate, model hoặc lượng clip được quan sát để lấy tốc độ.
Lợi ích tập trung vào retry/resume; thời gian suy luận của một request không đổi.

### 4. Giảm nạp lại model TTS giữa các job/đợt sửa — ưu tiên sau

`voiceover/manager.py:387` và `repair_tts.py:70` mở process worker mới;
`runtimes/voiceover/worker.py:400` nạp engine mỗi lần chạy. Các đợt sửa nhóm tại
`repair_pipeline.py:197` có thể lần lượt mở worker cho từng nhóm.

Đề xuất trước tiên gom các nhóm sửa độc lập vào cùng đợt worker, giữ riêng text,
mapping, ngân sách và WAV. Bước lớn hơn là worker sống qua nhiều request, có idle
timeout, khóa GPU dùng chung và nhường tài nguyên cho aligner. Cần xóa trạng thái
giọng/reference giữa job và giữ chính sách RNG/lấy mẫu, checkpoint, cancel như cũ.
Không tăng số worker GPU một cách cơ học trên card 6 GB.

`docs/VOICEOVER_HARDWARE_BALANCE.md` ghi nhận thời gian nạp GPU khoảng 9,68–11,61 s
ở lượt đo cũ. Đây là chi phí đáng chú ý cho nghe thử/sửa ít câu, chưa chứng minh
worker lâu dài tăng tốc bao nhiêu hoặc giữ kết quả giống hệt.

GPU batch 4 và CPU hỗ trợ 2 luồng đã có trong cấu hình máy; đây không phải tối ưu
mới. Lượt đo 18 câu trước đây là 13,35 s với batch 4 so với 28,83 s với batch 1,
nhưng waveform có khác biệt do lấy mẫu. Không đề xuất đổi batch/precision như một
biện pháp chắc chắn giữ nguyên chất lượng.

### 5. Tránh quét lại toàn bộ WAV và dựng waveform peaks nhiều lần — ưu tiên vừa

`audio.py:14` vừa đọc checksum vừa tính peaks trên toàn bộ audio. Hàm này được gọi
trong `mix.verify_document`, ở API xuất trước job, trong `export_voice_audio`, rồi
trong `compose_voice`. `processed_audio.py:44,63,84` cũng dựng lại metadata để chủ
yếu kiểm tra checksum, kể cả khi cache đã có kết quả.

Đề xuất: tách kiểm tra tính toàn vẹn khỏi dựng peaks. Trong một export snapshot,
xác minh mỗi asset một lần và tái sử dụng kết quả khi file bất biến; giữ kiểm tra
thay đổi file, revision và proof. Peaks chỉ cần tính khi ingest/tạo asset. Không
bỏ checksum hoặc mặc nhiên tin file chỉ vì tên hash đúng.

Tác động dự kiến rõ hơn khi có nhiều cue hoặc xuất lại, không làm model TTS tự
suy luận nhanh hơn.

### 6. Giảm lượt ghi/đọc video trung gian — ưu tiên sau, cần đối chiếu DSP

`mix.py:380` tạo track WAV, trộn vào video MKV với PCM16, sau đó dòng 397 đọc MKV
để mã hóa AAC và copy video vào MP4. Phần hình đã dùng `-c:v copy` ở các bước này,
không có bằng chứng hình đang bị mã hóa lại nhiều lần trong riêng pha ghép giọng.
Chi phí còn lại là quét và ghi video trung gian, audio trung gian và mux lại.

Đề xuất: giữ cache audio trung gian riêng, ghép luồng video có sẵn với audio cuối
một lần. Có thể chuẩn bị track giọng trong lúc render hình nếu tài nguyên cho phép.
Cần giữ thứ tự limiter, duck envelope, gain, tempo, PCM16 rounding và AAC settings;
không gộp các bước DSP tùy tiện rồi khẳng định âm thanh giống hệt.

## Những tối ưu đã có và không nên tính lại thành lợi ích mới

- Gemini chia chunk, chạy nhiều worker theo key, cache VAD và checkpoint chunk.
- Lượt tạo và lượt đối chiếu đầu tiên dùng cùng file Gemini đã upload.
- TTS có cache theo nội dung/giọng, checkpoint từng WAV, batch GPU và thu nhỏ batch khi lỗi.
- Renderer thử encoder phần cứng và có cache video; pha ghép giọng copy luồng hình.
- Audio đã có cache trim/tempo; cần mở rộng tới công thức chuyển đổi dùng khi xuất.

Không đề xuất bỏ đối chiếu Gemini, đổi model nhẹ hơn, giảm bitrate/FPS/độ phân giải,
đổi sang preset video nhanh hơn hoặc quantize TTS khi yêu cầu là giữ chất lượng.
Trong `_encoder_arguments`, profile `fast` còn đổi CQ/CRF chứ không chỉ đổi tốc độ.

## Điều kiện nghiệm thu khi triển khai

- Chỉ đổi giọng: không gọi mã hóa video lại; hình/timing/phụ đề vẫn dùng đúng cache.
- Cache WAV: PCM và số sample trùng với pipeline cũ ở cùng asset/thông số.
- Upload/proxy: cùng bytes video và hệ timestamp; mọi ứng viên vẫn qua đối chiếu.
- Warm worker: giữ model/revision/voice/reference/thiết bị/tham số và cơ chế hủy;
  phải nghe đối chiếu trước khi cam kết chất lượng tương đương.
- Mỗi số liệu tốc độ cần tách cold/warm cache, lần đầu/xuất lại và từng pha.
  Không cộng tỷ lệ tăng tốc từng pha thành tỷ lệ tăng tốc toàn dự án.

Thứ tự triển khai đề xuất: tách cache hình/âm thanh → cache WAV theo clip → tái dùng
upload/proxy/checkpoint Gemini → tái dùng metadata → giảm nạp model và lượt mux.
