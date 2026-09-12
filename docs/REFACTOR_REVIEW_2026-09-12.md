# Review và kế hoạch refactoring Content Bot

Ngày review: 12/09/2026. Mốc Git: `bbae3a7`, **bao gồm thay đổi chưa commit trong workspace**. Đây là bản review và kế hoạch; chưa áp dụng refactoring vào mã ứng dụng.

Ưu tiên lớn nhất là tầng lưu trữ và tính đúng đắn khi chạy đồng thời. SQLite đang được dùng như kho JSON: truy vấn phổ biến đọc toàn collection, giải mã JSON rồi lọc trong Python. Chỉ tách file hoặc thêm memoization React sẽ không giải quyết chi phí này.

## Phạm vi và giới hạn

- Đọc kiến trúc, các đường truy vấn/ingestion, crawler orchestration, HTTP pool, auth, job lifecycle, dashboard, subtitle/voice playback và cấu hình CI.
- Chạy backend tests, frontend tests/lint/build, desktop policy tests và voice runtime unit tests.
- Đo SQLite trên DB tạm với dữ liệu giả; tái hiện lỗi HTTP pool và cập nhật checkpoint bằng chương trình cô lập. Không benchmark trên DB người dùng.
- Chưa đo end-to-end trên dữ liệu thật, React Profiler, CPU/GPU inference, tải nhiều người dùng hoặc crawler online. Chưa chạy auth-server integration suite riêng.
- Các báo cáo tháng 8 trong `docs/` là dữ liệu lịch sử, không phải kết quả hiện tại. Thay đổi chưa commit có thể đang trong quá trình triển khai; lỗi dưới đây mô tả snapshot đã review, không quy trách nhiệm cho commit nào.

## Kiến trúc hiện tại và phần nên giữ

| Thành phần | Vai trò | Quy mô mã đã đếm |
| --- | --- | ---: |
| `backend/app` | FastAPI, storage, crawler, analytics, media jobs | 150 file Python, 43.313 dòng |
| `frontend/src` | React/Vite, dashboard, editor, voiceover | 64 file TS/TSX, 16.407 dòng |
| `auth-server/app` | Tài khoản, JWT, OAuth, release API | 13 file Python, 1.520 dòng |
| `desktop` | Electron, quản lý browser views, dev runner | 6 file JS, 1.167 dòng |
| `runtimes/voiceover` | Worker/model trong môi trường riêng | Đọc worker và tests; không tính model tải về vào mã ứng dụng |

Nền tảng đã có nhiều điểm tốt: SQLite WAL, crawler concurrency theo nhóm 8/3/1, `asyncio.to_thread` cho nhiều thao tác storage, HTTP client reuse, checkpoint/dedupe, job persistence, cache media, lazy-load Subtitle Studio/VideoLibrary, virtualized subtitle list và playback store riêng. Clustering cũng đã có inverted index cho link/token; không nên mô tả thuật toán hiện tại như một vòng lặp so sánh mọi cặp vô điều kiện.

Giữ các cơ chế này và bổ sung contract tests trước khi đổi nội bộ. Chưa có bằng chứng cần chuyển sang microservices, Redis, một database khác hoặc thêm thư viện quản lý state chỉ để tối ưu.

## Kết quả kiểm tra hiện tại

Môi trường local: Windows, Python backend 3.13.6, Node 24.18.0. CI hiện dùng Python 3.11 và Node 22, nên kết quả local không thay thế được clean-install CI.

| Kiểm tra | Kết quả |
| --- | --- |
| `backend/.venv/Scripts/python.exe -m pytest -q` | **897 passed, 1 failed, 2 skipped**, 82,91 giây |
| `backend/.venv/Scripts/python.exe -m ruff check backend --output-format concise` | **58 lỗi** |
| `npm run test` tại `frontend` | **70 passed / 15 file** |
| `npm run lint` tại `frontend` | **1 lỗi**, `SubtitleStudio.tsx:1407`, `no-empty` |
| `npm run build` tại `frontend` | **Pass** |
| `npm run desktop:test` tại `frontend` | **6 passed** |
| `runtimes/voiceover/.venv/Scripts/python.exe -m pytest -q runtimes/voiceover/tests` | **16 passed** |

Lần chạy voice runtime tests bằng Python backend có 2 lỗi thiếu `soundfile`; chạy lại bằng đúng môi trường runtime thì cả 16 test qua. Đây là khác biệt môi trường, không phải bằng chứng lỗi worker.

Test backend lỗi là `backend/tests/test_voice_reference.py:25`: request `start_seconds=4&duration_seconds=3` trả `duration_ms=8000`, kỳ vọng 3000. `backend/app/api/voiceover.py:88` chưa nhận/truyền hai tham số này, trong khi `convert_reference()` ở `backend/app/services/voiceover/audio.py:50` đã hỗ trợ. Cần thống nhất contract API với test và UI.

Ruff có cả lỗi ảnh hưởng thực thi/tooling: `Callable` chưa import ở `backend/app/mongo.py:581`, và lỗi indentation ở `backend/scripts/voiceover_check_long_mp4.py:39,59`. `Callable` hiện nằm trong annotation hoãn đánh giá; không kết luận rằng lỗi này làm mọi request crash. Các lỗi style còn lại nên sửa riêng để diff chức năng dễ review.

## Phát hiện ưu tiên

### F1 — P1: Quét toàn collection, N+1 và vòng đời kết nối SQLite

**Bằng chứng:** `backend/app/sqlite_store.py:256` (`_all`), `:272` (`_find`), `:640` (`item_by_source`), `:938` (`item_matches`). `_find()` giải mã mọi document của collection. `item_matches()` tiếp tục gọi `item()` cho từng match, mỗi lần mở một connection. `match()`, `snapshots()` và đường ingest cũng dùng kiểu truy vấn này.

Phép đo gọi method trực tiếp, mỗi N có N content item và N match cùng keyword, payload nhỏ, lấy median của 3 lần:

| N | Tìm item theo source/external ID | Đọc item_matches | Connection mở khi đọc item_matches | JSON decode |
| ---: | ---: | ---: | ---: | ---: |
| 100 | 0,86 ms | 68,40 ms | 101 | 200 |
| 1.000 | 4,57 ms | 494,75 ms | 1.001 | 2.000 |
| 5.000 | 20,17 ms | 2.990,51 ms | 5.001 | 10.000 |

Đây **không phải latency HTTP**, chưa gồm scoring, Pydantic, network hay React. Thời gian có nhiễu từ máy đang chạy test; số connection và decode là bằng chứng cấu trúc đáng tin cậy hơn một tỷ lệ tăng tốc dự đoán.

Ngoài ra, `with self._connect() as connection` chỉ quản lý transaction, không đóng connection khi ra khỏi block. Lần benchmark đầu không xóa được DB tạm trên Windows vì handle còn mở; cần GC mới dọn được. Hành vi context manager này được xác nhận trong [tài liệu Python sqlite3](https://docs.python.org/3/library/sqlite3.html#how-to-use-the-connection-context-manager).

**Đề xuất:** connection scope có `close()` tường minh; cùng một connection cho toàn unit of work. Thêm query chuyên biệt bằng SQL và index cho `(source_id, external_id)`, `(keyword_id, content_item_id)`, `(content_item_id, captured_at)`, cùng các khóa filter/sort thực tế. Có thể bắt đầu bằng indexed generated columns hoặc expression indexes trên JSON, giữ payload và ID để giảm phạm vi migration; chỉ tách bảng nếu benchmark/schema cần. Thay N+1 bằng JOIN/bulk read. Index chỉ giúp khi truy vấn SQL sử dụng nó; thêm index nhưng vẫn gọi `_all()` sẽ không sửa vấn đề.

### F2 — P1: Cập nhật checkpoint có thể ghi đè lẫn nhau

**Bằng chứng:** `backend/app/sqlite_store.py:381,407`. `_get()` và chỉnh sửa document diễn ra trước khi `_upsert()` lấy write lock. Hai worker có thể đọc cùng phiên bản keyword rồi lần lượt ghi toàn document, làm mất checkpoint của nguồn kia. Luồng crawler chạy đồng thời và gọi storage qua thread nên đây là đường thực thi có thể gặp.

**Đã tái hiện:** dùng barrier buộc hai thread đọc cùng keyword, cập nhật checkpoint cho `youtube` và `web`; kỳ vọng 2 khóa, kết quả chỉ còn 1. Đây là chứng minh interleaving gây lỗi, chưa đo tần suất xảy ra trong sử dụng thật.

**Đề xuất:** bao trọn read-modify-write trong transaction và khóa thích hợp; ưu tiên atomic field update hoặc optimistic concurrency bằng revision. Áp dụng cùng contract cho cập nhật channel, session counter và các thao tác tương tự. Nếu hỗ trợ nhiều process/Store instance, RLock của một instance là chưa đủ. Test bắt buộc: cập nhật hai nguồn giữ cả hai checkpoint; tăng counter không mất lượt; rollback không để dữ liệu dở dang.

### F3 — P1: HTTP pool bỏ qua cấu hình khi tái sử dụng client

**Bằng chứng:** `backend/app/services/http_pool.py:34,44`. Cache key chỉ gồm base URL, `User-Agent` và `Authorization`; bỏ qua timeout, redirect và các header khác. `**extra` được nhận nhưng không truyền vào constructor. Caller thứ hai có thể nhận cấu hình của caller thứ nhất.

**Đã tái hiện, không gửi network:** tạo client với timeout 1/redirect false/Accept JSON, sau đó yêu cầu timeout 99/redirect true/Accept HTML. Cùng một instance được trả về; timeout vẫn 1, redirect vẫn false, Accept vẫn JSON. Trong caller thật, Mastodon deep-health dùng timeout 10 (`connectors.py:2891`), scan/comment dùng 30 (`:2943,3059`) với cùng pool key.

`connectors.py:30` còn đổi sang đường tạo client riêng khi `httpx.AsyncClient` bị monkeypatch. Vì vậy nhiều connector tests có thể bỏ qua chính đường pooling dùng khi chạy thật.

**Đề xuất:** explicit client factory theo provider và cấu hình bất biến; chuẩn hóa headers, phân biệt cấu hình ảnh hưởng hành vi hoặc đưa timeout/header phù hợp xuống từng request; truyền hoặc từ chối rõ tham số chưa hỗ trợ. Inject `MockTransport` qua cùng factory trong test. Thêm test khác timeout/header/redirect và credential rotation. HTTPX xác định cấu hình client áp dụng cho các request dùng client đó: [HTTPX Clients](https://www.python-httpx.org/advanced/clients/#sharing-configuration-across-requests).

### F4 — P1: Quality gate chưa đủ để bảo vệ refactoring

**Bằng chứng:** kết quả kiểm tra phía trên; `.github/workflows/ci.yml` chưa chạy Vitest, desktop tests, auth-server tests hoặc voice runtime tests. `backend/tests/conftest.py:23` mặc định thay application store bằng MongoStore/mongomock; có SQLite tests riêng nhưng phần lớn API tests không chạy trên storage mặc định của sản phẩm.

`backend/app/middleware/auth.py:7` import `jwt` trực tiếp, nhưng `backend/pyproject.toml` chưa khai báo PyJWT. PyJWT xuất hiện trong requirements của auth-server, là môi trường riêng. Đây là rủi ro clean install; chưa chạy một fresh venv để xác nhận lỗi khởi động.

**Đề xuất:** làm xanh baseline trước; khai báo dependency trực tiếp, pin cấu hình lint và matrix runtime rõ ràng. Tạo một bộ storage contract tests chạy trên SQLite và MongoStore, kiểm tra uniqueness, sorting, pagination, checkpoints, atomicity và migration. Mongo `ingest_content_bundle()` hiện gọi nhiều thao tác riêng dù docstring ghi “Atomically”; cần định nghĩa guarantee thật và kiểm thử khi một bước lỗi, không giả định hai adapter có transaction semantics giống nhau.

### F5 — P2: Dashboard lặp lại truy vấn và tính insights trước phân trang

**Bằng chứng:** `backend/app/main.py:373,400,531`. `item_output()` tính lại `content_insights`; `filtered_item_outputs()` tạo toàn bộ output; `/items` chỉ slice ở cuối. `/insights/summary` và `/insights/clusters` gọi lại cùng luồng. MongoStore đã bulk-read item nhưng tầng API vẫn chịu chi phí này.

`frontend/src/App.tsx:444,498,573` gọi items/summary/runs/clusters riêng. Khi batch chạy, SSE refresh và interval 5 giây (`:702`) cùng hoạt động; `refreshSnapshot()` chưa có khóa chống request chồng nhau. Các effect tải dữ liệu cũng không được chặn theo tab đang hiển thị.

**Đề xuất:** một `ItemQuery`/filter contract dùng chung cho list, summary, clusters và export. Materialize insights theo content hash + analysis version; invalidation khi nội dung thay đổi. Đẩy filter/sort/count/page xuống repository; chỉ hydrate page cần trả. Không được phân trang trước rồi mới áp language/sentiment/topic filter vì sẽ sai total và thiếu kết quả. Tách summary/cluster read model hoặc snapshot cache có revision, không cache vô hạn.

Frontend có một hook điều phối dữ liệu, request dedupe, abort, xử lý stale response, refresh theo tab và fallback polling khi stream không khỏe. Chọn polling sau khi request trước kết thúc để tránh tích tụ khi API chậm. Giữ một lần refresh kết quả cuối batch và cùng semantics filter cho export.

### F6 — P2: Khởi tạo và shutdown tài nguyên chưa đối xứng

**Bằng chứng:** `backend/app/main.py:134` trở đi tạo manager lúc import; `SubtitleJobManager.__init__()` gọi `_load_existing()` (`subtitle_jobs.py:84`) và có thể ghi lại trạng thái job. Lifespan ở `main.py:193` đóng voiceover/live-wall/login/HTTP pool nhưng không gọi shutdown cho `subtitle_jobs` và `gemini_subtitle_jobs`, dù method tồn tại ở `subtitle_jobs.py:248`. RunManager cũng chưa có lifecycle shutdown tổng cho batch tasks. Pool đóng trước khi scheduler bị cancel.

**Tác động:** import có side effect, fixture phải đặt data-dir trước import; stop/reload có thể còn job/thread giữ tài nguyên; tài nguyên dùng chung có thể bị đóng khi producer còn chạy. Chưa đo thời gian shutdown thực tế.

**Đề xuất:** `create_app()` + `AppServices` được tạo trong lifespan; import thuần. Shutdown theo thứ tự: ngừng nhận/tạo việc → dừng scheduler → cancel/drain jobs và crawler có deadline → đóng HTTP/storage. Đưa manager retention vào thiết kế vì `_records`, `_futures`, `_cancel_events` hiện không có eviction cho job hoàn tất. Giữ metadata lâu dài trên đĩa, giới hạn phần active trong RAM. Đây phù hợp cơ chế [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/).

### F7 — P2: Auth cold path chặn event loop; SSE không gắn bearer token

**Bằng chứng:** async middleware `backend/app/main.py:250` gọi sync `verify_token()`. Nếu chưa có public key, `middleware/auth.py:21` gọi sync HTTP với timeout 5 giây. Dependency `get_current_user()` cũng verify lại thay vì dùng `request.state.user` đã có.

`frontend/src/App.tsx:687` dùng native `EventSource` với URL thuần. Middleware yêu cầu `Authorization: Bearer` cho `/runs/{id}/events`; route này không nằm trong danh sách exempt. Vì vậy khi auth enabled, đường stream hiện không đáp ứng contract header của backend và polling phải gánh việc cập nhật. Kết luận này từ call path; chưa chạy browser integration cho auth trong lượt review.

**Đề xuất:** tải key bất đồng bộ/cached, chống nhiều request tải key cùng lúc và dùng payload đã verify trong request. Chuẩn hóa streaming transport hỗ trợ auth, ví dụ fetch streaming cùng API client hiện tại; test reconnect, token hết hạn và cancel. Không đưa bearer token dài hạn vào URL.

### F8 — P2: Component/module lớn và bundle entry tăng trở lại

**Bằng chứng:** `App.tsx` 3.060 dòng, `SubtitleStudio.tsx` 2.114, `SettingsModal.tsx` 1.095; backend `connectors.py` 3.256, `main.py` 1.677, `runs.py` 1.335. Đây là chỉ báo tập trung trách nhiệm, không tự nó chứng minh chậm.

Build hiện tại: entry JS **350,59 kB / gzip 102,14 kB**; entry CSS **109,55 kB / gzip 20,74 kB**. Baseline tháng 8 ghi 242,43/75,55 kB JS và 53,02/10,80 kB CSS. Sản phẩm đã thêm tính năng, nên chỉ kết luận payload lớn hơn, chưa suy ra mức giảm tốc page-load.

**Đề xuất:** chia theo feature và trách nhiệm, không theo số dòng tùy ý:

- `AppShell` + `features/topics`, `features/sources`, `features/live-wall`, `features/library`; state dữ liệu vào hook tương ứng.
- Studio tách project/draft persistence, job polling, import/export, toolbar; giữ timing/undo/redo trong modules thuần hiện có.
- API frontend tách HTTP/auth/error transport, feature clients và types; cân nhắc sinh types từ OpenAPI sau khi contract ổn định.
- Backend tách routers `runs`, `items`, `insights`, `subtitles`, `videos`; query/application services độc lập HTTP; store protocol rõ ràng.
- Connector contracts tách khỏi implementations theo provider; HTTP/retry/checkpoint shared giữ ranh giới rõ, bảo toàn module provenance/licensing.
- Lazy-load modal/view ít dùng và CSS theo feature sau khi xem bundle breakdown. Không lazy-load từng icon hoặc thêm `memo` đại trà.

### F9 — P2: Voice playback vẫn có công việc O(N) mỗi frame và tái tạo audio graph

**Bằng chứng:** `frontend/src/voiceover/useVoicePlayback.ts:67,81` binary-search cue hiện tại nhưng sau đó `.filter()` toàn clips cho cửa sổ preload mỗi RAF, kể cả khi paused. Effect phụ thuộc toàn `doc` và volume (`:221`); thay document hoặc chỉnh volume sẽ đóng AudioContext, revoke blob URLs rồi tạo lại. `useVoiceover.ts` thay document từ polling nên có đường kích hoạt việc này. Comment “only current/next” cũng không còn đúng với cache 25–30 clip và sliding window thực tế.

**Đề xuất:** giữ audio graph qua vòng đời project/video; cập nhật gain và document index bằng refs/commands. Dùng index cửa sổ/pointer thay full filter mỗi frame; cập nhật preload khi đi qua biên clip hoặc seek. Giới hạn cache theo tổng byte và số request tải song song, không chỉ số clip. Khi pause, tạm dừng RAF và đánh thức qua event seek/play/edit. Phải test seek, rate, overlap semantics, mute/duck và đổi project; chỉ cam kết hiệu quả sau khi đo profiler trên 500/2.000 cue.

### F10 — P2: Thumbnail chưa có giới hạn công việc và chống tạo trùng

**Bằng chứng:** `backend/app/main.py:1594` dùng exists-check rồi chạy `subprocess.run()` ở `:1631`, không timeout/lock và ghi trực tiếp vào final JPG. Nhiều request miss cùng lúc có thể tạo nhiều FFmpeg process cho cùng video; thư viện nhiều video có thể tạo tải đột biến. Endpoint là sync nên không kết luận nó trực tiếp chặn event loop, nhưng nó chiếm worker và tài nguyên FFmpeg.

**Đề xuất:** single-flight theo video/fingerprint + semaphore, timeout và cleanup process, file tạm rồi atomic replace. Thêm pagination/metadata catalog cho `/videos` nếu phép đo thư viện lớn cho thấy directory scan/stat là đáng kể. Không chạy toàn bộ thumbnail eagerly chỉ để đổi cách tổ chức code.

## Kế hoạch triển khai thành các PR nhỏ

Ước lượng dưới đây dành cho một developer quen dự án, gồm test và review; là khoảng lập kế hoạch, không phải cam kết lịch. PR-04 có thể làm sau PR-00 mà không chờ storage migration. Mỗi PR cần baseline xanh và diff tập trung.

| PR | Nội dung và đầu ra | Phụ thuộc | Ước lượng |
| --- | --- | --- | --- |
| 00 | Sửa contract voice reference, syntax/import/lint; dependency PyJWT; CI chạy các suite phù hợp và lưu baseline | — | 1–2 ngày |
| 01 | Connection lifecycle, atomic checkpoint/counter, storage contract tests và race/rollback tests | 00 | 1–2 ngày |
| 02 | Migration index có version; lookup SQL; JOIN/bulk read; query plan và benchmark ingestion | 01 | 3–5 ngày |
| 03 | ItemQuery, insights versioning, filter/count/page ở storage, thống nhất summary/clusters/export | 02 | 2–4 ngày |
| 04 | Client factory đúng cấu hình, test transport dùng đường production; sửa auth cold path và streaming auth | 00 | 2–3 ngày |
| 05 | App factory/lifespan, shutdown ordering, queue admission và active-job retention; thumbnail single-flight | 00, 01 | 2–3 ngày |
| 06 | Hook refresh dashboard, request dedupe, visibility gating, SSE fallback/backoff, test chuyển keyword/filter nhanh | 03, 04 | 1–2 ngày |
| 07 | Tách feature UI/router/provider, shared types; lazy-load modal/CSS dựa trên bundle report | 05, 06 | 3–5 ngày |
| 08 | Profile voice/subtitle, ổn định audio graph, indexed prefetch và cache budget nếu benchmark xác nhận | 07 | 1–3 ngày |

Tổng tham khảo **16–29 ngày công**, khoảng 3–6 tuần làm việc. Nếu chỉ làm một đợt đầu, chọn **00 → 01 → 02**, kèm 04 khi có thể: xử lý baseline, mất checkpoint và chi phí truy vấn trước khi chia nhỏ toàn bộ UI.

## Nghiệm thu và đo hiệu năng

Các ngưỡng sau là **mục tiêu đề xuất**, chưa phải kết quả đã đạt. Sau PR-00 cần đo baseline end-to-end để điều chỉnh theo máy mục tiêu.

| Đường đo | Dataset/kịch bản | Tiêu chí |
| --- | --- | --- |
| Item lookup | 1k/10k/100k item | Query plan dùng index, decode tối đa item cần trả; không gọi `_all()` |
| Items page | 50 item/page, nhiều keyword và 100k item tổng | Số query/connection không tăng theo số match; mục tiêu warm API p95 < 200 ms |
| Snapshot/summary/clusters | Cùng filter, ingest/update/delete xen kẽ | Totals, membership, export giữ đúng semantics; cache invalidation có kiểm thử |
| Ingestion | 1k item mới + 1k trùng trên DB 100k | Không mất/nhân đôi dữ liệu; ghi items/s và p95, đặt mục tiêu tăng ít nhất 3x sau khi có baseline tương ứng |
| Concurrency | 2–8 nguồn cập nhật cùng keyword | Giữ toàn bộ checkpoint/counter; thử cả nhiều Store instance nếu được hỗ trợ |
| Dashboard refresh | API nhanh/chậm, SSE reconnect, đổi tab/keyword | Tối đa một refresh đang chạy cho một query; bỏ stale response, không nhân request sau reconnect |
| Editor/voice | 500/2.000 cue, play/pause/seek/drag/volume | Đo frame time, long tasks, audio discontinuity, RAM; mục tiêu ≥55 FPS và không tăng RAM không giới hạn |
| Startup/shutdown | Có job queued/running và history lớn | Import không ghi job files; stop có deadline, không còn process con do app sở hữu; báo riêng ready time |
| Frontend build | Production bundle cùng cách đo | Ban đầu giữ JS entry gzip ≤105 kB; mục tiêu sau tách feature ≤85 kB, CSS gzip ≤16 kB nếu UX không bị flash |

Đo tối thiểu 30 lần cho p50/p95 của API, tách cold/warm cache, giữ nguyên fixture, ghi Python/Node/CPU/disk và tải nền. Không dùng median của 3 lần microbenchmark phía trên để tuyên bố p95 hoặc mức tăng tốc end-to-end.

## Migration và rollback

1. Chốt snapshot thay đổi đang làm trước khi triển khai, để mỗi PR có đầu vào và rollback rõ.
2. Trên DB fixture/copy, thêm schema/index theo migration version, giữ ID và payload cũ; backfill theo batch. Quét và xử lý duplicate trước khi thêm unique index.
3. So sánh read path cũ/mới bằng aggregate/count/hash trên fixture: item ordering, filter, checkpoint, export. Không chạy hai write path thiếu cơ chế đồng bộ.
4. Dùng feature flag cho query/read model mới trong giai đoạn chuyển đổi. Nếu schema bổ sung còn tương thích, rollback bằng flag; nếu đã đổi write schema, cần quy trình restore/migration ngược đã diễn tập.
5. Kiểm tra backup/restore nhất quán với WAL trước mọi schema migration dữ liệu người dùng; không drop legacy tables hoặc xóa dữ liệu trong đợt tối ưu đầu.

## Công việc trì hoãn cho đến khi có số đo

- Worker voice giữ model warm giữa nhiều job: có khả năng giảm model-load latency nhưng giữ RAM/VRAM và làm lifecycle phức tạp hơn. Đo model load so với inference trước khi chọn.
- Thay thread pool bằng process pool, tăng concurrency crawler/FFmpeg, hoặc chuyển database: chỉ quyết định sau CPU profile, quota và lock-contention measurements.
- Đổi thuật toán clustering: hiện đã có inverted index; cần benchmark worst-case token phổ biến trước khi thêm thuật toán hoặc cache mới.
- Chạy lại UI/GPU benchmark lịch sử bằng script hiện có sau khi baseline xanh. Các số tháng 8 không bao phủ luồng voiceover vừa thay đổi.
