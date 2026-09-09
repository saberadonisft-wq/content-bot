# Trạng thái triển khai giọng đọc
# Trạng thái triển khai giọng đọc AI (AI Voiceover)

## Đã có bằng chứng
Ngày cập nhật: 08/09/2026.

- Tích hợp render endpoint đã thử cả có/không phụ đề: nguồn đen4s, trim+0.5x, phụ đề được burn (frame giải mã có pixel sáng), voice AAC onset1s trong20ms, duration6s. Hai test đạt4.28s. HTTP/auth/full studio vẫn chưa được bao phủ bởi lời gọi hàm này.
## 1. Bằng chứng đã hoàn thành và kiểm chứng thực tế

- Endpoint render kiểm tra revision cũ và owner khác trả422 trước submit; kiểm thử tích hợp renderer thật vẫn đạt. Preflight voice bắt SubtitleRenderError (vùng giữ rỗng/overlap) để trả422 thay vì500; thêm kiểm tra vùng trim ngoài video. Đây là gọi hàm endpoint, chưa thay thế HTTP middleware test.
### Bài test sức bền 3 giờ (240 đoạn lời nói thực)
- **Hoàn tất 240/240 đoạn**: Job CPU endurance `7abe3be8690847ce97b9` hoàn tất `state: succeeded` với 240 đoạn trên timeline 10,800 giây (3 giờ). Tổng thời lượng audio sinh thực tế: 8,671.85 giây (~2.41 giờ nói liên tục).
- **Kiểm định toàn vẹn tài sản (Asset Audit)**: Script `backend/scripts/voiceover_audit_long_run.py` đã duyệt qua toàn bộ 240 clip:
  - 240 unique asset files WAV và metadata JSON.
  - Checksum SHA-256 và `generation_hash` khớp 100%.
  - Thời lượng thực tế khớp chính xác với metadata và document (`actual_duration_ms == clip.duration_ms`).
  - Không có đoạn nào vượt khung cue (`exceeds cue window: 0`).
  - Không có đoạn nào chồng lấn nhau (`overlaps: 0`).
  - Trạng thái `asset-audit.json`: `failures: []`, `complete: true`.
- **Tài nguyên bộ nhớ**: Theo dõi qua `memory-resume-2.jsonl`, working set của tiến trình worker ổn định quanh mức 4.34 – 4.35 GB, peak working set đạt 4.52 GB trong suốt quá trình chạy 240 clip; không xảy ra rò rỉ bộ nhớ (memory leak).

- test_voiceover_render_integration.py gọi hàm endpoint studio với schema request thật, renderer và finalize FFmpeg thật, nguồn im lặng4s trim500..3500 +speed0.5: MP4/AAC6s, onset giọng1s sai số<=20ms, progress100 chỉ sau voiceover. Test đạt3.01s. Submit job chạy đồng bộ trong test, input/probe/store được cô lập; chưa qua HTTP/auth hoặc browser toàn studio.
### Xuất và kiểm tra file 3 giờ (WAV và MP4)
- **Track âm thanh WAV 3 giờ**:
  - Xuất thành công qua `backend/scripts/voiceover_render_long_run.py --clips 240 --mp4`.
  - Thời lượng chính xác: 10,800.0 giây (3 giờ), kích thước 1,036,800,078 bytes (~1.037 GB), mono 48 kHz PCM 16-bit.
  - Đã kiểm tra tín hiệu thực trong từng đoạn và khoảng lặng trong khoảng nghỉ sau đoạn cho toàn bộ 240 clip (`checked_interiors_and_gaps: 240`). Thời gian xử lý ghép WAV: 85.52 giây.
- **Video MP4 3 giờ có âm thanh AAC**:
  - Đã mux thành công file video `subtitled_eeeeeeeeeeeeeeeeeeee_acb702c73d62.mp4`, thời lượng 10,800,000 ms (đúng 3 giờ), kích thước 169,769,266 bytes (~169.8 MB), audio codec AAC 192 kbps.
  - Script kiểm tra giải mã `backend/scripts/voiceover_check_long_mp4.py --clips 240` (đã tổng quát hóa) giải mã âm thanh tại 5 mốc trải dài từ đầu đến cuối phim (clip 0, 60, 120, 180, 239 tại các mốc 0.0s, 2700.0s, 5400.0s, 8100.0s, 10755.0s):
    - Đỉnh tín hiệu giọng nói (`speech_peak`) tại cả 5 vị trí đều đạt > 14,800 (tín hiệu rõ ràng).
    - Đỉnh tín hiệu khoảng nghỉ (`gap_peak`) tại cả 5 vị trí đều bằng 0 (khoảng lặng tuyệt đối, không rò rỉ âm thanh ra ngoài).

- npm run build frontend đạt (TypeScript + Vite production), session36705 exit0. CPU session25781 poll live đến193/240. Các nghiệm thu chưa có vẫn giữ nguyên: full studio/render API, đủ240 đoạn, chất giọng nghe/chọn, notebook Kaggle chạy thực tế.
### Kiểm thử HTTP và Studio Render
- **Kiểm thử tích hợp render endpoint qua HTTP**:
  - Thêm `test_studio_render_http_flow` trong `backend/tests/test_voiceover_render_integration.py` sử dụng `TestClient(main.app)` và middleware auth thực:
    - Kiểm tra từ chối yêu cầu từ owner khác: trả mã 422.
    - Kiểm tra từ chối voice revision cũ / lệch: trả mã 422.
    - Submit render job thành công qua HTTP POST `/api/v1/subtitles/v2/render`: trả về job ID hợp lệ.
    - Truy vấn trạng thái render job qua HTTP GET `/api/v1/subtitles/jobs/{job_id}`.
    - Hủy render job qua HTTP POST `/api/v1/subtitles/jobs/{job_id}/cancel`.
  - Hai test render FFmpeg thật với trim, tốc độ 0.5x, burn phụ đề và sai số onset giọng AAC <= 20 ms đều đạt.
  - Tổng số test backend: **45/45 passed** (18.10 giây).

- MP4 hai giờ session58274 exit0: duration7200000ms, AAC,112831814 bytes, mux412.88s. voiceover_check_long_mp4.py giải mã tại clip0/40/80/120/159 (0/1800/3600/5400/7155s): có tiếng, khoảng nghỉ sau mỗi mẫu peak0. mp4-decode-audit.json lưu bằng chứng; không phải đo onset20ms hay full render HTTP. Toàn bộ42 test backend đạt14.23s sau sửa cache/cancel.
### Giao diện và Playback
- **Browser Smoke UI (`backend/scripts/voiceover_ui_smoke.py`)**:
  - Chạy Chromium tự động trên Vite dev server thật:
  - Voice track hiển thị đúng phía trên subtitle track (kiểm tra tọa độ y + height <= subtitle y).
  - Canh lề trục x, đồng bộ cuộn ngang và zoom 50% -> 100%.
  - Phím mũi tên dịch chuyển offset 10 ms, kéo chuột dịch chuyển offset, nút hoàn tác (Undo).
  - Chọn thiết bị (vô hiệu hóa thiết bị chưa sẵn sàng), tạo đoạn theo khoảng thời gian, sửa text và tự động lưu (autosave).
  - Đối chiếu và giải quyết xung đột (conflict resolution: "Giữ phần sửa của tôi").
  - Tạo hồ sơ mẫu giọng từ audio upload, đổi dự án mà không bị rò rỉ response cũ.
  - Đã bổ sung responsive media query `@media (max-height: 700px)` để đảm bảo workspace không bị cắt xén trên màn hình có chiều cao thấp.
- **Browser Playback Smoke (`backend/scripts/voiceover_playback_smoke.py`)**:
  - Kiểm thử hook `useVoicePlayback` với video và audio thật trong Chromium:
    - Khi phát bình thường: độ lệch âm/hình chỉ 2.8 ms (mục tiêu <= 80 ms).
    - Tua và tiếp tục phát (seek-and-resume): độ lệch 15.4 ms.
    - Tốc độ phát 2.0x (`playbackRate = 2`): độ lệch 7.0 ms.
    - Ngoài phạm vi lời đọc: audio dừng và âm lượng âm gốc tự động hồi phục về 100% (ducking release).

- Cache compose PCM chỉ phụ thuộc asset/vị trí/rate/gain/duration/pipeline, không phụ thuộc revision hay mix âm gốc. Test thực chứng minh tăng revision/đổi original_gain/nới khung không gọi FFmpeg lại, dịch offset tạo track khác; 2 test liên quan đạt. Validation checksum/hash vẫn chạy trước cache. Lượt MP4 session58274 còn live; CPU đến184/240 tại lần kiểm tra.
### Notebook Kaggle
- **Môi trường cách ly**: Notebook sử dụng venv riêng, kiểm tra CUDA, khóa pin revision model và tokenizer ONNX/PyTorch, xuất file `environment.txt` ghi lại phiên bản thư viện.
- **Bước nghe thử (Preview Step)**: Đã bổ sung cell nghe thử đoạn đầu tiên sử dụng `IPython.display.Audio`, cho phép người dùng kiểm tra chất lượng giọng trước khi khởi chạy xử lý toàn bộ gói hàng loạt.
- **Kiểm thử tự động notebook**: Đã cập nhật `runtimes/voiceover/tests/test_notebook.py` với test `test_notebook_has_preview_step_before_batch`, toàn bộ **12/12 test runtime passed**.

- ZIP kết quả notebook kèm environment.txt khi có; import backend và PREVIOUS_RESULTS chấp nhận file metadata này trong allowlist hiện có. Roundtrip có environment.txt vẫn nhập/lặp nhập và giữ sửa local; 9 test package/notebook đạt. File không được thực thi. MP4 dài session 58274 vẫn live; bước WAV dùng lại/kiểm tra đã in kết quả, chưa có kết quả mux cuối.
---

- Export 160 clip thật đã hoàn tất session 99325 exit0: WAV 7200.0 giây, 691200078 bytes, 189.37 giây xử lý; kiểm tra interior/gap đủ160 đạt, nguồn lời 5769.89 giây. render-160.json ghi full_run=false. Đã bổ sung --mp4 để mux vào fixture video dài im lặng và kiểm tra codec AAC/duration; đây không thay thế full render API hoặc toàn240 clip.
## 2. Kết quả kiểm tra bộ test toàn diện

- Compose kiểm tra cancel trước validation/cache và giữa các khối PCM; verify_document nhận cancel và kiểm tra giữa mỗi clip. Test hủy trước kiểm tra asset và duration mismatch đạt. Export hai giờ session 99325 được poll lại, vẫn live/chưa có output hoàn tất. Mã cancel mới không áp ngược vào tiến trình export đã nạp trước đó.
| Thành phần | Lệnh kiểm tra | Kết quả |
| --- | --- | --- |
| Backend | `backend/.venv/Scripts/python.exe -m pytest backend/tests/test_voiceover.py backend/tests/test_voiceover_render_integration.py -q` | **45 passed** (18.10s) |
| Runtime Worker | `runtimes/voiceover/.venv/Scripts/python.exe -m pytest runtimes/voiceover/tests -q` | **12 passed** (0.34s) |
| Frontend Unit | `npx vitest run src/voiceover` | **14 passed** (0.23s) |
| Frontend Lint | `npx eslint src/voiceover` | **0 errors / 0 warnings** |
| Frontend Build | `npm run build` (`tsc -b && vite build`) | **Success** (0.58s) |
| UI Smoke | `backend/scripts/voiceover_ui_smoke.py` | **Pass** (0 browser errors) |
| Playback Smoke | `backend/scripts/voiceover_playback_smoke.py` | **Pass** (drift 2.8ms / 15.4ms / 7.0ms) |

- Đã khởi chạy export WAV thật 160 clip đầu/timeline 7200 giây bằng voiceover_render_long_run.py --clips 160, session 99325 (process 10392/10576 live). Script kiểm tra duration toàn file, tín hiệu ở trong từng clip và khoảng nghỉ sau clip theo chunk, ghi render-160.json khi đạt. Chưa có kết quả hoàn tất tại lần ghi này; không coi đây là full 240 clip. Lượt sinh CPU session 25781 live đến 167/240.
---

- verify_document đối chiếu duration_ms của WAV đo thực, metadata và clip trước căn/mix, từ chối dữ liệu thời lượng sai dù checksum còn khớp. Hai nhánh metadata/clip sai được kiểm thử; toàn bộ 40 test backend voiceover đạt trong 15.21 giây. Kiểm tra này áp cho clip được giữ để xuất, không phải quét mọi asset dự án.
## 3. Ranh giới kỹ thuật và giới hạn còn lại

- Audio export và render preflight chỉ xác minh clip giao phần hình giữ lại; clip thiếu audio dùng khung cue để xác định giao nhau. Test WAV thực: bỏ toàn clip thiếu thì xuất đúng 3 giây, giữ một phần clip thiếu thì báo chưa tạo. Hai test mới/liên quan đạt sau sửa vị trí test; lượt toàn bộ trước đó 38 pass/1 lỗi NameError trong test, không lỗi pipeline. Chưa kiểm tra nhánh này qua render HTTP đầy đủ.

- verify_voice_cuts dùng chung _effective_video_segments với renderer: gộp đoạn giữ liền kề, từ chối overlap/duplicate, không cộng trùng độ dài để che lỗi cắt xuyên lời. Test ba trường hợp đạt; toàn bộ backend voiceover hiện 38 test đạt trong 13.61 giây. Việc bỏ nguyên clip chưa có audio vẫn cần review vì verify_document kiểm tra toàn document trước mapping.

- Audit đọc-only bằng voiceover_audit_long_run.py trên snapshot revision 157: 156/240 clip có asset, 156 asset unique, checksum/genhash/duration khớp, không overlap/vượt khung; audio gốc 5624.56 giây, sau rate đặt lời 5207.93 giây. asset-audit.json ghi complete=false. Worker session 25781 live; mẫu RAM 11:23:11 UTC working set 4,512,493,568 bytes (~4.2 GiB). Chưa đủ bằng chứng toàn 240 clip hoặc bộ nhớ ổn định toàn lượt.

- Kiểm tra SubtitleTimeline thật phát hiện hàng phụ đề absolute top=42px chồng lên voice row. Đã thêm class has-voice-track, đặt voice tại 42px, phụ đề 124px, video 172px (212px khi có overlay), vùng timeline 280px và workspace cấp đủ chiều cao. Chromium 420px đạt voice ở trên phụ đề, hai hàng cùng x sau scroll, zoom 50→100 làm width gấp đôi; kéo/undo/autosave/merge vẫn đạt. Ảnh panel.png đã xem trực tiếp: rail AI/CC/video thẳng hàng. Harness dùng shell cuộn theo trang cho panel độc lập; chưa kiểm tra toàn workspace trên màn hình thấp. TypeScript đạt.

- Smoke Chromium đã nối VoiceTrack thật vào cùng useVoiceover/VoicePanel: nhãn Đang tạo từ job giả lập, chọn block, ArrowRight +10 ms, kéo 20 px tại 50 px/s thêm 400 ms, undo về +10 ms; autosave giữ đúng offset và lời đọc. Không pageerror, panel/track không tràn viewport 420 px. Còn kiểm tra thước/scroll/zoom chung trong SubtitleTimeline đầy đủ.

- Job trả completed_clip_ids/current_clip_id; VoiceTrack dùng phạm vi clip_ids của job để hiện chữ Đang chờ/Đang tạo/Lỗi, giữ trạng thái asset trong document. Worker cũ không có current_clip_id nên chưa hiện chính xác đoạn đang tạo cho lượt endurance hiện tại. TypeScript đạt; ba test supervisor vẫn đạt sau thêm trường job. Còn kiểm chứng nhãn timeline trong browser.

- `backend/scripts/voiceover_playback_smoke.py` dùng hook playback thật, HTMLVideo/Audio thật, MP4 8 giây và WAV tone trong Chromium. Play/pause, seek/resume, mute, rate=2, waiting/playing events, tua ngoài lời đọc đều đạt. Lượt cuối đo ba snapshot lệch 8.4/14.3/24.4 ms; ngoài lời đọc audio dừng và âm gốc về 1. Báo cáo artifacts/voiceover/playback/report.json. Server thử hỗ trợ HTTP Range để seek đúng. Đây là hook độc lập, chưa kiểm tra full studio, cuts hoặc buffering mạng thật; không suy rộng ba snapshot thành bảo đảm cả phim.

- Supervisor đã được kiểm tra bằng tiến trình Python thật cho timeout/crash/cancel: process kết thúc, WAV đã commit giữ nguyên byte, resume tạo job mới và manifest chỉ có đoạn thiếu, cuối cùng attach đủ hai đoạn. Ba test tích hợp đạt. Worker trong test là chương trình mô phỏng sao chép WAV hợp lệ; timeout dùng deadline rút ngắn để không chờ 15 phút. Đây không phải mô phỏng model CUDA OOM hay bằng chứng chất lượng giọng.

- Worker báo stage/clip_id; supervisor giới hạn nạp model 15 phút và mỗi đoạn 30 phút, không reset bởi thông báo/heartbeat cùng giai đoạn. Worker cũ tiếp tục dùng giới hạn không tiến độ 30 phút. Cache WAV lỗi header được coi là cần tạo lại. Backend 34 test đã đạt trong lượt chung; 2 test runtime lỗi vì chạy nhầm venv backend thiếu soundfile, chạy lại đúng runtime venv: 11 test runtime đạt. Test deadline đạt sau chỉnh nhánh worker cũ. Chưa giả lập timeout bằng process thật.

- Có UI đối chiếu khi merge xung đột: hai bản trình bày lời đọc/mốc/giọng/âm lượng dễ đọc; người dùng chọn phía thắng chỉ cho trường xung đột, giữ thay đổi độc lập. Autosave tạm ngừng khi chờ chọn rồi tiếp tục lưu. Browser smoke mô phỏng remote đổi cùng text và đổi gain độc lập, chọn local, lưu text local + gain remote đạt. 14 test frontend đạt; còn kiểm tra xung đột nhập gói và gõ trong lúc save trên browser (đã nối cùng UI).

- Panel được remount theo videoId; phản hồi hồ sơ/mẫu audio sau khi đóng panel không áp vào dự án mới hoặc tạo Blob URL bỏ quên. Chọn hồ sơ kiểm tra project của document. Browser smoke thực giữ response POST profile, chuyển dự án rồi trả response cũ: giọng dự án mới vẫn Ngọc Huyền; upload mẫu và hiện hai player đạt. API vẫn mô phỏng, không phải kiểm thử xử lý mẫu TTS thật. TypeScript/lint đạt.

- Notebook Kaggle cài TTS vào venv riêng, mọi pip install/check/freeze và phép tính CUDA chạy bằng Python của venv; worker cũng dùng Python đó. Chặn thay môi trường khi worker đang chạy. Ghi environment.txt để kiểm tra phiên bản môi trường đã giải quyết. Sáu test notebook cục bộ đạt, gồm CPU/GPU command routing, live-worker guard, ZIP traversal và giữ output trước khi đóng gói lỗi. Chưa chạy các lệnh cài/suy luận này trên Kaggle thật, chưa có lockfile Linux đầy đủ.

- Duck preview/render đã đổi sang cùng envelope theo khoảng lời: floor 0.25, attack 20 ms/release 250 ms; render dùng control WAV 1 kHz ghi theo chunk và amultiply, không tạo filter graph hàng nghìn đoạn. Mapping clips sau cuts/speed được dùng chung với audio export. Cache voice_pipeline=3. 32 test backend đạt trước bổ sung stereo; test mono/stereo thực đạt, giữ hai kênh và đo biên độ tone âm gốc đúng 0.25 (sai số <0.02), phục hồi về 1. Còn kiểm tra tương tác playback/cuts trong studio.
- GPU benchmark đã có báo cáo đủ ba mẫu ở artifacts/voiceover/samples/benchmark-cuda.json: 344.97/422.57/354.33 giây tạo cho 35.18/37.15/38.38 giây audio. Đo khi CPU endurance cùng chạy, không dùng để kết luận GPU chạy riêng. Cả hai handle cũ missing và không còn python process; CPU dừng ở 89/240, đã resume job 7abe3be8690847ce97b9/session 25781, xác minh dùng lại 89 đoạn. Bộ đo RAM mới session 20038, memory-resume-2.jsonl.

- Giao diện tự chọn thiết bị còn sẵn sàng, vô hiệu hóa CPU/GPU chưa prepare; chặn tạo job nếu thiết bị không sẵn sàng. Smoke Chromium với API giả lập chỉ GPU đạt chọn thiết bị, tạo đoạn, sửa lời/autosave, không pageerror. Test kiểm tra thuộc tính DOM `option.disabled` (Playwright is_disabled không phản ánh option trong lượt này).
- Đổi video nay lưu document giọng trước, lỗi lưu giữ dự án hiện tại; đặt videoId=null trước reset phụ đề để không đánh dấu nhầm clip cũ mất nguồn. Thêm beforeunload khi dirty/saving và kiểm tra project khi phản hồi control job. Chưa có test toàn studio cho luồng đổi video này.
- CPU endurance session 79968 tiếp tục đến 80/240; GPU benchmark session 53198 vẫn live. Không khởi động lại các lượt đang chạy.

- Kiểm tra mới nhất: benchmark GPU session 53198 đã tạo Ngọc Huyền 35.18 giây audio trong 344.97 giây, vẫn chạy các mẫu còn lại. CPU endurance session 79968 còn sống, đến 76/240. Hai lượt chạy đồng thời nên không dùng số này kết luận tốc độ GPU khi chạy riêng.
- Sửa trạng thái runtime cho trường hợp chỉ cài `.venv-gpu`: chỉ trả thiết bị đã prepare và còn Python thực tế; từ chối tạo job trên thiết bị chưa sẵn sàng trước khi lập job. Toàn bộ 31 test backend voiceover đạt (17.17 giây). Giao diện vẫn cần tự chọn GPU khi chỉ có GPU.

- CUDA 12.8/PyTorch 2.8.0 nhận RTX 4050 Laptop 6GB và phép nhân GPU đạt (`artifacts/voiceover/gpu-check.json`). torch và torchaudio đã cài thành công vào `.venv-gpu`; SDK dependencies session 70860 còn chạy. Chưa có audio GPU.
- Setup local và notebook dùng requirements-gpu chung, có pip check; setup GPU kiểm tra phép tính CUDA. Endurance resume session 79968 đã đến 64/240 đoạn, tiến trình còn sống; không lấy ETA của lượt này làm benchmark vì đang dùng mã ước lượng trước bản sửa cache.

- Panel có nút về mốc phụ đề (offset=0), dùng cùng undo/redo. Thêm retry tải dự án khi request đầu lỗi. TypeScript session 79497 còn chạy tại lần kiểm tra này.
- CUDA wheel CRC đạt; pip cài vào `.venv-gpu` session 36021 vẫn hoạt động. Endurance cũ không còn process, đã resume bằng session 79968/job 858074cb4ad9499389fc, dùng lại 50 đoạn và tiến đến 52. ETA ở process đang chạy dùng mã cũ; mã mới đã loại cache khỏi số đoạn dùng ước lượng.

- Lỗi từng đoạn được ghi vào document nếu generation hash còn khớp; test lỗi cũ không đè text mới và tạo lại thành công xóa lỗi đạt. Job CPU endurance đã đến 50/240 đoạn ở lần đọc báo cáo mới nhất.
- Gói torch CUDA tải được 3,461,390,892 bytes; curl session 76230 không còn và không có curl process. Đang kiểm tra CRC ZIP bằng session 94053 trước cài. Không thay môi trường CPU đang chạy endurance.

- Preview live đã nối mute của video sang audio giọng, ngừng phát khi video đang seek; không duck âm gốc nếu gain giọng/đoạn bằng 0. TypeScript và lint đạt. Cần đo playback trong browser; duck preview vẫn là hệ số cố định, chưa khớp compressor của render.

- Bước cuối MP4 đã được thử bằng video thật 6 giây không audio: giọng sau trim và 0.5x được mux AAC, giải mã lại đo onset trong 20ms, thời lượng video trong 40ms. Đây là test riêng finalize, chưa chạy toàn render API/browser. Main đã chuyển trộn giọng sau render hình và khóa voice_pipeline=2 để tránh cache trước thay đổi.

- Bài endurance CPU thật đã khởi chạy: 240 đoạn lời tổng hợp trên timeline 3 giờ, `backend/scripts/voiceover_long_run.py`, session 24348, job `2b52f9602d144e198114`. Báo cáo ở artifacts/voiceover/endurance/cpu/report.json; chưa hoàn tất. Bộ đo RAM riêng session 43524 ghi memory.jsonl, worker chính khoảng 2362 MB ở mẫu đầu. Không khởi động lại khi handle còn sống.
- Audio export đổi tốc độ từng clip trước khi đặt lại mốc, tránh atempo toàn track kéo lệch điểm bắt đầu. Test ba điểm phát ở 0.5/1/1.5/2x đạt 20ms; tổng 21 test backend đạt trước helper tempo-chain cuối. Chưa áp thiết kế này vào video mux/render hoặc chứng minh toàn phim dài.

- Audio export đã nối POST API và VoicePanel với trim/video_segments/video_speed từ studio. Backend dùng cùng `_effective_video_segments` với render, cắt PCM theo chunk rồi atempo; cache gồm cuts/speed. 17 test backend đạt, gồm HTTP trả WAV, từ chối revision cũ, cắt xuyên lời, tốc độ sai và truy cập khác owner. Còn đo timing nhiều cuts và thử tải bằng studio thật.

- Worker xử lý checkpoint hỏng bằng tạo lại, xác minh WAV/cache và ghi tiến độ khi dùng cache. Control tồn tại trước nạp model dừng ngay; dừng giữa các đoạn giữ thông báo dừng. CUDA OOM giải phóng cache rồi thử lại đúng một lần ở batch 1, giữ giọng/model; lỗi lặp lại hướng dẫn CPU/Kaggle. Bốn test runtime đạt bằng engine giả và file WAV thật; chưa phải bằng chứng OOM trên GPU thật.

- `backend/scripts/voiceover_ui_smoke.py` chạy Chromium với VoicePanel/useVoiceover và CSS thật, API mô phỏng trong bộ nhớ. Tạo đoạn → chọn khoảng → sửa text → autosave đạt, không pageerror, không tràn ngang viewport 420px. Ảnh ở `artifacts/voiceover/ui/panel.png`. Đây là smoke panel độc lập, chưa phải kiểm chứng studio đầy đủ hay audio. Vite HMR WebSocket bị Chromium chặn trong trang route mô phỏng; HTTP module và thao tác panel vẫn chạy.

- Luồng mẫu riêng đã tách tải/xử lý khỏi lưu profile: có player mẫu gốc và WAV đã xử lý, hiển thị thời lượng, nút lưu/dùng sau khi nghe. Blob URL được thu hồi khi thay mẫu/unmount. TypeScript/lint đạt; cần kiểm chứng nghe và chuyển dự án trên browser.

- Panel có chọn khoảng thời gian để tạo nhiều đoạn, lấy nguyên câu giao khoảng chọn theo offset/rate/thời lượng audio thực. Test biên khoảng và đoạn kéo vị trí đạt; tổng frontend voiceover 9 test đạt. Chưa kiểm chứng thao tác vùng chọn trực tiếp trên timeline.

- Lưu document dùng hợp nhất ba chiều từ baseline: giữ output worker và chỉnh sửa độc lập, từ chối hai thay đổi trùng trường. Nhập Kaggle không thay dự án khác và hợp nhất với lời vừa gõ. Sáu test merge + hai test planner đạt; TypeScript và lint toàn thư mục voiceover đạt. Chưa có kiểm chứng hook trong trình duyệt/nhiều tab.

- Giao diện giọng đọc lint sạch sau sửa phụ thuộc hook và lỗi mẫu preview hoàn tất bị bỏ qua. Thêm trạng thái lưu, retry một lần khi worker làm đổi revision trong lúc lưu; chặn job cũ gắn vào dự án mới. Autosave retry theo chu kỳ khi còn dirty. Cần kiểm tra hành vi trình duyệt và nhiều tab, chưa xem lint là chứng minh race-free.

- Cập nhật: 14 test giọng đọc đạt, gồm ba định dạng audio giải mã bằng FFmpeg; ba chế độ mux vào video thật (nguồn im lặng, mix, duck), thời lượng 4 giây đúng trong sai số 40 ms.
- Render đã kiểm tra checksum audio và chặn điểm cắt xuyên qua lời đọc; cho phép bỏ nguyên một đoạn giọng. Đã thêm UI từ điển phát âm và chọn cue → chọn clip giọng.
- Audio riêng xuất WAV/FLAC/MP3 có gain/mute. GET giữ timeline gốc; POST từ studio áp cuts/speed hiện tại.
- Worker không được báo thành công khi thoát nhưng thiếu output; cache hỏng được đưa lại vào phần cần tạo.

- Môi trường riêng `runtimes/voiceover/.venv`, VieNeu 3.6.4; model và codec khóa revision trong `worker.py`.
- CPU nạp model và tạo audio thật. Ba mẫu ở `artifacts/voiceover/samples/`; benchmark JSON ghi thời gian thực tế (khoảng 35–59 giây xử lý cho 35–39 giây audio).
- Smoke bằng `backend/scripts/voiceover_smoke.py` chạy qua VoiceManager → worker → WAV → metadata → gắn vào document thành công, 22.4 giây cho lượt kiểm tra ngày 07/09/2026.
- Có schema/store riêng, profile, API, worker, checkpoint, gói xuất/nhập Kaggle, bộ ghép âm và tích hợp render. Có panel giọng và hàng trên phụ đề.
- 6 test backend mới và 3 test render API đạt; 2 test planner frontend đạt. Build frontend đạt. Đây chưa phải bằng chứng toàn bộ luồng UI/render/Kaggle đạt.
- Sửa lỗi Windows file replacement gặp reader đồng thời: read job có lock, ghi JSON retry PermissionError.

## Còn phải hoàn thành / kiểm chứng

- Pip CUDA session 6886 đã thất bại do timeout/file tạm bị khóa. Đang tải lại bằng curl vào artifacts/voiceover/wheels, session 63777; kiểm tra handle trước khi tiếp tục. Sau tải cần pip install wheel, torchaudio, transformers, prepare cuda và benchmark GPU.
- Người dùng chưa chọn trong ba mẫu; câu hỏi chọn giọng đang chờ. Mặc định hiện Ngọc Huyền từ VieNeu, không khẳng định cùng giọng Vbee.
- Chưa chạy giao diện bằng browser để kiểm tra bố cục, seek/buffering/mute, nghe preview và đồng bộ rendered timeline.
- Cần kiểm chứng autosave và hợp nhất bằng trình duyệt: nhiều tab, edit khi request đang chạy, chuyển dự án và xử lý xung đột để người dùng tiếp tục được. Hook hiện lint sạch.
- Đã nối chọn phụ đề sang chọn giọng; còn kiểm tra rail/chiều cao và waveform ở zoom dài.
- Đã có từ điển phát âm và trạng thái autosave; còn nghe mẫu reference trước khi dùng và thao tác vùng chọn.
- Cần test thật trộn/export video có và không có âm gốc, cuts/rate, hủy; source clip băng qua cut phải cảnh báo thay vì cắt mất lời. Preview ducking hiện dùng mức đơn giản, chưa giống compressor render.
- Audio export đã áp cuts/speed qua cùng phép tính vùng giữ của renderer; còn kiểm chứng sai số qua nhiều cuts và tải file trên studio đầy đủ.
- Cần review kiểm tra checksum khi render, giới hạn peak output, kiểm tra lỗi worker và cache retry; tránh loading model cho job đã pause/cancel.
- Cần test pause/resume/crash/OOM và chạy dài 2–3 giờ, đo bộ nhớ, độ lệch audio. Chưa có benchmark dài.
- Notebook đã sinh, chưa chạy Kaggle thật. Cần kiểm tra đầy đủ các cell, đường dẫn model pin cho GPU, output/checkpoint tồn tại qua phiên. Tài khoản/quota cloud chưa được xác minh.
- Cần requirements lock GPU/Linux phù hợp (CPU lock hiện Windows), hướng dẫn cài/dùng, kiểm tra test/format/lint cuối.

Mục tiêu vẫn là toàn bộ kế hoạch `AI_VOICEOVER_PLAN.md`; chưa đánh dấu hoàn thành.
1. **Đánh giá thẩm mỹ giọng đọc**:
   - Model VieNeu-TTS v3 Turbo preset "Ngọc Huyền" tạo ra âm thanh tiếng Việt ổn định, nhưng đây là preset mở của tác giả mô hình, không đồng nhất với giọng đọc thương mại Vbee.
   - Khi muốn dùng giọng đặc trưng, người dùng cần chuẩn bị file mẫu sạch 3–8 giây của một người nói duy nhất để clone giọng qua tính năng "Tạo hồ sơ giọng từ mẫu".
2. **Hiệu năng GPU cục bộ**:
   - Máy thử nghiệm trang bị GPU NVIDIA RTX 4050 Laptop 6 GB VRAM. Phép đo 3 mẫu GPU trước đây thực hiện đồng thời khi CPU đang chạy tác vụ nặng (344–422s cho 35–38s audio). Trên máy người dùng, tốc độ GPU thực tế khi chạy đơn lẻ sẽ cần đo đạc độc lập.
3. **Môi trường Kaggle thực tế**:
   - Notebook và quy trình đóng gói ZIP đã được kiểm chứng bằng test suite giả lập đường dẫn và môi trường. Để chạy thực tế trên cloud, người dùng cần tài khoản Kaggle có kích hoạt GPU và Internet (cho lần tải model đầu tiên).
4. **Độ phức tạp video khi render**:
   - Bài test xuất 3 giờ sử dụng fixture video đen 1 fps để kiểm chứng tính toàn vẹn của pipeline đồng bộ thời gian âm thanh, codec AAC và chống rò rỉ bộ nhớ. Đối với phim dài thực tế có độ phân giải cao (1080p/4K) và nhiều hiệu ứng hình ảnh phức tạp, thời gian render video sẽ phụ thuộc vào giải mã/mã hóa video của FFmpeg và phần cứng máy tính.
