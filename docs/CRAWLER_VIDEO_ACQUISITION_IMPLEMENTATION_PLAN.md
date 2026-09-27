# Kế hoạch cào dữ liệu và tải video theo hướng ViralCrawl

Ngày lập: 2026-09-21. Trạng thái: đang triển khai theo phase, chưa hoàn tất toàn bộ scope.

## Cập nhật triển khai 2026-09-23

- Metadata yt-dlp của acquisition chạy trong subprocess riêng với JSON protocol, deadline supervisor và hard-stop khi pause/cancel; worker không nhận cookie hoặc signed URL qua argv/log. Bộ test mới xác nhận protocol, cancel và deadline cleanup.
- Downloader chỉ publish file sau khi kiểm tra extension/size và dùng ffmpeg decode ít nhất một video frame; HTML, file rỗng, audio-only hoặc file không có video stream không được ghi nhận là thành công. Bổ sung timeout 30 giây cho bước kiểm tra.
- Nhóm regression acquisition/download/batch/storage hiện đạt `149 passed` ở lượt chạy lại. Frontend có mốc unit `133 passed` trước thay đổi hiển thị lịch; build/lint và browser smoke desktop/mobile đã chạy lại sau thay đổi cào tiếp. Đây là evidence kỹ thuật local, chưa thay thế live gate/auth cho từng provider.
- Canary live sau thay đổi vẫn đạt cho YouTube `dQw4w9WgXcQ` (480p, publication `succeeded`, 11.8 MB) và Bilibili `BV1xx411c7mD?p=1` (480p, publication `succeeded`, 74.9 MB). Acquisition E2E YouTube cũng đạt `run completed → candidate có title → selection completed` trong thư mục tạm; dữ liệu canary đã được dọn sau khi chạy.
- Acquisition E2E mới xác nhận job thành công giữ provenance `source_id=youtube`, `provider_id=yt-dlp`, `external_id`, `media_id`, `creator_id`, uploader và canonical `source_url`; publication vẫn kiểm tra được qua `job_is_published`.
- UI Cào video đã cho phép chọn ngân sách preview `20/50/100` metadata và khôi phục lựa chọn này khi mở lại run; backend vẫn clamp và áp dụng budget độc lập.
- Bilibili CBCE detail/search/creator được bọc bởi cancel/deadline monitor chung; khi run bị hủy hoặc quá hạn, async task được cancel để supervisor thu hồi browser worker thay vì chỉ đổi state ở parent.
- Subscription finalizer hiện có reconciliation lease atomically riêng trên SQLite/Mongo và bước complete compare-and-set; hai process không cùng finalize một run terminal, lease hết hạn cho phép replay sau crash, còn thao tác tắt subscription không bị snapshot cũ ghi đè.
- Reconciliation lease có token riêng cho mỗi lần claim; commit cuối phải khớp token và còn hạn. Test SQLite dùng process `spawn` thật xác nhận một winner cho schedule/reconciliation/selection và từ chối commit của process cũ sau takeover. Mongo hiện kiểm chứng contract bằng mongomock; chưa có bằng chứng Mongo server đa process thật. Token chỉ bảo vệ commit cuối và kiểm tra trước cấp tải, chưa phải transaction cho mọi observation/outbox write.
- UI kênh nguồn tự cập nhật mỗi 15 giây, hiển thị trạng thái/lỗi/lần quét gần nhất/lịch tiếp theo; mở được run theo ID ngay cả khi không nằm trong 50 run gần đây. Có nút xem thêm sau sáu kênh và thông báo dữ liệu có thể cũ khi mất kết nối. Browser smoke fixture đã kiểm tra khôi phục polling, bảy kênh, timezone khi mở lại bộ lọc, giữ selection qua hai trang candidate và layout 1440/390 px; chưa thay thế nguồn thật cập nhật video mới.
- Danh sách 2–20 creator/playlist đã có parent/child run bền vững; chạy tuần tự trong một executor slot, dùng chung trần metadata/deadline và chia phần cho từng kênh còn lại. Mỗi child chọn provider riêng; nguồn không hỗ trợ hoặc lỗi không làm mất preview của nguồn đạt. Parent trả `partial` khi còn child lỗi/chưa quét; retry giữ child hoàn tất và ID cũ, pause/cancel đi qua parent. Test phục hồi crash sau child publication trước parent commit đạt; chưa chứng minh mọi điểm crash giữa từng item.
- UI hiển thị từng child, thử lại kênh lỗi và chuẩn bị lượt mới cho kênh bị bỏ qua do hết ngân sách. Thao tác chuẩn bị không tự cào. Đạt giới hạn candidate ghi `candidate_budget`, không coi đó là đã duyệt hết kênh. Nút cào tiếp bằng cursor trong cùng kênh đã được triển khai cho YouTube creator/playlist một target.
- YouTube creator/playlist và Bilibili playlist một target đã có cào tiếp theo cửa sổ vị trí: cursor bền vững gồm `next_offset`, `generation`, `seen_ids`, `exhausted`; mỗi lượt giữ candidate cũ, chống trùng theo ID và có CAS cho thao tác lặp. Cursor lặp dừng sau hai cửa sổ không có ID mới; giới hạn 5.000 vị trí mỗi lượt được ghi rõ trong UI. Đây là cursor theo danh sách có thể thay đổi, không cam kết snapshot nhất quán.
- Live canary nhóm YouTube `@GoogleDevelopers/videos` + `@YouTube/videos` đạt `completed`, 40 candidate (20/kênh), title/canonical URL đầy đủ, 6,68 giây; không dùng cookies, không tải file, SQLite tạm được dọn sau kiểm thử. Đây là một lượt metadata, chưa đủ gate 3 lượt/operation hoặc chọn tải 5 video/kênh.
- Live canary cào tiếp YouTube `@GoogleDevelopers/videos` đạt lượt đầu 20 → sau một lần cào tiếp 40 candidate, title/canonical URL hợp lệ, 6,29 giây, dừng với `candidate_budget`; không tạo download và SQLite tạm đã được dọn. Đây mới là một target công khai, chưa thay thế gate nhiều lượt/connection.
- Bilibili playlist public đã được mở trong acquisition qua yt-dlp + enrichment metadata bounded: ba playlist công khai đạt 7, 2 và 20 candidate có title/canonical URL; continuation live `10 → 20` candidate trên `ml1103317812` đạt `candidate_budget`, không tạo download, không dùng cookie. Creator Bilibili vẫn chờ CBCE/browser live gate.
- `scripts/acquisition_batch_canary.py` hiện có `--connection-id` opt-in cho target Bilibili, khởi tạo CBCE connector/profile tương ứng, chỉ trả metadata và chỉ báo boolean profile đã dùng; một public playlist rerun sau thay đổi đạt 2 candidate hợp lệ trong 2,62 giây. Chưa chạy profile live vì cần connection đã đăng nhập.
- Download bridge đã ghi `intent_key` bền vững vào job JSON trước khi worker chạy; acquisition dispatcher truyền khóa khi cấp job và downloader tái sử dụng đúng job sau restart nếu DB chưa kịp ghi `job_id`, đồng thời từ chối tái sử dụng khóa cho media/quality/profile khác.
- Selection idempotency đã chuyển xuống storage reservation: SQLite dùng transaction `BEGIN IMMEDIATE`, Mongo dùng unique partial index và atomic upsert; selection dở dang sau crash có thể tiếp tục tạo intents thay vì trả về bản ghi rỗng.
- Downloader JSON đã có lock cấp process trên `downloads/jobs`, refresh merge không thay object đang được worker giữ, schema version tương thích job cũ và publication manifest để phục hồi crash sau khi move file nhưng trước khi ghi `succeeded`; đã có test process thật cho race cùng `intent_key`.
- Tải trực tiếp từ link đã có API/UI pause-resume cùng job ID; pause giữ staging/checkpoint và resume dùng lại luồng retry, còn cancel vẫn là kết thúc rõ ràng.
- Subscription run bị gián đoạn bởi application restart hiện được nhả lease và xếp catch-up ở tick kế tiếp, không biến thành lỗi chờ 30 phút; baseline/observation vẫn giữ nguyên để không tải lại lịch sử.
- TikTok Display đã được nối vào acquisition ở mức metadata-only cho video detail và creator đã cấp quyền; capability vẫn `unverified`, không có global search/playlist/media download, và candidate được đánh dấu không thể tải thay vì fallback sang yt-dlp.
- Douyin search metadata đã nối qua `cbce_douyin` khi reviewed DOM contract sẵn sàng; operation vẫn `unverified`, chưa mở detail/creator/download và candidate không được fallback sang yt-dlp.
- Live probe Douyin search ngày 2026-09-23 qua `cbce_douyin` (query `猫`, 3 item, deadline 60 giây) kết thúc `DEADLINE_EXCEEDED`, 0 item/0 metadata; vì vậy chưa đạt live gate và vẫn giữ `unverified`.
- Capability response hiện ghi rõ implementation/version, target kind, auth basis, coverage, schedule policy (`background_safe`/`manual_only`), budget và thời điểm kiểm tra cho từng operation; scheduler tiếp tục chỉ claim operation `ready`.
- Rollout guard đã có với `CONTENT_BOT_ACQUISITION_ENABLED`: capability trả trạng thái cờ, UI vô hiệu hóa thao tác tạo mới khi cờ tắt, backend chặn run/selection/resume và scheduler; list/cancel cùng thao tác tắt subscription vẫn được giữ để rollback an toàn.
- Subscription run đã nhận trước lúc tắt flag không làm mất video mới: auto-download được ghi vào `deferred_auto_download_ids`, giữ `next_run_at` để catch-up và cấp lại đúng candidate khi flag bật trở lại.
- Thư viện kênh video đã có vòng đời đầy đủ hơn: UI/API `PATCH` đổi nhãn/connection, UI/API `DELETE` bỏ theo dõi theo soft-delete idempotent; run, candidate, selection và file đã tải không bị xóa, kênh đã bỏ có thể thêm lại theo canonical URL.
- UI Cào video đã expose các filter metadata backend đang hỗ trợ: tiêu đề chứa, thời lượng min/max và ngày đăng sau; filter được gửi cùng run và khôi phục khi mở lại run cũ.
- Lỗi admission queue hiện trả `retry_after` cùng các field ngữ cảnh tùy chọn `run_id`/`job_id`; queue-full mặc định hướng client thử lại sau 60 giây.
- Lỗi provider `RATE_LIMITED` giờ trả HTTP 429, truyền `retry_after_seconds` an toàn vào contract acquisition và HTTP `Retry-After`; nếu provider không gửi thời điểm reset thì dùng gợi ý 60 giây, lưu lại trên run/child run failed và UI hiển thị hướng dẫn thử lại. Giá trị được làm tròn và chặn tối đa 7 ngày.
- Subscription thất bại vì provider rate limit không còn luôn chờ cứng 30 phút: `next_run_at` tôn trọng cooldown đã chuẩn hóa, còn lỗi tạm thời không có cooldown cụ thể vẫn giữ backoff 30 phút.
- Lớp điều phối metadata đã retry riêng `RATE_LIMITED` tối đa 3 attempt tổng, dùng chung deadline của run, exponential backoff + jitter và hủy được trong lúc chờ; auth/parser/unsupported không bị retry mù.
- Full backend regression ngày 2026-09-23 đạt `1603 passed, 3 skipped, 23 failed` trong khoảng 380 giây trước patch pause-resume trực tiếp; toàn bộ failure được ghi nhận trong OCR/subtitle hiện đang thay đổi độc lập. Patch pause-resume sau đó đạt targeted regression, không có failure mới trong acquisition/download.
- Live gate YouTube P2 qua acquisition subprocess đạt creator `@GoogleDevelopers/videos` và playlist `PL-osiE80TeTt2d9bfVyTiXJA-UTHn6WwU` ở ngân sách 20: cả hai run `completed`, mỗi run có 20 candidate với title và canonical URL.

## Cập nhật triển khai 2026-09-21

P0/P1 đã được bắt đầu trong code hiện tại:

- Preview video Bilibili có connection hiện dùng detail worker CBCE khi connector sẵn sàng; xử lý danh sách target trong hạn mức và giữ `p` trong URL điều hướng worker/browser. Khi CBCE không sẵn sàng, đường metadata công khai vẫn là fallback hiện tại; chưa chứng minh preview nội dung yêu cầu đăng nhập.
- UI khởi động lại polling sau resume/retry cùng run ID; lỗi mạng khi polling run/selection được thử lại sau 3 giây.
- UI có danh sách 50 lượt cào gần đây lấy từ backend để mở lại sau khi đóng tab; khi chọn lượt sẽ tải lại candidate/trạng thái và phục hồi target/query/connection. Có danh sách nhóm tải đã lưu qua `GET /api/v1/acquisition/download-selections`, mở nhóm để theo dõi/pause/resume/cancel độc lập với run đang xem. Chưa phục hồi lựa chọn checkbox chưa gửi; cookie file cần chọn lại.
- Acquisition giới hạn mặc định 32 lượt metadata đang chạy/chờ, dùng chung admission lock cho create/resume; khi đầy trả `QUEUE_FULL`/HTTP 429. Subscription nhả lease và chuyển `waiting_capacity`, thử lại sau một phút. Pause/cancel giữ trạng thái khi extractor trả lỗi muộn; resume trả `RUN_STOPPING`/HTTP 409 cho tới khi worker cũ đã kết thúc. Metadata subprocess hiện đã có hard-stop khi cancel hoặc vượt deadline.
- Subscription lưu `baseline_at` từ lúc bật theo dõi. ID chưa gặp có ngày đăng trước mốc này là `historical`; thiếu ngày đăng hoặc ngày trong tương lai là `unknown`, không tự tải theo luật new-only. Backfill chỉ cấp tải khi auto-download được bật; replay finalizer tái dùng selection idempotent. Các kiểm thử này là fixture, chưa chứng minh lịch trên kênh thật có bài mới.

- Đã có `AcquisitionManager`, storage document contract cho SQLite/Mongo, run/candidate/selection bền vững và API `/api/v1/acquisition/*`.
- Đã nối tab **Cào video** vào Nguồn & Video: cào preview, chọn candidate, chọn chất lượng/cookies và chuyển selection vào `VideoDownloadManager` hiện có.
- Đã hỗ trợ bước đầu target video/creator/playlist và YouTube search qua provider yt-dlp; Bilibili giữ query `p` trong identity media.
- Đã có lưu kênh/playlist độc lập với keyword, baseline lần quét đầu, observations và scheduler tự phát hiện video mới; auto-download vẫn mặc định tắt.
- Đã có điều khiển selection tải theo nhóm pause/resume/cancel, polling trạng thái và giữ intent/job khi retry.
- Admission tải đã tôn trọng bounded queue 32 slot: selection lớn giữ intent `pending`/`needs_cookies`, tự lấp slot khi job hoàn tất, không biến phần còn lại thành failed giả.
- Đã thêm `GET /api/v1/acquisition/capabilities`; UI đọc capability matrix để chỉ bật Bilibili search khi CBCE/browser đã sẵn sàng, còn operation `unverified`/`unsupported` hiển thị đúng trạng thái. Backend cũng từ chối provider ID không đăng ký hoặc provider không khớp operation.
- Metadata filter của run đã có contract và được áp dụng trước khi tạo run-item (`media_type`, title/creator, duration, published-after); filter lạ không còn bị bỏ qua âm thầm và counter `filtered` được lưu bền vững.
- UI subscription đã expose baseline/backfill, chu kỳ, quality và `max_items`, thay vì chỉ có hai toggle enabled/auto-download.
- Kênh Bilibili đã lưu được `connection_id` đã chuẩn hóa; scheduler truyền lại connection đó vào child run, nên profile đăng nhập không bị rơi khi chạy nền. Kênh YouTube không nhận connection bridge chưa được hỗ trợ.
- Scheduler hiện không claim subscription cho operation capability chưa `ready` (ví dụ Bilibili creator chưa qua session/live gate); trạng thái được ghi là `waiting_capability` và không tạo run giả.
- Đã nối session bridge theo hướng fail-closed cho CBCE Bilibili: `connection_id` được chuẩn hóa thành account reference, profile browser được hash/cô lập theo từng connection, worker/search/creator truyền đúng profile, và login/probe API hỗ trợ query `connection_id`. Download bridge hiện hỗ trợ video Bilibili trực tiếp và selection: job chỉ lưu connection ID an toàn, worker resolve profile lúc chạy, giữ `ProfileLock` và dùng `yt-dlp cookiesfrombrowser` trong subprocess; profile path/cookie không vào job JSON, log hay response. Creator adapter hiện nhận diện login gate thành `AUTH_REQUIRED` thay vì báo nhầm parser hỏng; capability vẫn `unverified` và scheduler chưa claim khi chưa có session live đã kiểm chứng.
- UI Video Library và Cào video đã cho phép chọn connection profile Bilibili; file cookie và profile không được gửi đồng thời. YouTube/nguồn khác vẫn bị từ chối rõ ràng khi gắn profile.
- Target acquisition detail/creator của Douyin, toàn bộ XHS và các nguồn ngoài wave đầu hiện bị từ chối rõ ràng. Douyin search chỉ mở qua reviewed CBCE contract ở trạng thái `unverified`; TikTok chỉ nhận video/creator qua Display API khi account đã cấp quyền. Không provider nào fallback sang yt-dlp chỉ vì hostname được nhận diện.
- Scheduler subscription đã có local admission lock, atomic storage claim có lease hết hạn trên SQLite/Mongo và reconcile lượt terminal trước khi tạo lượt mới; vẫn cần hardening recovery đa process dài hạn.
- Nhóm regression acquisition/download/batch/storage hiện đạt `149 passed`. Frontend build/lint và browser smoke kênh nguồn/nhóm kênh/cào tiếp đạt. Full backend lần chạy ngày 2026-09-23 đạt `1603 passed, 3 skipped, 23 failed` trong khoảng 380 giây trước patch pause-resume trực tiếp; 23 lỗi tập trung ở OCR/subtitle thuộc thay đổi phụ đề đang có sẵn trong workspace, không có lỗi mới ở acquisition/download.

Canary metadata read-only ngày 2026-09-21 (không tải file, không dùng cookie):

- YouTube video `dQw4w9WgXcQ`: đạt, nhận được title, identity và canonical URL.
- YouTube creator `@GoogleDevelopers/videos`: đạt với 2 candidate metadata.
- YouTube search `python tutorial`: đạt với 2 candidate metadata, cả hai có title và canonical URL.
- YouTube playlist `PL-osiE80TeTt2d9bfVyTiXJA-UTHn6WwU`: đạt với 2 candidate metadata, cả hai có title và canonical URL.
- Bilibili CBCE search `cats`: đạt với `provider_id=cbce_bilibili`, 2 candidate metadata; option UI đã mở với điều kiện browser/CBCE.
- Bilibili video `BV1xx411c7mD?p=1`: đạt metadata; identity giữ `media_id=BV1xx411c7mD_p1:p1`.
- Bilibili creator `space.bilibili.com/2`: profile sạch nhận HTTP 200 nhưng không có card/link video và hiển thị login gate; CBCE trả `AUTH_REQUIRED` sau bổ sung phân loại DOM. Một probe trước đó với profile mặc định từng trả 2 candidate, nhưng probe lặp lại với profile mặc định và hai creator (`2`, `946974`) không tái lập được; hai lượt live acquisition mới nhất dừng sớm với `RATE_LIMITED`, chưa có candidate. Chưa đạt yêu cầu live gate tối thiểu 2 creator/20 ID. Giữ operation creator ở trạng thái `unverified`, chưa mở claim scheduler.
- YouTube download canary `dQw4w9WgXcQ` ở trần 480p: `VideoDownloadManager` publication đạt (`succeeded`, file tồn tại), chạy trong thư mục tạm và đã dọn sau kiểm thử.
- Bilibili download canary `BV1xx411c7mD?p=1` ở trần 480p: publication đạt (`succeeded`, file tồn tại), không dùng cookies và đã dọn thư mục tạm.
- YouTube acquisition E2E cùng URL: run `completed` → candidate có title → selection `completed` → intent/job `succeeded` và publication tồn tại; dữ liệu/file đều nằm trong thư mục tạm.
- Bilibili acquisition E2E cùng URL: run `completed` → `media_id=BV1xx411c7mD_p1:p1`, `part_index=1`, canonical URL còn `p=1` → selection/job `succeeded` và publication tồn tại; dữ liệu/file đều nằm trong thư mục tạm.

Các canary trên chỉ chứng minh đúng URL/cấu hình tại thời điểm chạy; chưa chứng minh auth session, Bilibili creator/browser session, tải multipart qua từng connection hay toàn bộ nền tảng. Profile sạch creator hiện đã phân biệt được blocker `AUTH_REQUIRED`; cần người dùng đăng nhập thủ công vào từng connection profile rồi lặp lại live gate.

Phần còn lại của tài liệu vẫn là backlog bắt buộc: live gate/auth cho từng operation (đặc biệt Bilibili creator, Douyin search và TikTok Display), canary thật cho download qua từng connection, connection-aware download cho các provider khác nếu đạt contract, provider Douyin detail/creator và XHS, TikTok download/search/playlist nếu có contract hợp lệ, Mongo reconciliation đa tiến trình, và kiểm thử nguồn thật theo từng connection. Chưa coi acquisition là hoàn tất chỉ vì preview giả lập hoặc tải một URL thành công.

## 1. Kết quả cần đạt

Biến **Nguồn & Video** thành nơi người dùng có thể:

1. Dán link video, kênh, playlist hoặc danh sách kênh; hệ thống nhận diện đúng loại đầu vào.
2. Tìm video/kênh theo từ khóa trên những nền tảng thực sự hỗ trợ thao tác đó.
3. Cào metadata trước, xem thumbnail/thông tin, lọc và chọn video cần tải.
4. Tải hàng loạt qua hàng đợi; biết từng video đang tải, chờ đăng nhập, bị giới hạn hay đã hoàn thành.
5. Lưu kênh nguồn, theo dõi video mới và tùy chọn tự đưa video mới vào hàng đợi tải.
6. Mở video đã tải trong Video Library/Subtitle Studio hiện tại, giữ truy xuất về nguồn gốc.

Luồng sản phẩm đích:

```text
Link / từ khóa / kênh đã lưu
          ↓
Nhận diện nguồn + kiểm tra khả năng + phiên đăng nhập
          ↓
Cào metadata → danh sách xem trước → lọc/chọn
          ↓                            ↑
Hàng đợi tải ← video mới từ kênh đang theo dõi
          ↓
Xác thực file → Video Library → mở Subtitle Studio
```

“Giống ViralCrawl” ở đây là giống cách sử dụng và khả năng xử lý công việc, không phải chạy kèm EXE hoặc sao chép nguyên ứng dụng. Các chức năng tham chiếu được nhận diện từ khảo sát tĩnh bộ cài 1.9.13 ở lượt review trước; chưa coi đó là chứng minh rằng mọi nền tảng của ViralCrawl đang hoạt động thực tế.

## 2. Quyết định kiến trúc và phạm vi

- Giữ CBCE làm lõi crawler: registry, provider, worker cô lập, profile, ngân sách, checkpoint và hủy tác vụ.
- Giữ `VideoDownloadManager` làm chủ duy nhất của công việc tải video và file đầu ra. Không dựng một downloader/scheduler thứ hai bên cạnh rồi đồng bộ thủ công.
- Thêm luồng **video acquisition** độc lập với chủ đề. `keyword_id` chỉ là liên kết tùy chọn, không phải điều kiện để cào kênh hoặc tải video.
- Tách ba khái niệm: **đã phát hiện**, **đã chọn tải**, **đã có file hợp lệ**. Không dùng một cờ `seen` cho cả ba.
- Phát hành theo **nền tảng × thao tác × provider**, không gắn nhãn “hỗ trợ nền tảng” chỉ vì tải được một URL.
- Đợt đầu đề xuất: **Bilibili + YouTube**; tiếp theo **Douyin**, rồi **TikTok + XHS**. Đây là thứ tự kỹ thuật đề xuất, chưa phải ưu tiên nền tảng do người dùng chốt.
- Giữ luồng social listening, Live Wall và toàn bộ pipeline OCR/phụ đề/voiceover hiện có.

Quan hệ với [kế hoạch crawler trước](INTERNAL_MULTIPLATFORM_CRAWLER_PLAN.md): tiếp tục hướng tích hợp chọn lọc MediaCrawler cho nghiên cứu phi thương mại đã ghi tại mục 0, giữ license/provenance và ranh giới core/provider. Tài liệu này bổ sung nhánh sản phẩm video, điều chỉnh thứ tự triển khai để có luồng sử dụng được sớm; không đợi hoàn tất cutover cả 17 nguồn mới làm video.

Kế hoạch cũ chỉ cho X/TikTok/Meta dùng provider chính thức. Đề xuất mới là cho phép xây **provider acquisition riêng, opt-in** cho nội dung công khai hoặc phiên đăng nhập do người dùng chủ động cung cấp, sau khảo sát và kiểm thử từng operation. Không âm thầm mở rộng quyền hay đổi ý nghĩa provider chính thức; các provider mới vẫn mặc định tắt trước khi đạt gate. Việc thay đổi này cần được ghi rõ khi triển khai, không suy diễn từ nhãn “API đã kết nối”.

Ngoài phạm vi đợt đầu: comments/follower graph, tải livestream, DRM/paywall, tự giải CAPTCHA, luân chuyển proxy/tài khoản để né giới hạn, nhân bản hệ thống license của ViralCrawl, tự chạy OCR sau mỗi video tải xong. Bài ảnh được nhận diện và hiển thị đúng loại; tải album ảnh là tính năng sau.

## 3. Hiện trạng đã đối chiếu với repository

| Thành phần | Hiện có | Công việc cần bổ sung |
|---|---|---|
| Tải link | `video_downloads.py` + worker yt-dlp; hàng đợi bền vững, retry, cancel, khôi phục tải dở; Bilibili connection profile đã có lock/runtime bridge | Gắn candidate/asset ID, pause rõ nghĩa, canary auth/profile thật, bridge cho provider khác và dedupe theo nội dung |
| Hạn mức tải | 1–20 link/lượt; 2 worker, tối đa 32 job đang chờ/chạy | Selection lớn được lưu bền vững, cấp dần vào 32 slot; không tăng giới hạn vô điều kiện |
| Cookies | Netscape `.txt` tối đa 1 MB; dùng tạm; job có cookies cần chọn lại sau restart; profile Bilibili là lựa chọn thay thế không lưu plaintext | Mở connection/profile theo từng provider sau live gate; tiếp tục hỗ trợ import `.txt` |
| Cào nguồn | CBCE registry/provider/runtime; có licensed reuse zone | Hoàn thiện detail/creator/media metadata theo từng nguồn cần video |
| Kênh đăng ký | Nằm trong `KeywordInput.channels`; UI nhận `selected: Keyword` | Thư viện kênh video độc lập, có thể liên kết về chủ đề |
| Run hiện tại | `RunManager._start_batch()` cần keyword; ingest có relevance score | Acquisition manager dùng contract riêng; không ép qua bộ lọc relevance |
| Theo lịch | `AppServices._scheduler_loop()` gọi lịch keyword | Thêm tick subscription vào vòng đời hiện có, có lease/chống chồng lượt |
| Lưu trữ | SQLite mặc định; `PersistenceStore` có cả triển khai Mongo | Repository acquisition có capability rõ; không giả định Mongo standalone có transaction đa document |
| Giao diện | `ContentLibrary`, `RegisteredChannels`, `VideoLibrary`, tab kết nối | Bổ sung cào ngay, preview, queue tổng hợp và kênh video |

Mã tham chiếu: [runs.py](../backend/app/services/runs.py), [channel_scans.py](../backend/app/services/channel_scans.py), [schemas.py](../backend/app/schemas.py), [video_downloads.py](../backend/app/services/video_downloads.py), [video_download_worker.py](../backend/app/services/video_download_worker.py), [manifests.py](../backend/app/crawlers/manifests.py), [storage_protocol.py](../backend/app/storage_protocol.py), [ContentLibrary.tsx](../frontend/src/features/library/ContentLibrary.tsx).

Các mốc “7/17 đã cutover” trong tài liệu tháng 8 là lịch sử, không phải chứng nhận runtime hiện tại. Review trước cho thấy cutover audit còn thiếu bằng chứng trong cấu hình đang dùng; cần tạo bằng chứng mới theo operation. Không diễn giải thiếu evidence thành kết luận tất cả adapter đều hỏng.

## 4. Chức năng và hành vi người dùng

### 4.1. Cào ngay

- Chọn chế độ: **Link video / Kênh hoặc playlist / Danh sách kênh / Từ khóa**.
- Dán đoạn chia sẻ: lấy URL, resolve short link có giới hạn rồi nhận diện đúng target. Nếu chứa nhiều URL thì hiển thị số lượng, không tự chọn link đầu tiên.
- Link vừa chỉ video vừa chứa playlist: mặc định một video, có lựa chọn rõ ràng để chuyển sang playlist.
- Kênh/playlist luôn xem trước trước khi tải hàng loạt. Với một video có thể dùng “Tải ngay”; backend vẫn resolve và kiểm tra target.
- Số lượng mặc định đề xuất: 20 candidate; tối đa 100/lượt tương tác. Có nút cào tiếp, không dùng cuộn vô hạn làm lệnh tải vô hạn.
- Từ khóa chỉ bắt buộc trong chế độ search. Không yêu cầu tạo chủ đề trước khi dùng các chế độ còn lại.
- Danh sách kênh là parent run chứa child run từng kênh; lỗi một kênh không hủy mọi kênh khác.

### 4.2. Xem trước, lọc và chọn tải

Mỗi candidate hiển thị: nền tảng, thumbnail, tiêu đề, tác giả/kênh, thời điểm đăng, thời lượng, loại media, chỉ số có sẵn, trạng thái đã tải, link gốc và lỗi/thiếu dữ liệu nếu có.

- Chọn từng dòng hoặc chọn tất cả **trong tập kết quả đã xác định**. Khi đổi bộ lọc, hiển thị rõ số mục đã chọn; không tự đổi selection theo số thứ tự card.
- Bộ lọc: thời gian đăng, thời lượng, video/chỉ ảnh, chưa có file, kênh; lượt xem/tương tác chỉ xuất hiện khi provider có dữ liệu.
- Phân biệt lọc phía nguồn và lọc trong tập đã thu thập. Dữ liệu không biết giữ `null`, không giả làm `0`; không quảng cáo kết quả là toàn bộ nền tảng.
- Preview đầu tiên chỉ lấy metadata nhẹ; chi tiết/URL media được lấy khi cần. Có nút xem nguồn gốc; không tải toàn bộ video chỉ để vẽ card.
- “Lấy thêm 20 video mới” nghĩa là 20 ID chưa phát hiện trong phạm vi run/kênh, **không** phải quét đúng 20 card. `scanned`, `new`, `duplicate`, `filtered`, `unavailable` là các bộ đếm riêng.
- Hết ngân sách, cursor lặp hoặc nhiều vòng không có ID mới thì dừng có lý do; không báo đủ 20 nếu thực tế chỉ có 7.

### 4.3. Tải video

- Chọn chất lượng `480/720/1080/best`, giữ ý nghĩa trần chất lượng hiện tại; hiển thị chất lượng thực tế, không upscale.
- Một candidate có thể có nhiều media/part. Bilibili phải giữ part `p`/`cid`; không dedupe chỉ bằng BV ID rồi làm mất các phần khác nhau.
- Queue hiển thị các bước: chờ → lấy thông tin → tải → ghép/xử lý → kiểm tra file → hoàn tất.
- Có tạm dừng/tiếp tục, hủy, thử lại từng mục hoặc nhóm. Tiếp tục có thể cần tải lại nếu nguồn không hỗ trợ byte-range/fragment resume; UI không cam kết resume byte trong mọi trường hợp.
- Giữ file tải dở khi pause/cancel theo retention cấu hình; “xóa dữ liệu tải dở” là thao tác riêng, chỉ tác động thư mục job tương ứng.
- File hoàn tất có source URL, source ID, creator ID, external ID/media ID, download job ID và video ID. Mở Studio dùng video ID hiện tại, không tạo kho video song song.

### 4.4. Kênh nguồn và theo dõi video mới

- Lưu kênh sau khi resolve thành công; đổi tên, gắn nhãn, nhóm, liên kết chủ đề tùy chọn; xem lịch sử quét và các video của kênh.
- Theo dõi mặc định **chỉ phát hiện/thông báo video mới**; auto-download mặc định tắt.
- Khi bật theo dõi lần đầu, mặc định lập mốc hiện tại, không tự tải toàn bộ lịch sử. Có lựa chọn backfill riêng, ghi rõ số video/ngày cần lấy.
- Kênh đã có trong chủ đề được đề nghị “Thêm vào kênh video”, không tự sửa/xóa channel hay checkpoint của chủ đề.
- Lịch mặc định đề xuất 6 giờ; tối thiểu 30 phút chỉ khi provider cho phép, có jitter, backoff và giới hạn dùng chung theo nguồn/connection.
- Chỉ operation có `BACKGROUND_SAFE` đã kiểm thử mới được chạy lịch. `MANUAL_ONLY` vẫn cho quét tay và hiển thị lý do không bật được lịch.
- Session hết hạn/challenge: chuyển sang chờ người dùng, không tự bật cửa sổ đăng nhập từ scheduler.
- `pause subscription` chỉ dừng lượt theo dõi tương lai; không hủy tải đã được người dùng xếp hàng. UI có thao tác hủy riêng.

## 5. Ma trận nền tảng và chiến lược provider

Đây là **đích triển khai**, không phải bảng xác nhận tất cả chức năng đang hoạt động.

| Nguồn | Link video | Kênh/playlist | Tìm kiếm/gợi ý kênh | Thứ tự và lưu ý |
|---|---|---|---|---|
| Bilibili | Reuse yt-dlp; refresh phiên khi cần; giữ multipart | Hoàn thiện CBCE/licensed creator, giới hạn phân trang | Reuse search, thêm gom creator và metadata | Đợt 1; cần test thật link thường, short link, part và phiên đăng nhập |
| YouTube | Reuse yt-dlp và kiểm tra runtime phụ trợ | Tách dịch vụ uploads playlist hiện có khỏi phụ thuộc keyword; playlist resolver riêng | API hiện có nếu có key; provider khác phải khai báo riêng | Đợt 1; API dùng lấy metadata, không phải API tải file |
| Douyin | Chưa có media download contract acquisition | Search metadata đã nối qua reviewed CBCE contract, còn chờ live gate | Search hữu hạn qua CBCE; detail/creator chưa mở | Đợt 2; tách Douyin khỏi TikTok, không fallback yt-dlp |
| TikTok | Display API hiện chỉ metadata; chưa có media download contract | Đã nối creator/video detail cho account đã cấp quyền, còn chờ live gate | Global search/playlist chưa hỗ trợ; không dùng Display API để hứa cào creator tùy ý | Đợt 3; chỉ mở từng operation sau canary và capability gate |
| XHS/RedNote | Detail video + bounded download khi provider có media | Creator/feed union theo ID qua các lần cuộn | CBCE/licensed search; giữ token truy cập trong runtime cần thiết | Đợt 3; bài ảnh không báo lỗi tải video; không chọn card bằng index DOM |
| Kuaishou, Weibo | Làm theo adapter thực có và khả năng lấy media | Mở từng operation sau canary | Reuse chọn lọc nếu cần | Đợt sau; không chặn bản đầu |
| Instagram, Facebook, X, Reddit | Provider tải link riêng nếu test thành công | Khảo sát từng loại account/page/subreddit | Không suy từ tải link sang hỗ trợ search toàn cục | Đợt sau; giữ nguyên quyền của provider chính thức |

YouTube có uploads playlist trong `contentDetails.relatedPlaylists.uploads`, phù hợp để tái dùng luồng quét kênh hiện tại. [Tài liệu Channels](https://developers.google.com/youtube/v3/docs/channels).

TikTok Display API lấy metadata của người dùng đã cấp quyền; không thay thế một công cụ tìm kiếm video công khai tùy ý. Provider acquisition mới, nếu khả thi, phải có capability và bằng chứng riêng. [Display API](https://developers.tiktok.com/docs/en/display-api-overview), [List Videos](https://developers.tiktok.com/docs/en/tiktok-api-v2-video-list).

Mỗi operation cần `provider_id`, implementation, availability, target kind, auth, schedule policy, coverage, giới hạn, phiên bản extractor và ngày kiểm chứng gần nhất. UI vô hiệu hóa thao tác chưa sẵn sàng kèm lý do. Không fallback từ API sang browser một cách vô hình; người dùng biết provider/connection nào đang được dùng.

## 6. Thiết kế backend

### 6.1. Phân chia trách nhiệm

| Thành phần | Trách nhiệm | Không làm |
|---|---|---|
| `AcquisitionManager` mới | Resolve target, parent/child run, candidate, filter, checkpoint, cancel/recovery | Không quản lý subprocess tải file riêng |
| CBCE registry + providers | Search/detail/list creator/media metadata, auth, giới hạn nguồn | Không lưu DB/UI hoặc tự tạo download queue |
| `AcquisitionRepository` mới | Channels độc lập, runs, candidates, selection intent, subscriptions, lease/outbox | Không lưu cookie/token/URL ký số vào record công khai |
| Download bridge mới | Chuyển selection intent sang job; retry idempotent; đồng bộ liên kết asset | Không coi enqueue thành tải thành công |
| `VideoDownloadManager` hiện có | Sở hữu job tải, worker, file tạm, pause/retry/cancel, publication | Không cào cả creator bên trong một job không giới hạn |
| `AppServices` | Start/recover/shutdown; scheduler tick và admission control chung | Không chạy scheduler trong frontend |

Tái sử dụng `SEARCH`, `FETCH_DETAIL`, `LIST_CREATOR`, `MEDIA_METADATA`, `MEDIA_DOWNLOAD` trong contract hiện có. Chỉ thêm target kind playlist/collection khi cần, giữ tương thích catalog cũ. `MEDIA_DOWNLOAD` chọn cách lấy bytes; việc xếp hàng và sở hữu file vẫn thuộc downloader hiện tại.

Metadata preview qua yt-dlp phải ở worker có timeout/cancel, không chạy blocking trong request API. Chế độ flat playlist có thể thiếu metadata; chỉ bổ sung detail cho tập giới hạn cần lọc/xem/tải, không N+1 toàn kênh. [yt-dlp: flat playlist và các tùy chọn](https://github.com/yt-dlp/yt-dlp#general-options).

### 6.2. Target, định danh và chống trùng

- Reuse `target_detection.py` cho hostname/URL an toàn; thêm resolver phân biệt video/creator/playlist/hashtag. Không dùng `normalize_channel()` hiện tại để normalize mọi link video vì nó bỏ nhiều query.
- Canonical content key: `(source_id, external_id)`; media key bổ sung `media_id/part_id`. Channel key dùng ID nền tảng ổn định, không dựa tên hiển thị/handle có thể đổi.
- Signed CDN URL, XHS access token và cookie là dữ liệu runtime có tuổi thọ; không dùng làm khóa chống trùng. Lưu canonical URL sạch và re-resolve khi tải/retry.
- Download intent key: `(library_scope, media_key, quality_policy, output_profile)`. Có unique reservation và khóa nhận việc; hai lần click/auto-download cùng lúc không tạo hai job.
- MVP dedupe theo cùng quality policy. Reuse file chất lượng cao hơn cho yêu cầu thấp hơn chỉ thêm khi có rule kiểm chứng; không tự coi file 480p đáp ứng 1080p.
- Asset phải có file còn tồn tại và metadata kiểm tra hợp lệ. Bản ghi `succeeded` có file đã mất không chặn tải lại.
- Scope ban đầu theo workspace/library hiện có, không tự biến dự án thành multi-tenant. Nếu bật chế độ nhiều người dùng, connection/job/asset phải ràng buộc owner từ server; không tin owner ID do client gửi.

### 6.3. Dữ liệu tối thiểu

| Record | Trường chính |
|---|---|
| `acquisition_channels` | ID nội bộ, source/provider, creator ID, canonical URL, label/avatar, tags, connection ref, topic links |
| `acquisition_runs` | ID/parent ID, mode, target, query tùy chọn, provider/version, state/phase, limits, cursor, counters, stop reason, retry-after, timestamps |
| `acquisition_candidates` | Content/media key, creator, canonical URL, title, thumbnail, published_at, duration, media type, available metrics, metadata completeness |
| `acquisition_run_items` | Run/candidate relation, thứ tự, trạng thái/loại bỏ, first_seen/last_seen; cho phép một candidate xuất hiện ở nhiều run |
| `download_intents` | Idempotency key, candidate/media key, options, intent state, job ID, error, retry/lease metadata |
| `acquisition_subscriptions` | Channel ID, enabled, interval, initial policy, auto-download rule, baseline, scan checkpoint, next_run_at, lease |
| `subscription_observations` | Subscription/content key, discovered_at, published_at, classification new/backfill/unknown; không đồng nhất với downloaded |
| Asset linkage | Download job ID → video ID, media key, actual format/height/bytes, validation result, source provenance |

`connection_ref` trỏ về cơ chế credentials/OAuth/browser profile hiện có; không dựng bảng mật khẩu mới. Selection dựa candidate ID ổn định và snapshot bộ lọc; API trả phân trang, không nạp toàn bộ kênh vào RAM/browser.

SQLite là đích MVP. Mở rộng protocol/repository theo capability; khi dùng Mongo chưa có implementation acquisition đủ tương đương, tắt tính năng mới với lỗi rõ ràng nhưng giữ tính năng cũ hoạt động. Không tự mở một SQLite thứ hai sau lưng cấu hình Mongo. Đợt sau nếu hỗ trợ Mongo standalone phải thiết kế idempotent writes/reconciliation thay vì giả định transaction đa record.

### 6.4. Bridge DB ↔ downloader và phục hồi sự cố

Job tải hiện lưu JSON, còn acquisition lưu DB; hai bên không có một transaction chung. Triển khai theo intent/outbox:

1. Transaction lưu selection + intent, chỉ nhận unique key một lần.
2. Dispatcher đọc intent chờ, kiểm tra slot và gọi `submit_intent()` idempotent bổ sung cho manager hiện tại.
3. Manager ghi durable intent key trong job JSON trước khi chạy; khi restart xây lại index theo key. Dispatcher crash trước khi ghi job ID về DB vẫn tra được job cũ, không tạo job mới.
4. Đồng bộ trạng thái qua event và periodic reconciliation; polling/replay phải an toàn khi lặp lại.
5. Với selection lớn hơn 32, intent còn lại chờ bền vững; chỉ cấp job khi còn slot, không trả lỗi mất toàn bộ selection.
6. File được kiểm tra trong staging, chuyển nguyên tử trong cùng volume sang kho video, rồi mới công bố hoàn tất. Crash giữa move và save cần manifest/reconciler để nhận lại đúng file.

Không chuyển toàn bộ lịch sử download JSON sang DB trong MVP. Thêm `schema_version` và field optional; reader đọc được job cũ, retry/resume không đổi ID. Mọi thay đổi định dạng phải có fixture job cũ và kiểm thử rollback.

### 6.5. State, lỗi và điều phối

- Acquisition run: `queued → resolving → collecting → completed | partial | failed | canceled`; có `paused`, `waiting_auth`, `rate_limited` với resume point. Run discovery hoàn tất không có nghĩa các video đã tải xong.
- Download intent/nhóm: tách số lượng chờ/tải/đạt/lỗi/bị hủy. Giữ contract download job cũ (`state`/`phase`), thêm state mới chỉ khi FE/BE cùng hỗ trợ.
- Pause: ngừng cấp việc mới, lưu checkpoint, dừng worker có kiểm soát; resume chính run/job đó. Cancel: kết thúc lượt thực thi; retry là thao tác rõ ràng.
- Cancel discovery không xóa candidate/file hoặc mặc nhiên hủy job được share với run khác. Hủy nhóm tải chỉ tác động job do nhóm sở hữu; job được tái dùng phải thể hiện quan hệ đó.
- Lỗi có mã ổn định: `AUTH_REQUIRED`, `CHALLENGE_REQUIRED`, `RATE_LIMITED`, `UNSUPPORTED_OPERATION`, `SOURCE_UNAVAILABLE`, `CONTENT_UNAVAILABLE`, `PARSER_CHANGED`, `BUDGET_EXHAUSTED`, `DISK_FULL`, `MEDIA_INVALID`, `DEPENDENCY_MISSING`.
- Chỉ retry lỗi tạm thời; tối đa 3 lần tổng ở lớp điều phối, có backoff/jitter và tôn trọng Retry-After. Retry bên extractor phải được tính vào deadline/request budget, tránh nhân số lần vô hạn.
- Hết nguồn thực sự là `completed` với `stop_reason=exhausted`; hết ngân sách hoặc lỗi sau khi có dữ liệu là `partial`; parser vỡ không trả thành danh sách rỗng thành công.
- Ghi item trước, checkpoint sau. Cursor lặp/no-progress dừng có mã; profile lock không được giữ khi chờ người dùng thao tác trong thời gian không giới hạn.

Mặc định đề xuất: tối đa 2 acquisition worker toàn app, 1 worker/connection profile; giữ 2 download worker và 32 active/pending job. Preview mặc định 20, trần 100 candidate, tối đa 20 trang/5 phút hoặc hạn mức nguồn thấp hơn; detail enrichment tính chung ngân sách. Dừng sau 4 vòng không có ID mới. Provider budget luôn có quyền siết các số này xuống.

Áp dụng admission control theo CPU/RAM/băng thông/ổ đĩa; không dùng GPU hay hàng đợi OCR cho crawler. Khi máy đang render, người dùng có thể giảm tải tải/crawl. Có max bytes/video, tổng bytes/nhóm và dung lượng dự phòng cấu hình được; không nhận vô hạn file khi thiếu Content-Length.

### 6.6. Phiên đăng nhập và lấy media

- Reuse login/profile lifecycle CBCE; người dùng chọn nền tảng và connection, mở profile do app quản lý để đăng nhập thủ công.
- Không tự quét/giải mã tất cả profile Chrome/Edge cá nhân dù workspace có helper đọc Chromium cookies. Import cookie `.txt` hiện tại vẫn là lựa chọn chủ động.
- Cookie file cho worker tải chỉ lấy đúng lượt, cấp qua file tạm ACL hạn chế và dọn sau khi kết thúc; không đưa cookie vào job JSON, stdout, log hay response. Với Bilibili connection profile, worker resolve profile hash lúc chạy và để yt-dlp đọc cookie trong profile thay vì tạo snapshot plaintext lâu dài.
- Không mở đồng thời hai browser/worker cùng profile. Dùng `ProfileLock`; worker tải chạy trong subprocess cô lập và chỉ giữ lock trong thời gian trích xuất/tải. Playwright cũng yêu cầu profile automation riêng và không cho nhiều browser dùng chung user data dir. [Persistent context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context).
- Restart: job cookie-file vẫn chờ chọn lại như hiện tại. Job gắn app profile chỉ tự tiếp tục nếu lấy được phiên hợp lệ theo policy; không mặc định lưu plaintext cookie lâu dài.
- Ưu tiên downloader hiện có cho URL gốc. Nếu provider cần media URL/headers theo session, thêm transport strategy bên trong cùng worker/manager, dùng bounded streaming; primitive `BoundedMediaDownloader` không trở thành queue độc lập.
- Reuse kiểm soát mạng public và kiểm tra mọi redirect; giới hạn bytes/time cả thumbnail và video. Kiểm tra loại nội dung, video stream, duration và khả năng decode sample trước publication; file HTML/ảnh/audio-only không được báo tải video thành công.
- Pin phiên bản extractor/runtime; bổ sung preflight ffmpeg/ffprobe hoặc verifier tương đương. YouTube cần kiểm tra JS runtime/EJS theo phiên bản yt-dlp đang dùng; upstream có liệt kê các phụ thuộc này. [yt-dlp dependencies](https://github.com/yt-dlp/yt-dlp#dependencies). Không tự cập nhật dependency giữa run đang chạy.

### 6.7. Thuật toán theo dõi không bỏ sót hoặc tải lại lịch sử

1. Baseline đầu: ghi thời điểm bật và ID trong cửa sổ quét hữu hạn; không coi các ID còn nằm ngoài cửa sổ là video mới khi gặp sau này.
2. Mỗi lượt quét newest-first nếu nguồn hỗ trợ, quét chồng một cửa sổ gần đây để bắt bài đăng trễ/cùng timestamp. Dùng cả ID và thời gian, không dừng ngay ở video cũ đầu tiên vì có pinned post/thứ tự không ổn định.
3. Upsert observations trước khi advance cursor/high-water mark; auto-download tạo intent riêng, dedupe độc lập với discovery.
4. Item trước baseline là historical/backfill; thiếu thời gian đáng tin thì gắn `unknown`, không auto-download theo luật new-only cho tới khi đủ bằng chứng. UI nêu hạn chế bao phủ.
5. Timeout/partial scan không được đẩy high-water mark qua phần chưa quét. Cursor hết hạn thì quét lại cửa sổ hữu hạn và dedupe.
6. Lease theo subscription, unique scheduled run key và distributed lock/lease nhận việc chống hai tick cùng chạy. Backend không hoạt động thì lịch không chạy; khi mở lại gom thành một lượt catch-up hữu hạn, không chạy bù mọi tick.
7. Người dùng xem video chưa đồng nghĩa đã tải; tải thất bại vẫn nằm trong danh sách chờ/retry. Auto-download có trần số video/bytes mỗi ngày, chất lượng và bộ lọc rõ ràng.

## 7. API và frontend

### 7.1. Contract hiện tại và phần mở rộng còn lại

Các đường dẫn acquisition dưới đây đã có trong backend hiện tại; phần còn lại của bảng là contract cần tiếp tục mở rộng theo capability. Backend là nguồn sự thật cho schema/error, frontend không mock thêm capability không có trong catalog.

| Endpoint | Chức năng |
|---|---|
| `POST /api/v1/acquisition/runs` | Tạo run resolve/search/creator/playlist; trả 202 và run ID |
| `GET /api/v1/acquisition/runs` | Lịch sử/lượt đang chạy, phân trang |
| `GET /api/v1/acquisition/runs/{id}` | Progress, counters, provider, lỗi và stop reason |
| `GET /api/v1/acquisition/runs/{id}/candidates` | Kết quả phân trang, filter/sort phía server |
| `POST /api/v1/acquisition/runs/{id}/{pause|resume|cancel}` | Ba route thao tác riêng, idempotent theo state |
| `POST /api/v1/acquisition/runs/{id}/continue` | Cào tiếp creator/playlist YouTube hoặc playlist Bilibili theo generation cursor, chống replay |
| `POST /api/v1/acquisition/download-selections` | Lưu selection + options + idempotency key, trả selection ID/counters |
| `GET /api/v1/acquisition/download-selections/{id}` | Theo dõi nhóm intent và job đã cấp, phân trang |
| `POST /api/v1/acquisition/download-selections/{id}/{pause|resume|cancel}` | Điều khiển nhóm theo semantics ownership ở mục 6.5 |
| `GET/POST /api/v1/acquisition/channels` | Liệt kê/tạo thư viện kênh video độc lập |
| `PATCH/DELETE /api/v1/acquisition/channels/{id}` | Sửa/bỏ theo dõi kênh; không xóa file đã tải |
| `PUT /api/v1/acquisition/channels/{id}/subscription` | Cập nhật lịch, new-only/backfill, auto-download limits |

Giữ `GET/POST /api/v1/videos/downloads`, `/{job_id}/cancel`, `/{job_id}/retry`; pause/resume trực tiếp đã nối tương thích với state machine và giữ nguyên job ID. Dùng lại API login/profile/credentials hiện có. Catalog hiện tại được mở rộng theo operation, không tạo một danh sách nguồn hardcode nữa trong FE.

Ví dụ request tạo run; `limits` là yêu cầu và server sẽ clamp theo policy:

```json
{
  "mode": "creator",
  "targets": ["https://space.bilibili.com/123456"],
  "keyword_id": null,
  "connection_id": null,
  "limits": {"max_candidates": 20, "max_pages": 20, "deadline_seconds": 300},
  "filters": {"media_type": "video", "only_not_downloaded": true}
}
```

`123456` là ID minh họa, không phải nguồn đã kiểm chứng. Server xác thực target/provider, quyền dùng connection, schema filter; target API nhận loại nguồn/URL, không nhận raw signed media URL/headers tùy ý từ frontend.

Download selection và direct video download nhận thêm `connection_id` đã chuẩn hóa cho Bilibili. Khi dùng connection profile thì không gửi `cookie_text` cùng request; job JSON chỉ giữ connection ID, còn profile path được resolve ở worker runtime. Các nền tảng khác bị từ chối rõ ràng cho tới khi có provider/session contract riêng.

Response lỗi thống nhất tối thiểu có `code`, `message`, `retryable`, `retry_after`, `run_id/job_id` khi có. Validation 422; operation chưa hỗ trợ 409 kèm capability; quota đầu vào 429. Async run fail trả lỗi trong run, không dùng HTTP 200 để ngụy trang lỗi thành zero results.

Với nhiều target creator/playlist, response parent có `child_run_ids`; `GET /runs/{id}` bổ sung `children` gồm ID, target, provider, state, error và counters. `GET /runs` chỉ liệt kê parent/lượt độc lập. State `partial` giữ candidate của child thành công; resume cùng parent chỉ chạy phần chưa hoàn tất. Child cho phép đọc riêng; pause/resume/cancel trực tiếp child trả 409 kèm parent `run_id`. Ngân sách metadata/thời gian là tổng của nhóm, không phải số yêu cầu nhân theo số kênh; khi hết số metadata cần tạo lượt mới cho các target bị bỏ qua.

### 7.2. Bố trí giao diện

Mở rộng **Nguồn & Video**, không tạo app khác:

- **Cào ngay**: input + mode + cấu hình gọn, kết quả dạng grid/table, bộ lọc, chọn tải/lưu kênh.
- **Kênh nguồn**: kênh video độc lập; khu vực kênh theo chủ đề hiện có được giữ và phân biệt rõ.
- **Hàng đợi**: hai nhóm “Đang cào” và “Đang tải”; xem nhóm/từng item, pause/retry/cancel, tiến độ không nhảy giả về 100% khi mới xếp hàng.
- **Video**: tiếp tục `VideoLibrary`; thêm nguồn/kênh/chất lượng, lọc, mở nguồn, mở Studio.
- **Kết nối** và **Live**: giữ chức năng, kết nối hiển thị operation nào dùng được; Live không bị đổi thành crawler/download.

MVP dùng polling khi màn hình mở; trạng thái thật nằm backend, đóng tab không hủy job. Chỉ thêm SSE nếu tái dùng event bus thuận lợi và có snapshot/reconnect đúng. Đảm bảo selection theo ID qua phân trang/virtualization và trạng thái chờ đăng nhập có nút hành động cụ thể.

## 8. Lộ trình và điều kiện nghiệm thu

### P0 — Chốt contract, capability và bộ nguồn kiểm thử

- Đối chiếu snapshot hiện tại, lập capability matrix Bilibili/YouTube/Douyin/TikTok/XHS theo operation, auth, dependency và lỗi thật.
- Chọn bộ URL công khai/người dùng có quyền truy cập: video thường/short link/multipart, creator, playlist, bài ảnh, video không còn tồn tại, phiên hết hạn.
- Chốt schema/state/error, thư mục file, profile ownership, migration và feature flags; giữ baseline test downloader/crawler hiện có.
- Spike nhỏ TikTok/XHS song song để phát hiện sớm phần không khả thi, không kéo dài thành triển khai đầy đủ.
- Nghiệm thu: mỗi operation có trạng thái “đã có bằng chứng / cần triển khai / bị chặn” và lý do; danh sách việc không dựa vào lời quảng cáo bộ cài.

### P1 — Nền tảng acquisition + tải từ preview

- Repository SQLite, resolver, AcquisitionManager, candidate/run API và UI Cào ngay.
- Download intent/outbox, idempotent bridge, asset linkage; reuse tải link hiện tại.
- Link video Bilibili/YouTube → metadata → chọn chất lượng → tải → có trong Video Library → mở Studio.
- Nghiệm thu: không cần keyword; cùng link/short link/selection retry không nhân đôi; restart ở các điểm crash vẫn khớp intent/job/file. Đường tải link cũ không hồi quy.

### P2 — Kênh/playlist và tải có lựa chọn trên Bilibili + YouTube

- Creator/playlist enumerate có budget, metadata preview, lọc, selection bền vững, cào tiếp theo cursor vị trí/ID với lý do dừng.
- Kênh video độc lập, link về topic tùy chọn; grid/table theo stable ID; hàng đợi nhóm và pause/resume.
- Nghiệm thu: cào tập 20 video từ mỗi kênh test, chọn 5 chỉ có 5 mục được cấp tải; chọn 50 mục không vượt 32 slot và không mất 18 mục còn lại; multipart không bị gộp nhầm.
- Đây là **mốc dùng được đầu tiên theo workflow ViralCrawl**. Hoạt động live của mỗi nguồn phải đạt gate riêng; nguồn lỗi không làm ẩn kết quả nguồn đạt.

### P3 — Tìm kiếm/gợi ý kênh + Douyin + session bridge

- Search metadata, bộ lọc thực hỗ trợ, gợi ý creator gom từ kết quả có provenance; không gọi đó là bảng xếp hạng toàn nền tảng.
- Douyin search metadata đã nối qua CBCE reviewed contract nhưng còn `unverified`; tiếp tục detail/creator qua CBCE/licensed zone và child run độc lập khi có provider contract.
- App-owned login profiles và bridge phiên cho acquisition; UI auth/challenge/retry rõ ràng. Bilibili video/selection đã có download bridge qua `cookiesfrombrowser`; các provider khác chỉ mở sau khi có contract lấy session tương ứng, không suy diễn từ việc creator đã đăng nhập.
- Nghiệm thu còn lại: canary thật cho video Bilibili cần login trên từng connection; cùng luồng search → preview → lưu kênh/chọn tải chạy với nguồn đã đạt gate; đăng nhập một connection không ảnh hưởng connection khác; hết phiên giữ được công việc.

### P4 — TikTok + XHS theo operation

- Dựa kết quả spike P0, làm link download trước, rồi creator, cuối cùng search/hashtag khi có bằng chứng.
- XHS union ID qua feed ảo hóa, phân biệt bài ảnh/video, session token chỉ trong runtime; media fetch có giới hạn bộ nhớ.
- TikTok giữ nguyên Display API, thêm provider riêng nếu viable; ghi rõ giới hạn metadata/download/search.
- Nghiệm thu: canary thật từng operation, không coi test unit hay tải được một link là hoàn tất cả nền tảng. Operation chưa đạt giữ trạng thái chưa hỗ trợ và ghi backlog/blocker cụ thể.

### P5 — Theo dõi kênh và tự tải video mới

- Subscription scheduler trong AppServices, baseline/overlap window, lease, observations, auto-download rule/budget.
- Notifications trong app và lịch sử quét; pause lịch, lỗi nguồn, chờ đăng nhập, catch-up sau restart.
- Nghiệm thu: baseline không tải lại lịch sử; item mới phát hiện đúng một lần; pinned/đăng trễ/cùng timestamp không làm bỏ sót; failed download không bị đánh dấu hoàn tất; hai scheduler tick không nhân đôi run/intent.
- Có thể làm song song P4 sau P2/P3, chỉ bật cho provider đã được xác nhận background-safe.

### P6 — Kiểm thử dài, rollout và mở rộng nguồn

- Test Windows process tree, profile locks, disk-full, mất mạng, pause/cancel/restart, migration và rollback feature flag.
- Kiểm tra với OCR/render chạy đồng thời ở mức tải nhỏ; đo RAM, CPU, throughput và độ trễ UI.
- Pin dependency; log phiên bản/coverage; runbook parser thay đổi, auth hết hạn và quota. Mở nguồn sau theo nhu cầu, không thêm đồng loạt 17 nguồn vào lời hứa tải video.
- Nghiệm thu: đủ bằng chứng cho toàn bộ scope bật trong catalog; test hồi quy cũ, FE tests/lint/build đạt; tài liệu vận hành và rollback đã thử thực tế.

Thứ tự phụ thuộc: `P0 → P1 → P2 → P3 → P4`, `P2/P3 → P5`, các scope đạt → `P6`. Hardening cơ bản, giới hạn và xử lý secrets phải làm trong từng phase, không dồn hết đến P6.

Ước lượng để chia việc, không phải cam kết: P0 1–2 ngày kỹ thuật; P1 3–5; P2 3–5; P3 4–7; P4 5–10; P5 3–5; P6 2–4. Tổng khoảng 21–38 ngày công cho một người làm tuần tự; auth/API/quota và lỗi nền tảng có thể kéo dài. Cập nhật lại sau P0; mốc P2 dự kiến 7–12 ngày công nếu hai nguồn đầu khả dụng.

## 9. Phân công và ranh giới file

| Owner | File hiện có cần tích hợp | File/nhóm mới đề xuất |
|---|---|---|
| Backend orchestration/storage | `application_services.py`, `config.py`, `storage_protocol.py`, `sqlite_store.py`, đăng ký router trong `main.py` | `services/acquisition/{manager,models,repository,targets,subscriptions,download_bridge}.py`, `api/acquisition.py` |
| Backend providers/runtime | `crawlers/contracts.py`, `manifests.py`, `registry.py`, adapter nguồn, `services/channel_scans.py`, CBCE runtime | Provider/resolver/metadata worker theo platform; contract fixtures |
| Backend downloader | `services/video_downloads.py`, `video_download_worker.py`, `api/videos.py` | Intent index/reconciliation, verifier/transport module nếu cần |
| Frontend | `App.tsx`, `features/shared/presentation.ts`, `features/library/ContentLibrary.tsx`, `RegisteredChannels.tsx`, `VideoLibrary.tsx`, API types/library | `features/acquisition/*`, `api/acquisition.ts`, tests selection/queue |
| QA/docs | `backend/tests/`, frontend tests, docs crawler/download | Bộ fixture nguồn, live canary runner opt-in, evidence không có secrets, hướng dẫn thao tác |

Đường dẫn mới là đề xuất, chốt khi làm P0 để tránh module trùng trách nhiệm. Backend sở hữu schema/API, frontend sở hữu `frontend/**`; provider team không sửa queue/file format ngoài contract chung. `config.py`, `application_services.py`, `schemas.py`, `main.py`, `App.tsx`, `VideoLibrary.tsx` và API types có nhiều thay đổi đang làm trong workspace: giao một owner tích hợp, không ghi đè hoặc gom commit các thay đổi không thuộc acquisition.

Không sửa OCR timing/model, subtitle history hay voiceover như một phần của kế hoạch này. Integration với Studio chỉ cần video ID/path/provenance đúng và hành động mở video.

## 10. Bộ kiểm thử và bằng chứng bắt buộc

| Nhóm | Điều kiện đạt |
|---|---|
| Resolver | Short link/redirect/query/Unicode; creator khác video; YouTube video+playlist; Douyin video trong query; Bilibili nhiều part |
| Ingestion | Không keyword vẫn cào được; bài không khớp chủ đề không bị loại; null metrics không thành 0; identity ổn định qua lần cào |
| Pagination | Trùng card/cursor, pinned post, reorder, empty page/challenge; dừng đúng ngân sách; partial có lý do |
| Selection | Chọn qua nhiều trang/filter; double-click, retry request, hai run chọn cùng media; không tải ngoài tập đã xác nhận |
| Queue | 50 intent/32 slot; pause/resume/cancel từng mục/nhóm; auth wait không chiếm worker; không deadlock profile |
| Recovery | Crash trước/sau persist intent, job JSON, move file, success save; không mất job hoặc nhân file; lịch sử job cũ đọc được |
| Media | Có video stream, duration hợp lệ, decode được sample đầu/giữa/cuối trong budget; HTML/ảnh/zero-byte/audio-only/missing fragment không thành success |
| Resume | Local HTTP Range test chứng minh resume bytes; server không hỗ trợ phải restart sạch; đổi format không nối nhầm partial |
| Subscription | Baseline/backfill, late publish, timestamp bằng nhau/thiếu, pinned; file xóa/retry fail; overlapping tick và catch-up |
| Runtime | Cancel nhận ở UI trong 1 giây và worker/process tree dừng trong 5 giây ở fixture chuẩn; nếu quá deadline thì supervisor force-stop, ghi lỗi |
| Secrets/network | Không token/cookie/signed URL trong response/log/job; URL nội bộ/redirect/rebinding bị chặn; temp cleanup giới hạn đúng job |
| UI | Mở lại trang thấy đúng state; closing tab không hủy run; nguồn unsupported có lý do; chọn theo ID không trượt card |
| Tương thích | Tải link cũ, keyword crawl, Live Wall, video upload/Studio, shutdown và config backend cũ không hồi quy |

Live gate mỗi operation: tối thiểu 3 lượt có ghi thời gian, provider/version, loại phiên và target; thử 2 kênh khác nhau cho creator, tập mục tiêu tới 20 ID công khai mỗi kênh, chọn 5 video để tải ở những nguồn hỗ trợ. Ghi số đủ điều kiện/thành công/lỗi và đối chiếu ID với trang nguồn trong cùng cửa sổ thời gian; số này là mẫu kiểm chứng, không chứng minh toàn bộ nền tảng.

Với search có personalization/biến động, kiểm tra target/filter/metadata và nêu coverage; không yêu cầu thứ tự khớp tuyệt đối hai phiên khác nhau. Với theo dõi video mới, dùng fixture để chứng minh race/baseline và nguồn có cập nhật thực tế để chứng minh lịch chạy; không đăng nội dung thử lên tài khoản người dùng chỉ để tạo mẫu.

Đo riêng: thời gian tới candidate đầu tiên, tổng thời gian 20 candidate, request/page/detail count, peak RAM/process count, download throughput, retry/auth failures, tỷ lệ duplicate và recovery. Chốt SLO theo baseline P0; không lấy tốc độ quảng cáo của ViralCrawl làm số đo.

Fixture/unit/integration pass là gate kỹ thuật, **không thay thế live gate**. Nếu chưa có tài khoản/cookie/API key hoặc nguồn từ chối mạng, báo đúng blocker của operation; không tự tuyên bố thành công bằng mock.

## 11. Phát hành, rollback và công việc bắt đầu

- Feature flag `CONTENT_BOT_ACQUISITION_ENABLED` đã có trong config và capability response. Mặc định code là `true` để không hồi quy checkout hiện tại; deployment production nên đặt `false` trước khi mở canary. Khi tắt, API chặn run/selection/resume mới và scheduler không tạo scan mới; list/cancel và tắt subscription vẫn hoạt động.
- Migration chỉ thêm schema/index/field ở đợt đầu, có backup và version; không xóa keyword channels/job JSON cũ. Chạy dry-run migration/recovery trước khi bật trên data thật.
- Tắt flag: ngừng nhận acquisition mới và schedule mới, drain/pause run theo policy; download đã được nhận vẫn theo manager hiện có, dữ liệu/video không bị xóa.
- Rollback UI/provider không rollback mất dữ liệu. Test bản cũ đọc job có field bổ sung và kiểm tra chuyện run đang dở trước khi thay binary.
- Retention đề xuất: lịch sử run 30 ngày, raw diagnostic tối thiểu đã redact 7 ngày, partial file 7 ngày sau terminal state; chỉ dọn file do app sở hữu, không xóa video hoàn tất tự động. Candidate/kênh đang được subscription hoặc asset tham chiếu không bị prune theo run.
- Bằng chứng live lưu bản tổng hợp/sanitized; cookie, browser profile, raw response có token và media tải về không đưa vào Git.

Việc nên làm đầu tiên khi chuyển sang implementation: **P0 rồi P1/P2**, hoàn chỉnh một đường Bilibili/YouTube từ nhập kênh → xem danh sách → chọn tải → vào thư viện. Sau đó nhân rộng provider và thêm theo dõi tự động trên cùng contract. Chưa cần viết lại engine hay đụng vào pipeline OCR.

Tài liệu vận hành cần cập nhật khi implementation đạt gate: [VIDEO_LIBRARY_DOWNLOADS.md](VIDEO_LIBRARY_DOWNLOADS.md) và [INTERNAL_MULTIPLATFORM_CRAWLER_PLAN.md](INTERNAL_MULTIPLATFORM_CRAWLER_PLAN.md). Các nguồn upstream trong kế hoạch được tra cứu ngày 2026-09-21; cần đối chiếu lại lúc tích hợp dependency/provider.
