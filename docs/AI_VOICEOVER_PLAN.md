# Kế hoạch giọng đọc AI local và Kaggle

Ngày: 07/09/2026. Trạng thái: kế hoạch triển khai, chưa cài model hoặc sửa chức năng ứng dụng.

## 1. Mục tiêu và phạm vi

Tạo lời thuyết minh tiếng Việt theo phong cách review phim, một giọng nhất quán cho video dài 2–3 giờ. Có thể chọn giọng có sẵn hoặc tạo hồ sơ từ mẫu giọng được phép sử dụng. Chạy trên máy người dùng, chuyển tác vụ sang Kaggle khi cần, không phụ thuộc dịch vụ TTS tính phí qua API.

Máy đã kiểm tra: i5-13420H, RAM khoảng 16 GB, RTX 4050 Laptop 6 GB VRAM. Đây là máy mục tiêu để đo hiệu năng; chưa có benchmark suy luận TTS.

Yêu cầu giao diện bắt buộc: hàng **Giọng đọc AI nằm ngay trên hàng Phụ đề**, dùng chung thời gian, zoom, cuộn ngang và vạch phát.

Bản đầu bao gồm: chọn/lưu giọng, nghe thử, sửa lời đọc riêng, tạo theo đoạn, căn thời gian, waveform, tạo lại đoạn, lưu/tiếp tục, xuất/nhập gói Kaggle, trộn âm, xuất MP4 và audio riêng.

Các hạng mục sau bản đầu: huấn luyện thêm model, tự nhận diện và phân vai nhiều nhân vật, đồng bộ khẩu hình, tách thoại khỏi nhạc/hiệu ứng gốc, tự điều khiển tài khoản Kaggle. Việc viết lại phim thành kịch bản review là một chức năng biên tập riêng; TTS đọc văn bản người dùng cung cấp.

## 2. Model và lựa chọn giọng

Ứng viên đầu tiên: **VieNeu-TTS v3 Turbo**. Tài liệu model mô tả CPU/ONNX, GPU/PyTorch, audio 48 kHz, 20 giọng có sẵn và nhân bản từ mẫu 3–8 giây. Có preset tên Ngọc Huyền, nhưng không xem việc trùng tên là bằng chứng giống giọng Vbee. Tham số `style` được ghi là không còn tác dụng ở v3 Turbo; không xây một nút chỉnh biểu cảm giả dựa vào tham số này. [Nguồn model](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Turbo)

Khóa SDK, model revision, tokenizer/codec, backend và thiết lập suy luận sau thử nghiệm; không dùng phiên bản mới nhất tự động. Repo và model card có thể khác nhau về mặc định/cách cài nên môi trường cuối cùng phải được kiểm chứng trên cả Windows và Kaggle. [Nguồn SDK](https://github.com/pnnbao97/VieNeu-TTS)

Quy trình chọn giọng:

1. Nghe thử preset Ngọc Huyền cùng 2–3 giọng nữ Bắc/kể chuyện, dùng cùng một đoạn review 60–90 giây.
2. Đánh giá chất giọng, dấu tiếng Việt, ngắt câu, tên riêng, độ mệt khi nghe và độ ổn định qua nhiều đoạn.
3. Nếu chưa đạt, nhập mẫu riêng sạch, chỉ một người nói; chọn đoạn 3–8 giây phù hợp model. Cho nghe mẫu gốc và bản xử lý trước khi lưu. Mẫu Vbee chỉ được dùng khi quyền sử dụng cho phép; link thư viện không phải model local.
4. Lưu hồ sơ giọng và phiên bản. Sửa mẫu tạo phiên bản mới, không thay ngầm giọng của audio đã hoàn thành.
5. Chốt mẫu nghe trước khi tạo phim dài. Không cam kết độ giống theo phần trăm hoặc giống hoàn toàn Vbee.

Bản đầu dùng suy luận từ giọng có sẵn/mẫu, chưa cần fine-tune. Nếu sau thử mẫu vẫn không đạt, lập benchmark cho model khác hoặc kế hoạch huấn luyện riêng trước khi đầu tư triển khai tiếp.

## 3. Luồng người dùng

Mở video → nhập/tạo phụ đề → chọn Giọng đọc AI → chọn giọng và chế độ chạy → chỉnh lời đọc → nghe thử → tạo toàn bộ/phần đã chọn → kiểm tra đoạn cần sửa → xuất video.

Thanh công cụ: Giọng, Nghe thử, Tạo phần chọn, Tạo phần còn thiếu, Tạm dừng, Tiếp tục, Xuất sang Kaggle, Nhập kết quả.

Giao diện timeline dự kiến:

```text
Thời gian      00:00          00:05          00:10
Giọng đọc AI   [ ≋≋ Đoạn 1 ≋≋ ] [ ≋ Đoạn 2 ≋ ]
Phụ đề        [ Câu 1 ][ Câu 2 ] [   Câu 3    ]
Ảnh phủ       [ ... nếu có ...                 ]
Video         [ khung hình ...                ]
```

Một đoạn giọng có thể liên kết nhiều cue phụ đề để giữ câu đọc tự nhiên. Click đoạn giọng chọn các cue liên quan; click cue làm nổi đoạn giọng tương ứng. Bản đầu chọn một đoạn giọng tại một thời điểm, nhưng cho tạo nhiều đoạn qua vùng chọn.

Mỗi block hiển thị waveform, lời đọc rút gọn, thời lượng và trạng thái bằng chữ/biểu tượng. Màu chỉ hỗ trợ phân biệt, không phải tín hiệu duy nhất. Có trạng thái chưa tạo, đang chờ, đang tạo, sẵn sàng, cần tạo lại, vượt thời lượng và lỗi.

Panel đoạn có: lời đọc, giọng kế thừa từ dự án, mốc bắt đầu, tốc độ, gain, nghe riêng, nghe cùng video và tạo lại. Kéo block đổi vị trí; bản đầu chưa cho kéo mép để cắt mất từ trong audio. Chỉnh thời lượng qua công cụ tốc độ/khoảng nghỉ có ý nghĩa rõ ràng.

Hàng giọng có mute và gain riêng. Chỉnh vị trí có undo/redo. Nếu đoạn được kéo độc lập, ghi nhận offset; có nút đưa về mốc phụ đề.

## 4. Lời đọc và chia đoạn

Lưu `spoken_text` riêng với chữ phụ đề. Cho sửa cách đọc tên người, game, chữ viết tắt, số và từ nước ngoài qua từ điển phát âm của dự án. Chuẩn hóa Unicode, khoảng trắng và dấu câu; không tự tóm tắt hoặc thêm nội dung.

Chia theo ranh giới câu/cụm câu, tránh chẻ giữa từ hoặc gom qua khoảng nghỉ/cảnh dài. Mục tiêu khởi đầu khoảng 5–20 giây lời đọc mỗi đoạn, điều chỉnh sau benchmark và theo giới hạn model. Không coi mỗi dòng phụ đề là một câu hoàn chỉnh.

Mỗi đoạn giữ danh sách `source_cue_ids`. Không tự gom các cue chồng lấn thành lời đọc đồng thời; đưa vào danh sách cần duyệt cho chế độ một giọng.

Chế độ đầu tiên: bám các khoảng thời gian đã có. Nếu người dùng nhập kịch bản review tự do, họ cần đặt/duyệt đoạn trên timeline; không suy ra mốc hình chính xác chỉ từ văn bản. Có thể thêm tự sắp tuần tự trong giai đoạn sau.

## 5. Dữ liệu và lưu trữ

Giữ schema phụ đề V2, thêm document giọng độc lập có schema version riêng. Dữ liệu dự án bền vững nằm ở backend; localStorage chỉ giữ ID/thông tin giao diện, không chứa audio hay waveform đầy đủ.

| Đối tượng | Trường chính |
| --- | --- |
| VoiceProfile | id, revision, name, engine, model_revision, preset_id hoặc reference_asset_id, reference_hash, thiết lập xử lý mẫu |
| VoiceDocument | project_id, video_fingerprint, revision, timebase=milliseconds, default_voice, clips, mix_options |
| VoiceClip | id, source_cue_ids, spoken_text, source_start_ms, source_end_ms, offset_ms, voice_revision, generation_hash, asset_id, actual_duration_ms, rate, gain, status |
| VoiceAsset | id, checksum, path nội bộ, sample_rate, channels, duration, peaks_asset_id, engine/backend metadata |
| VoiceJob | id, project/document revision, device, manifest, completed/failed/pending clip IDs, progress, checkpoints |

Vị trí dự kiến: `data/voiceover/profiles`, `projects`, `assets`, `jobs`, `imports`. Model/runtime có thư mục riêng để nâng cấp độc lập. Asset được phục vụ bằng ID có kiểm tra quyền sở hữu theo cơ chế ứng dụng hiện tại; client không được gửi đường dẫn tùy ý.

Migration draft hiện có phải chấp nhận dự án chưa có hàng giọng. Dự án cũ mở được và xuất video như trước.

## 6. Worker local và tác vụ dài

Tách môi trường Python TTS khỏi backend FastAPI để tránh xung đột torch/transformers. Backend gửi manifest cho một tiến trình worker cục bộ, nhận sự kiện tiến độ; không cần API key hoặc dịch vụ TTS từ xa. Tiến trình quản lý trên Windows chạy ẩn, log xuất vào ứng dụng/terminal hiện có.

Worker nạp model một lần rồi tạo nhiều đoạn. Một tác vụ GPU hoạt động tại một thời điểm ở cấu hình đầu; batch bắt đầu từ 1, chỉ tăng sau đo VRAM. CUDA hết bộ nhớ: giảm batch và thử lại giới hạn; nếu vẫn lỗi, báo rõ và cho chọn CPU hoặc xuất Kaggle. Không đổi model âm thầm.

Đo CPU cho nghe thử ngắn và GPU cho hàng loạt, không mặc định GPU luôn nhanh hơn. Worker có timeout mỗi đoạn, timeout khởi động, heartbeat và đóng tiến trình khi hủy cứng. Tạm dừng dừng nhận đoạn mới, hoàn thành batch đang chạy; hủy có thể bỏ batch chưa hoàn tất nhưng giữ các asset đã commit.

Lưu audio bằng file tạm rồi rename nguyên tử; checkpoint sau từng đoạn hoặc batch commit. Khi backend khởi động lại, tác vụ đang chạy được ghi là gián đoạn. Lệnh tiếp tục tạo một lượt chạy mới cho các đoạn còn thiếu đã xác minh checksum.

Cache tạo giọng dựa trên chữ chuẩn hóa, phiên bản chuẩn hóa/phát âm, model, giọng/mẫu, backend và tham số sinh. Cache đặt audio trên timeline tách riêng: thay mốc không phải sinh lại âm thanh; đổi text/giọng mới làm audio cũ không còn phù hợp. Kết quả job cũ không được ghi đè bản chỉnh mới; lưu asset nhưng chỉ gắn khi revision/hash còn khớp.

Giao diện báo X/Y đoạn, số lỗi, tổng thời lượng đã tạo, tốc độ thực đo và ETA cập nhật. Không suy ETA chỉ theo thời lượng video.

## 7. Căn thời gian và phát thử

Giữ mốc gốc bằng số nguyên mili giây; khi ghép audio đổi sang chỉ số sample tại sample rate chuẩn của pipeline. Đo `actual_duration_ms` từ file thực tế, không dùng độ dài chữ để kết luận chính xác.

Audio ngắn hơn khung: giữ khoảng nghỉ còn lại, không kéo dài giọng cho đủ. Audio dài hơn khung: cho tăng tốc giữ cao độ trong giới hạn nhẹ. Mức thử ban đầu của preset review là 1.05–1.15x, giới hạn tổng tự căn khoảng 1.20x; đây là lựa chọn sản phẩm cần nghe thử, không phải thông số chất lượng model. Nếu vẫn không vừa, đánh dấu để sửa lời đọc hoặc mốc; không tự cắt mất từ.

Tách tốc độ đọc, tốc độ căn khung và tốc độ video; kiểm tra tốc độ hiệu dụng để tránh nhân nhiều lần làm giọng quá nhanh. Đoạn giao với vùng cắt video được đánh dấu duyệt trước khi xuất nếu việc cắt có thể mất lời.

Tất cả hàng dùng chung time map cắt/ghép/đổi tốc độ. Preview và render lấy cùng kế hoạch đặt audio. Khi đổi cuts hoặc video_speed, cache bản trộn mất hiệu lực nhưng audio sinh ban đầu vẫn dùng lại được.

Trình phát lấy đồng hồ video hiện tại làm chuẩn. Khi seek/pause/buffering/đổi tốc độ, hủy lịch audio cũ và đặt lại đoạn quanh vị trí mới. Chỉ nạp audio vùng đang phát và vùng sắp phát, không giải mã toàn bộ phim vào RAM trình duyệt.

Waveform dùng peaks nhiều mức độ chi tiết tính ở backend; chỉ vẽ các block trong viewport theo cơ chế timeline hiện có. Không tạo một phần tử audio cho mọi cue.

## 8. Kaggle qua gói tác vụ

Bản đầu dùng notebook và gói ZIP xuất/nhập thủ công; không mở máy local thành server public. Notebook xử lý suy luận, không huấn luyện lại giọng.

Gói đầu vào chứa `manifest.json`, danh sách đoạn/lời đọc, cấu hình model khóa revision, mẫu giọng cần thiết và các checkpoint/audio hoàn thành nếu chọn tiếp tục. Không cần gửi video gốc để làm TTS. Model được tải theo revision đã chốt; khi không có Internet, notebook hỗ trợ đường dẫn model đã chuẩn bị.

Notebook gồm các bước: kiểm tra GPU/môi trường → xác minh manifest → tải model → chạy thử một đoạn → xử lý batch → lưu checkpoint → kiểm tra audio → đóng gói kết quả.

Kết quả gồm manifest, WAV/FLAC từng đoạn, checksum, thông tin môi trường và danh sách lỗi. Khi nhập, đối chiếu project ID, video fingerprint, document revision và hash từng đoạn. Đoạn đã sửa local không bị ghi đè; gói thiếu được nhập phần hợp lệ; nhập cùng gói lần hai không sinh bản sao.

ZIP import có giới hạn kích thước giải nén, số file, định dạng, và kiểm tra đường dẫn để không ghi ra ngoài thư mục import. Chỉ chấp nhận media/metadata, không thực thi mã trong gói.

Lưu checkpoint trong phiên chưa đủ bảo đảm tồn tại sau khi Kaggle kết thúc. Notebook hướng dẫn lưu phiên bản kèm output hoặc tải checkpoint/kết quả về theo lô. Tiếp tục phiên sau bằng chính gói output đã lưu. Không giả định quota GPU hoặc độ dài phiên cố định; kiểm tra trong giao diện tài khoản trước khi chạy. [Tài liệu Kaggle](https://www.kaggle.com/docs/notebooks), [theo dõi GPU](https://www.kaggle.com/docs/efficient-gpu-usage).

Dùng cùng model/giọng/cấu hình để giảm thay đổi âm sắc giữa local và Kaggle; không cam kết file giống từng byte trên phần cứng khác. Nghe đối chiếu đoạn nối khi trộn kết quả hai môi trường.

## 9. Trộn âm và xuất

Các chế độ: chỉ giọng AI; giọng AI cộng âm gốc với gain riêng; giảm âm gốc tại khoảng có lời đọc. Giảm âm gốc cũng giảm nhạc và hiệu ứng trong cùng track; tách thoại là chức năng sau.

Chuẩn hóa âm lượng theo mục tiêu nhất quán của dự án, có headroom và chống clipping. Thêm fade rất ngắn ở biên phù hợp để tránh tiếng click, không làm mất phụ âm đầu/cuối.

Ghép theo lô/chunk ra file trung gian rồi trộn tuần tự bằng FFmpeg, tránh filter graph hàng nghìn input và tránh nạp toàn bộ audio vào RAM. Xuất MP4/AAC; audio riêng WAV/FLAC và tùy chọn MP3. Ví dụ dung lượng thô 3 giờ mono 48 kHz PCM16 khoảng 1.04 GB (chưa gồm cache/trung gian); kiểm tra dung lượng trống trước khi chạy.

Sửa render để video gốc không có audio vẫn xuất được track AI. Cache render bao gồm hash asset giọng, vị trí, rate, gain, mix settings và revision pipeline. Thay đoạn giọng phải tạo bản render mới. Không áp dụng đường copy audio gốc khi có trộn giọng.

## 10. Điểm tích hợp mã nguồn

| Khu vực | Thay đổi dự kiến |
| --- | --- |
| `frontend/src/SubtitleStudio.tsx` | Điều phối document giọng, panel, tác vụ, nhập/xuất Kaggle |
| `frontend/src/subtitles/SubtitleWorkspace.tsx` | Truyền trạng thái giọng, liên kết chọn cue và preview |
| `frontend/src/subtitles/SubtitleTimeline.tsx` | Thêm hàng giọng trên `subtitle-track-row`, dùng chung thước/scroll/playhead |
| `frontend/src/subtitles/draft.ts` | Migration tham chiếu dự án giọng và trạng thái giao diện |
| `frontend/src/subtitles/types.ts`, `frontend/src/api.ts` | Kiểu dữ liệu và client endpoint giọng |
| `frontend/src/voiceover/` mới | VoicePanel, VoiceTrack, VoiceClipBlock, playback, kiểu dữ liệu |
| `backend/app/api/voiceover.py` mới | API trạng thái runtime, profiles, document, jobs, assets, packages |
| `backend/app/services/voiceover/` mới | Store, planner, worker supervisor, cache, waveform, package, mix |
| `backend/app/services/subtitle_jobs.py` | Tái sử dụng cơ chế progress/cancel/persist, bổ sung loại job và checkpoint phù hợp |
| `backend/app/schemas.py` | Gắn tham chiếu voice document vào render; đồng bộ job kinds giữa FE/BE |
| `backend/app/services/subtitle_render.py` | Time map âm thanh, mix AI, audio-less source, render cache |
| `runtimes/voiceover/` mới | Môi trường/lockfile và entrypoint worker, model nằm ngoài Git |
| `notebooks/voiceover_kaggle.ipynb` mới | Notebook dùng cùng worker/manifest đã version hóa |

Endpoint dự kiến dưới `/api/v1/voiceover`: `GET /status`, `GET/POST /profiles`, `POST /profiles/{id}/preview`, `GET/PUT /projects/{id}`, `POST /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/pause`, `/cancel`, `/resume`, `GET /assets/{id}`, `POST /packages/export`, `/packages/import`. Preview cũng là job, không giữ HTTP request trong lúc nạp model lâu. Thiết kế route cuối cùng phải theo dependency auth và conventions của app.

Repo hiện có nhiều thay đổi chưa commit ở SubtitleStudio, schemas và renderer. Khi triển khai phải đọc lại diff và mở rộng trên trạng thái hiện có, không reset hoặc ghi đè công việc đang làm.

## 11. Các mốc triển khai và nghiệm thu

| Mốc | Công việc | Điều kiện hoàn thành |
| --- | --- | --- |
| P0 — Chọn model/giọng | Runtime thử riêng, preset/mẫu, đo CPU/GPU | Có 3 mẫu nghe cùng văn bản, ghi phiên bản và RAM/VRAM/thời gian; người dùng chọn được giọng |
| P1 — Lưu giọng/tạo đoạn | Schema, store, worker, profile, preview, job/cache | Tạo đoạn local, xem lỗi rõ, mở lại app vẫn có audio và profile |
| P2 — Hàng giọng/preview | Track, waveform, panel, sửa text, liên kết cue, playback | Track nằm đúng chỗ; seek/pause/zoom/undo hoạt động; sửa text hiện cần tạo lại |
| P3 — Phim dài/căn thời gian | Chia đoạn, rate, checkpoint, resume, tài nguyên | Tác vụ 2–3 giờ dữ liệu không nạp toàn phim; ngắt và tiếp tục không tạo lại đoạn hợp lệ |
| P4 — Kaggle | Notebook, package, checksum, import merge | Chạy một phần local, phần còn lại Kaggle; nhập được vào đúng timeline; gói cũ không ghi đè sửa mới |
| P5 — Trộn/xuất | FFmpeg, ducking, cache, audio-only export | Video có/không có audio đều xuất; cắt/đổi tốc độ không lệch track |
| P6 — Hoàn thiện | Dự án cũ, regression, tài liệu cài và hướng dẫn | Build/check liên quan đạt; có mẫu phim dài kiểm chứng và báo cáo giới hạn |

P0 trước mọi triển khai lớn. Sau P1, phần notebook có thể phát triển độc lập về mặt kỹ thuật với giao diện nhưng vẫn phải dùng chung manifest. Ước lượng công sức theo số mẫu cần sửa và benchmark sau P0; chưa hứa thời gian tạo phim hoặc ngày hoàn thành khi chưa đo.

## 12. Bộ kiểm thử có ý nghĩa

- Chia/gom câu: dấu câu, tên nước ngoài, số, cue chồng nhau và câu băng qua vùng cắt; không mất hoặc lặp lời.
- Cache: đổi text/voice/model cần tạo lại; đổi vị trí/gain chỉ dựng lại; kết quả job cũ không đè document mới.
- Worker: OOM, timeout, crash, cancel lúc ghi file; file chưa hoàn chỉnh không được dùng; resume chỉ chạy phần thiếu.
- Timeline/preview: seek vào giữa audio, play/pause liên tiếp, buffering, mute, rate và viewport có hàng nghìn block.
- Render: nguồn im lặng, mix, ducking, trim đầu/cuối, bỏ nhiều đoạn, đổi tốc độ; dùng xung âm ở mốc biết trước để đo thời gian.
- Kaggle: cùng gói nhập hai lần, gói thiếu/hỏng, checksum sai, khác revision, đường dẫn độc hại, giới hạn giải nén.
- Persistence: draft V2 cũ, reload khi job đang chạy, khởi động lại backend, mất file audio, mở dự án khác.
- Benchmark: tập 20 câu khó; mẫu 5–10 phút để đánh giá nghe; fixture timeline 2–3 giờ và ít nhất một lượt sinh dài trên môi trường được chọn. Báo cáo riêng thời lượng phim, thời lượng giọng, thời gian sinh và render.
- Mục tiêu đo ban đầu: đặt audio trong file xuất lệch không quá 20 ms so với timeline sau bù codec; preview khi phát ổn định lệch không quá 80 ms. Đây là tiêu chí cần đo, không phải kết quả đã đạt hoặc bảo đảm khớp khẩu hình.

Kiểm tra Python theo phạm vi thay đổi; frontend chạy các test liên quan, TypeScript/build và lint phù hợp. Khi code chưa đổi, không chạy toàn bộ test chỉ để kiểm tra tài liệu.

## 13. Quyết định mặc định và điều còn thiếu

Mặc định: thử v3 Turbo, một người kể, ưu tiên giọng nữ Bắc kiểu review; chọn preset trước, mẫu riêng khi cần; máy local để chỉnh và Kaggle cho lô dài; giữ lời đọc tách phụ đề; lưu mọi đoạn hoàn thành; không tự đổi model/giọng khi lỗi.

Còn cần trong P0: nghe và chọn giọng thực tế; mẫu giọng hợp lệ nếu preset không đạt; đo hiệu năng máy; kiểm tra GPU/quota hiện có trên tài khoản Kaggle khi sử dụng. Những điểm này không ngăn việc xây schema, manifest và kế hoạch giao diện, nhưng chưa cho phép kết luận giống giọng Vbee hay tốc độ sản xuất.
