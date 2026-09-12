# Tiến độ refactoring

Kế hoạch gốc: [Review 12/09/2026](REFACTOR_REVIEW_2026-09-12.md). Phạm vi toàn bộ PR-00 đến PR-08 vẫn được giữ; bảng này ghi kết quả thực tế theo từng bước. Các nhãn PR là đơn vị triển khai, chưa phải pull request đã xuất bản.

## Trạng thái cuối

**Hoàn tất triển khai và nghiệm thu local PR-00 → PR-08.** Kết quả cuối và các mục tiêu chưa đạt được ghi tại [báo cáo nghiệm thu](REFACTOR_ACCEPTANCE.md). Các đoạn phía dưới giữ lịch sử kiểm tra theo thời điểm; trạng thái “đang/còn” trong nhật ký cũ đã được thay thế bởi bảng này.

| Hạng mục | Thay đổi và bằng chứng |
| --- | --- |
| PR-00 | Sửa contract voice reference, dependency PyJWT, baseline lint/test; CI Linux/Windows và job auth/voice runtime đã cấu hình. Fresh environment `pip check` đạt; suite cuối bên dưới. |
| PR-01 | Connection đóng tại operation boundary; SQLite transaction atomic cho checkpoint/counter/item/snapshot/match. `test_sqlite_transactions.py` kiểm tra rollback, đọc write chưa commit trong cùng transaction, 2/8 Store instance và 8 nguồn cập nhật một keyword. `test_storage_contract.py` đối chiếu SQLite/Mongo và partial write Mongo standalone. |
| PR-02 | Additive versioned indexes, lookup SQL, JOIN và bulk snapshot population thay N query trong scoring. Query-plan/payload/ID/idempotence/WAL backup-restore tests qua. Lookup 1k/10k/100k và ingestion 1k mới + 1k trùng đã đo. |
| PR-03 | ItemQuery dùng chung list/count/page/summary/clusters/export; analysis hash/version, backfill batch, SQLite snapshot/revision cache và Mongo compare-and-set. `test_item_query.py` kiểm tra fallback parity, API membership/export, update/version invalidation và writer race; `test_insight_cache.py` kiểm tra revision, expiry, budget và isolation. |
| PR-04 | HTTP pool theo full config, event loop và app scope; test gọi provider bằng transport production. Auth async key cache/single-flight/backoff và verify một lần; SSE fetch có bearer, parser, reconnect và cancel được kiểm tra. |
| PR-05 | App factory/lifespan không khởi tạo tài nguyên lúc import; stop admission → scheduler → drain jobs → close pool/storage. Test hai app/restart, failed startup cleanup, storage thread drain, voice child process kill/reap thật và thumbnail FFmpeg single-flight/timeout/admission. Job retention 128, queue bounded, dedupe index SQLite dựng lại từ JSON; legacy results không đổi byte. History benchmark 0/1k/10k bên dưới. |
| PR-06 | Dashboard refresh controller single-flight, coalescing, stale/abort, visibility gating, SSE fallback/backoff và terminal refresh. 6 controller tests cùng browser hook API chậm/đổi keyword/visibility qua. |
| PR-07 | AppShell/feature UI, Studio hooks/toolbar/config, frontend transport/feature clients/types; backend routers/providers/storage protocol. OpenAPI giữ nguyên 77 path. Production lazy-view/CSS smoke và hook smoke có artifact; entry bundle đạt mục tiêu 85/16 kB gzip. |
| PR-08 | Graph theo project/video, indexed prefetch, cache 16 MiB/64 asset và 3 load đồng thời; pause ngừng RAF. Index/cache tests và browser transport/gain/seek/overlap-policy/cleanup qua. Sửa JASSUB trỏ nhầm WASM loader thành worker; để thư viện/Vite bundle RPC worker. Profile ASS thật 500/2k cue đạt ~60 FPS trong fixture. |

## Bảo toàn đầu vào

- Các thay đổi đã có của người dùng được giữ trong workspace. Bản diff trước triển khai nằm ở `artifacts/refactor-start/worktree.patch` (local, ignored).
- Không chạy migration hoặc benchmark trên database người dùng. Mọi test storage dùng DB tạm.
- Auth tests override cấu hình Mongo và signing keys trong bộ nhớ trước khi app/database được import.

## Kiểm tra đã chạy

- Backend fresh venv: `../artifacts/refactor-verify-env/Scripts/python.exe -m pytest -q --junitxml=../artifacts/refactor-http/backend-tests.xml` tại `backend` → 950 passed, 2 skipped, 73,60 giây. Có một deprecation warning từ Starlette/AnyIO.
- Ruff: `backend/.venv/Scripts/python.exe -m ruff check backend` → pass sau sửa import cuối.
- Frontend: `npm run lint`, `npm run test`, `npm run build`, `npm run desktop:test` tại `frontend` → pass.
- Auth: `.venv/Scripts/python.exe -m pytest -q` tại `auth-server` → 12 passed, 10,24 giây.
- Voice: `runtimes/voiceover/.venv/Scripts/python.exe -m pytest -q runtimes/voiceover/tests` → 16 passed.
- SQLite regression: `backend/.venv/Scripts/python.exe -m pytest -q backend/tests/test_sqlite_transactions.py backend/tests/test_sqlite_store.py` → 10 passed; 4/5 test transaction mới đã fail trên implementation cũ trước khi sửa.

Entry bundle sau PR-06: JS 354,31 kB / gzip 103,53 kB; CSS 109,55 kB / gzip 20,74 kB. Frontend 83 test / 17 file, desktop 6 test qua; TypeScript/Vite build và lint qua. Chưa tuyên bố tăng tốc end-to-end.

PR-07 đang làm: modal Settings/Admin/Update chỉ import khi mở; CSS Settings chuyển từ entry sang chunk feature. Build hiện tại entry JS **304,00 kB / gzip 93,34 kB**, CSS **98,49 kB / gzip 18,89 kB**; Settings chunk JS 36,17 kB / gzip 7,32 kB, CSS 11,06 kB / gzip 2,27 kB. Chưa đạt mục tiêu đề xuất 85/16 kB gzip, còn tách feature. `backend/scripts/app_modals_smoke.py` chạy app thật trong Chromium với auth/API fixture: xác nhận Settings/Admin chưa được tải ở entry, mở Settings mới tải module và overlay có `position: fixed`, không page error. Artifact `artifacts/refactor-modules/modal-browser.json` và `.png`; không dùng tài khoản/API/database người dùng.

## Read model, migration và rollback đã triển khai

- SQLite giữ nguyên `local_documents` và ID; thêm expression indexes, bảng analysis/dirty/revision. Legacy backfill xử lý từng batch 128 item ở lần query đầu. Trigger vô hiệu hóa projection/cache cả khi một phiên bản cũ ghi documents. Count, page và kiểm tra dirty dùng cùng read snapshot; test mô phỏng legacy write ngay giữa refresh và BEGIN.
- `CONTENT_BOT_INDEXED_ITEM_QUERIES=false` chuyển query sang fallback dùng chung ItemQuery, không cần xóa dữ liệu hoặc bảng bổ sung. Cấu hình đã có trong `.env.example`; chưa sửa `.env` người dùng.
- Mongo lưu projection nội bộ cùng item bằng compare-and-update trên các trường đầu vào. Backfill dữ liệu thiếu/khác analysis version, aggregation áp filters trước count/page; export dùng cursor để tránh giới hạn kích thước một document `$facet`. Mongo contracts chạy bằng mongomock, chưa benchmark Mongo server thật. Khi chạy song song với phiên bản Mongo writer cũ hoặc ghi trực tiếp DB, dùng flag fallback; projection không có trigger như SQLite.
- Summary/clusters SQLite cache tối đa 32 entry / 8 MiB / 30 giây mỗi Store; revision DB vô hiệu hóa khi item hoặc match thay đổi. Mongo chưa cache aggregate vì standalone không có revision transaction chung; vẫn dùng analysis materialized.
- Backup: `backend/scripts/backup_sqlite.py SOURCE DESTINATION` sử dụng SQLite online backup và integrity check, không chép riêng file DB khi WAL hoạt động. Script từ chối đè file; test backup/restore với dữ liệu đã commit trong WAL đã qua. Chưa chạy trên DB người dùng.

## Benchmark và giới hạn diễn giải

- `artifacts/refactor-storage/read-comparison.json`: lookup cuối 5k 26,77 → 1,73 ms; item_matches 2.315,55 → 73,35 ms. Đây là method microbenchmark, median 3 lần, không phải HTTP p95.
- `backend/scripts/benchmark_storage.py`: fixture riêng, so sánh Git `bbae3a7`, có phép đo persistence bundle mới/trùng. Không gồm crawler/scoring. `_trend_score` vẫn tính percentile trên nguồn và duyệt snapshot theo item; chưa tuyên bố tăng tốc ingestion end-to-end.
- `backend/scripts/benchmark_item_api.py`: gọi production middleware/router qua ASGI, fixture 100k item/10 keyword, page 50, 30 warm request cho mỗi bộ lọc; không chạy lifespan/jobs và không có network/browser. Lần đầu p95 fallback 491–541 ms, indexed 195–207 ms; mới chạm ngưỡng đề xuất nên đang giảm JOIN/hydration trước sort. Ghi rõ có benchmark baseline storage chạy nền cùng lúc; số đo không đại diện máy rảnh.
- `artifacts/refactor-item-query/api-comparison-v2.json`: sau CTE phân trang trước khi hydrate, p95 indexed 144,35 / 176,54 / 172,06 ms tương ứng không filter / topic / language+sentiment; fallback 545,20 / 469,75 / 493,25 ms. Decode 100 thay vì 20.000 document cho page 50; connection luôn 2. Sau phép đo có thêm ORDER BY ngoài CTE để bảo đảm thứ tự SQL tường minh; regression tests qua, sẽ đo lại ở nghiệm thu cuối.
- `artifacts/refactor-storage/large-comparison.json`: DB 100k; baseline Git lookup 469,54 ms, item_matches 49.757,69 ms/100.001 connection; PR-02 tương ứng 4,51 ms và 1.421,00 ms/1 connection (mỗi đường 1 mẫu, không gọi là p95). Persistence 1k mới: 847,42 → 5,50 giây; 1k trùng: 827,96 → 4,96 giây. Snapshot này chạy trước khi thêm analysis materialization.
- `artifacts/refactor-storage/current-with-analysis.json`: đo lại implementation có read model PR-03, 1k mới 18,72 giây (53,4 item/s, p95 từng bundle 28,52 ms), 1k trùng 17,84 giây (56,1 item/s, p95 33,63 ms). Kiểm tra số lượng cuối: 101.000 item, 101.000 match, 1.000 snapshot; không nhân đôi item/match/snapshot khi replay. Chi phí write tăng so với snapshot PR-02 và tải nền khác nhau; dùng số hiện tại để lập baseline tiếp theo, không quảng bá tốc độ PR-02 cho code có analysis.

## Kết quả PR-05 và PR-06

- `services/video_thumbnails.py`: gộp miss theo output, tối đa 2 process/16 yêu cầu khác nhau, chờ slot tối đa 2 giây; timeout FFmpeg theo config, kill/reap process, publish JPG và fingerprint qua file tạm. Endpoint trả 503/Retry-After khi đầy và 504 khi timeout. Thay video làm cache hết hiệu lực. Service shutdown đã nối vào AppServices/lifespan; test FFmpeg thật tạo JPEG và cache hit qua.
- `SubtitleJobManager`: giới hạn mặc định 32 job đang chạy/chờ và 128 record hoàn tất trong RAM; future/cancel-event/persist timestamp được dọn khi hoàn tất. Kết quả cũ vẫn đọc được và dedupe được reattach qua index file trên đĩa. Job bị từ chối khi queue đầy/stopping trả 503 ở API.
- Shutdown job đặt cancel, hủy job queued và chờ tối đa deadline; runner không hợp tác trả TimeoutError thay vì báo đã dừng. AppServices thu thập lỗi shutdown nhưng vẫn cố dọn các tài nguyên còn lại. Voice chờ hợp tác 10 giây rồi kill/reap process và thêm tối đa 5 giây cho future; test process Python thật đã xác nhận không còn process sống. Chưa khẳng định mọi loại inference chạy trong thread đều hủy tức thì.
- `create_app()` không tạo/recover manager khi import. AppServices được tạo lúc lifespan start; dependency lấy tài nguyên từ `request.app.state`. Router builders hỗ trợ resource providers, không giữ manager từ lần startup trước. Hai app dùng pool scope khác nhau ngay cả trong cùng event loop; auth middleware nằm trong scope để key-fetch client được đóng cùng app.
- Test import/OpenAPI trong subprocess so sánh file bytes trước/sau, không tạo DB hoặc sửa pending job; start/stop lại không nhân route/executor; startup lỗi vẫn cleanup; thứ tự shutdown; crawler drain storage thread khi coroutine caller bị cancel đều qua.
- Full fresh backend sau tích hợp: **961 passed, 2 skipped**, 66,25 giây; JUnit `artifacts/refactor-lifecycle/backend-tests.xml`. Sau đó thêm voice process test, chỉnh HTTP middleware scope và observe storage task errors; nhóm lifecycle/auth/pool/voice mới nhất **14 passed**. Ruff pass.
- `features/topics/useDashboardData.ts` quản lý query lifetime và visibility; `dashboardRefresh.ts` chỉ chạy một snapshot/query, gộp tín hiệu đang chờ, xử lý stream với auth, polling khi reconnect và backoff khi API lỗi. Snapshot cuối tải sau khi quan sát batch terminal, tránh đọc item trước khi ingestion hoàn tất; không tăng refreshKey để tải lại lần nữa.
- `backend/scripts/dashboard_refresh_smoke.py` chạy React hook thật qua Vite và Chromium 149.0.7827.55 với API in-memory cố tình bỏ qua AbortSignal. Kiểm tra đổi keyword 1→2→3, stale response, disable/enable view và refresh liên tiếp. 24 request, peak cùng endpoint/query **1**, không page error; artifact `artifacts/refactor-dashboard/browser.json`. Controller tests còn kiểm tra SSE reconnect/poll suppression, terminal exactly-once và retry backoff.


## Kết quả PR-07

Chi tiết trách nhiệm và cách chạy smoke: [Module boundaries](MODULE_BOUNDARIES.md).

- Backend router `runs/items/insights/subtitles/videos` đã tách khỏi main. Snapshot OpenAPI trước/sau bằng nhau trên **77 path**. Connector contracts độc lập implementation; 11 module provider/command, helper dùng chung và facade registry. Protocol storage độc lập loại database; giữ khác biệt transaction SQLite/Mongo standalone.
- Full fresh backend sau provider split và sửa race shutdown: **963 passed, 2 skipped**, 161,93 giây, JUnit `artifacts/refactor-modules/backend-final.xml`; một warning Starlette/AnyIO như trước. Sau đó chỉ thêm type protocol và đổi import dependency; 21 test storage contract/lifecycle/registry qua.
- Phát hiện và sửa race thực tế: future báo done trước callback retire, shutdown có thể trả về khi cache còn dư record. Shutdown nay retire các future done dưới lock; test cố tình trì hoãn callback xác nhận cleanup và late callback an toàn.
- `AppShell`, topics/library/sources/live-wall và các helper trình bày đã tách. Studio tách polling, draft, import/export, toolbar và cấu hình thuần. API chia transport/auth/error, feature clients, types; facade giữ public imports.
- Build mới nhất: entry JS **265,19 kB / gzip 83,37 kB**, CSS **71,13 kB / gzip 13,76 kB**. Đạt mục tiêu entry đề xuất 85/16 kB; CSS shell/auth-loading/role badge giữ trong entry. Mã/CSS các màn hình khác tải theo nhu cầu, không tuyên bố tổng bundle mọi feature nhỏ đi.
- Frontend **83 test**, desktop **6 test**, lint/build qua. Production Chromium 149: Settings/Admin/Update/library/video/Studio/theme/login/pending approval đều qua, computed style có hiệu lực và không page error. `artifacts/refactor-modules/production-browser.json` + screenshot.
- Chromium React hook: 3 job request, single-flight, latest callback, stale guard, terminal stop, unmount abort, draft debounce, cancel/stale import và authenticated desktop export qua; `artifacts/refactor-modules/studio-hooks.json`.
- Còn PR-08 và nghiệm thu hiệu năng cuối: scoring ingestion end-to-end, startup/history, API benchmark với app factory/current code. Chưa đánh dấu toàn bộ kế hoạch hoàn tất.


## PR-08 đang triển khai

- Baseline input playback được giữ tại `artifacts/refactor-playback/useVoicePlayback-before.ts` (nội dung đã có của người dùng trước bước 08). Benchmark mới `backend/scripts/benchmark_voice_playback.py` dùng Chromium 149, video MP4 thật 2.100 giây kích thước nhỏ, WAV tone 800 ms và 500/2.000 voice clip fixture; hoàn toàn local. Đây là hook benchmark, chưa phải full editor hoặc kiểm tra chất lượng nghe.
- `before.json`: 10 lần chỉnh volume tạo thêm **10 AudioContext** ở cả hai dataset. Trong 600 ms pause vẫn thêm **38/37 tick**. Tổng asset request **491/514**, đỉnh concurrency **26/50**. FPS khoảng 59,39/60,00; không có cơ sở nói baseline fixture này bị tụt FPS nặng.
- `VoicePlaybackEngine` sở hữu graph theo project/video; hook cập nhật document/volume mà không teardown. Index rebuild theo reference clip collection; chọn cửa sổ khi qua biên/seek thay vì filter toàn clips mỗi frame. Preview giữ policy latest-start cho overlap; việc đổi asset cùng clip ID được áp dụng. Swap standby trước khi prefetch clip tiếp để không ghi đè deck sắp dùng.
- `AudioAssetCache`: mặc định **16 MiB/64 entry**, **3 load đồng thời**, tính cả request đang abort chưa settle. Pin asset của deck; LRU evict theo byte/count; một attempt/asset/window tránh tải-evict liên tục khi window vượt budget. Giới hạn này áp dụng cache Blob URL; response Blob đang tải/giải mã có chi phí tạm ngoài cache, chưa tuyên bố hard cap toàn RAM.
- `after-v1.json`: volume edits tạo thêm **0 context**, pause thêm **0 tick**, tổng **193/189** request, concurrency đỉnh **3**. Tổng thời gian tick 241,5/250,2 → 68,5/56,7 ms trong kịch bản; đây không phải thời gian toàn bộ editor. FPS 59,86/60,00. Cleanup đóng đủ context, cache blob và fetch về 0, không playback error.
- Cache giữ tối đa khoảng 2,46 MB trong lần đo mới (cũ 1,31/1,35 MB), đổi lấy giữ được nhiều asset; không tuyên bố RAM giảm. Heap mẫu đầu/cuối chưa chứng minh memory plateau ở phiên dài. Có long task 277 ms trong lần 500 cue mới; cần tách startup và steady-state ở full editor profile.
- 6 test index/cache mới qua: latest-start overlap, offset/rate/end, seek xa/window bounded, overlap end không đơn điệu, request bỏ qua abort, eviction/pinning/byte budget, oversized asset và không refetch vô hạn. Build/lint qua. Còn kiểm tra browser seek/rate/mute/duck/đổi project và editor 500/2.000 cue trước khi chốt 08.


## Kiểm chứng bổ sung sau bước playback đầu tiên

- `voice_playback_semantics_smoke.py` dùng HTMLVideoElement/AudioContext thật, kiểm tra seek sync, gain, duck, voice-only/mix, mute video/voice, rate, thay asset giữ clip ID, disable/enable, đổi project và cleanup. Artifact `semantics.json`: **73 mẫu audio steady-state**, không mẫu im lặng ngoài dự kiến, drift lớn nhất **23,62 ms**. Đây không phải đánh giá chất lượng TTS hay toàn bộ biên clip.
- `editor-native.json`: production editor với native preview fallback, fixture video 32×32/2 FPS và tone 800 ms; 500/2k cue chỉ mount 4 hàng subtitle đang thấy. Playback ~60 FPS; tương tác 57,05/54,09 FPS, long task 85/127 ms. Blob cache ổn định ~2,46 MB qua 80 seek; heap sau GC tăng nhẹ giữa 4 vòng (500: 6,60→6,77 MB; 2k: 8,16→8,34 MB), gồm cả instrumentation/request log. Chưa diễn giải là memory plateau toàn renderer. Đang chuẩn bị chạy cùng fixture với ASS renderer mặc định.
- `services/snapshot_pairs.py` cùng `source_snapshot_pairs` trên SQLite/Mongo thay truy vấn snapshot từng item trong scoring bằng một query population. Giữ 2 observation mới nhất mỗi item; test so sánh với individual read có timezone khác nhau qua. Test SQLite xác nhận chỉ một SELECT cho population, không N SELECT theo item. Microbenchmark 100 item/10 mới/10 trùng cho **cùng hash điểm số** ở hai mode.
- `benchmark_ingestion.py` đang đo 100k/10 nguồn với 1k mới + 1k trùng, bao gồm relevance, trend scoring, persistence và counters. Comparator chỉ thay snapshot query, cùng code storage hiện tại. Chưa chốt tốc độ khi process còn chạy.
- `benchmark_lifecycle.py --histories 0`: import **1.304,53 ms**, không tạo DB/JSON; ready 106,84/38,26/15,39 ms (3 lần start), shutdown 3,17/1,62/0,63 ms; hai record queued/running được recover đúng. Còn history 1k/10k.
- Transaction tests mới nhất **8 passed**, gồm 8 nguồn cập nhật một keyword cùng lúc và counter nhiều Store instance với 2/8 worker. Snapshot/query/storage contract group **17 passed**. Frontend **89 passed**, build/lint qua ở mốc engine ban đầu; kiểm tra toàn bộ cuối còn phải chạy.
- Checklist chi tiết: [REFACTOR_ACCEPTANCE.md](REFACTOR_ACCEPTANCE.md). Goal vẫn active vì benchmark cuối/ASS editor/final suites chưa chốt.


## Nghiệm thu cuối ngày 12/09/2026

- Toàn bộ backend fresh suite **969 passed/2 skipped**, frontend **89**, auth **12**, voice worker **16**, desktop **6**; lint, pip check và production build qua. OpenAPI giữ nguyên 77 path.
- Ingestion có scoring trên 100k/10 nguồn: 1k mới 76,73 giây, 1k trùng 85,26 giây; duplicate nhanh hơn 3,18×, toàn lượt 2,23× so với comparator chỉ khôi phục N snapshot queries. Cùng score hash, item/match/snapshot/counter.
- API 30 warm samples/filter trên code cuối: p95 **156,05/199,44/169,89 ms**, page 50 chỉ decode 100 documents. Lookup 1k/10k: 1,345/2,124 ms, một decode.
- History benchmark phát hiện file dedupe nhỏ tạo startup I/O lớn. Chuyển sang `.dedupe.sqlite3`, một transaction khi backfill; test xóa/rebuild từ legacy JSON với cache RAM 0 qua. 10k history restart 1,06–1,16 giây, cache 128 record; lần đọc đầu vẫn 168,78 giây, được ghi là hạn chế còn lại.
- Browser phát hiện JASSUB dùng nhầm WASM loader làm RPC worker (có sẵn trong baseline); bỏ override để thư viện/Vite bundle worker đúng. ASS renderer production active; editor 500/2k ~60 FPS trong play/drag/volume/seek segments.
- Memory fixture 1.200 seek/dataset: heap sau GC các vòng cuối quanh 7,91/9,69 MB; Blob cache ~2,46 MB, context cleanup đủ. Audio semantics cuối 73 mẫu không silent bất thường, drift tối đa 20,95 ms. Không suy ra hiệu năng HD/4K hay chất lượng TTS.
- Production UI 9 view, Studio hook 8 trường hợp và dashboard peak request/query 1 đều qua. Artifact và giới hạn chi tiết ở báo cáo nghiệm thu. Không commit/PR/deploy hoặc migration dữ liệu người dùng.
