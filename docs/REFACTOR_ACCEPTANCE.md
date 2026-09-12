# Nghiệm thu kế hoạch refactoring

Trạng thái: **hoàn tất triển khai và nghiệm thu local PR-00 → PR-08**. Phạm vi là code và nghiệm thu local theo kế hoạch gốc, không bao gồm tạo PR/commit, deploy hay migration dữ liệu người dùng. Mọi benchmark sử dụng fixture riêng.

## Đối chiếu kế hoạch

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

## Kiểm tra cuối

- Backend fresh environment: **969 passed, 2 skipped**, 81,65 giây; `artifacts/refactor-modules/backend-acceptance.xml`. Hai skip: live Bilibili cần opt-in và Windows không tạo được symlink. Một deprecation warning từ Starlette/AnyIO.
- Auth **12**, voice worker **16**, frontend **89**, desktop **6** test qua; Ruff/ESLint và TypeScript/Vite production build qua. CI remote chưa chạy.
- ASS renderer cũng qua browser smoke trên Vite dev (`editor-ass-dev-smoke.json`); số đo production được báo riêng. Kiểm tra cuối Ruff, ESLint và diff whitespace qua, OpenAPI equality 77 path xác nhận lại sau mọi thay đổi.
- OpenAPI equality: snapshot `artifacts/refactor-modules/openapi-before.json`; test subprocess xác nhận import/OpenAPI không tạo DB hoặc sửa job files.
- Chromium production: 9 view (Settings/Admin/Update/library/video/Studio/theme/login/pending) qua, CSS/lazy loading đúng; `artifacts/refactor-modules/production-browser-final.json`.
- Studio hooks: 8 trường hợp polling/draft/import/export qua, 3 job request; `artifacts/refactor-modules/studio-hooks-final.json`. Dashboard: 24 request, peak cùng endpoint/query bằng 1, không lỗi; `artifacts/refactor-dashboard/browser-final.json`.
- Voice graph: `artifacts/refactor-playback/semantics-final.json` kiểm tra seek/gain/duck/mute/rate/thay asset/đổi project/cleanup; 73 mẫu steady-state, 0 mẫu im lặng bất thường, drift tối đa **20,95 ms**. Không thay thế nghe TTS hoặc kiểm tra mọi biên clip.

## Hiệu năng đo được

Máy local: Windows 26200, i5-13420H (8 core/12 thread), RAM 16 GB, NVMe WD SN740; Python 3.13.6, Node 24.18.0, Chromium 149.0.7827.55. Chi tiết ở `artifacts/refactor-hardware.json`. Các phép đo cuối chạy tuần tự, không chạy suite nặng đồng thời; không kiểm soát mọi tiến trình hệ điều hành.

| Đường đo | Kết quả | Phạm vi/diễn giải |
| --- | --- | --- |
| Lookup 1k/10k | Median 1,345/2,124 ms; 1 connection, 1 document decode | 3 lần; `read-final-small.json`, không gọi đây là p95. |
| Lookup 100k | 469,54 → 4,51 ms; 100k → 1 decode | `large-comparison.json`, baseline Git `bbae3a7`, microbenchmark trước bước analysis. |
| Item matches 100k | 49.757,69 → 1.421 ms; 100.001 → 1 connection | JOIN read microbenchmark, trả toàn population, không phải page API. |
| Items API, 100k/10 keyword/page 50 | Warm p95 530,168 → **156,049 ms** không filter; 474,258 → **199,443 ms** topic; 473,620 → **169,887 ms** language+sentiment | 30 warm samples/filter/mode, ASGI gồm middleware/router/serialization, không network. 100 decode cho 50 item+match thay vì 20.000; 2 connection không tăng theo số match. Đạt mục tiêu p95 <200 ms ở fixture này. |
| Ingestion 1k mới | 90,476 → **76,729 giây**, 13,03 item/s, p95 98,835 ms | Comparator chỉ thay cách đọc snapshot population; persistence hiện tại ở cả hai mode. Nhánh mới chưa có hai observation nên không chứng minh lợi ích bulk scoring từ chênh lệch này. |
| Ingestion 1k trùng | 270,955 → **85,261 giây**, 11,73 item/s, p95 106,984 ms; 10.141.000 → 41.000 câu SQL | **3,18×** nhanh hơn; tổng mới+trùng **2,23×**, chưa đạt mục tiêu đề xuất 3× toàn luồng. Có relevance/scoring/write/counters, không provider/network. |
| Entry bundle | JS gzip 102,14 → **83,37 kB**, CSS gzip 20,74 → **13,76 kB** | Giảm tải ban đầu qua lazy features/CSS; không tuyên bố tổng tất cả feature nhỏ đi. |
| Playback hook | Volume edits: thêm 10 → **0 AudioContext**; pause 600 ms: 38/37 → **0 tick**; peak asset loads 26/50 → **3** | `before.json`/`after-final.json`, fixture 500/2k. Tick CPU 241,5/250,2 → 68,4/67,3 ms; baseline đã ~60 FPS nên không tuyên bố tăng FPS tương ứng. |
| Editor ASS 500/2k | Play, drag+volume và seek+scroll **~60 FPS**, p95 frame interval 16,7–16,8 ms, không long task trong các đoạn đo | `editor-ass-final.json`, production renderer đã active; ready editor 2.234/573 ms, ready gồm chờ renderer 2.956/1.286 ms, không phải cold-start thống kê. |

Ingestion kiểm tra cùng hash điểm số `ff894a8d623599ed6ab6fad91e775346c7d566d36e439f4049427e8892f57d2a`, 101.000 item, 1.000 match và 2.000 snapshot; counter giữ 1.000 ingested/2.000 fetched. Artifact: `artifacts/refactor-storage/ingestion-legacy.json`, `ingestion-bulk-isolated.json`. API: `artifacts/refactor-item-query/api-final-isolated.json`.

### Startup và shutdown

`artifacts/refactor-lifecycle/history-final.json`: import 762 ms, không tạo file. Mỗi dataset có thêm hai job queued/running, được đánh dấu interrupted đúng khi recover.

| History hoàn tất | Ready lần đầu | Hai lần restart | Record trong RAM | Shutdown không có job đang chạy |
| --- | --- | --- | --- | --- |
| 0 | 81,88 ms | 49,62 / 9,33 ms | 2 | 0,57–1,14 ms |
| 1.000 | 17,29 giây | 121,57 / 108,72 ms | 128 | 0,58–0,59 ms |
| 10.000 | 168,78 giây | 1,162 / 1,064 giây | 128 | 0,62–0,88 ms |

Lần đọc history đầu tiên vẫn chậm trên máy này; phép đo chưa tách chi phí filesystem/cache/antivirus nên không quy nguyên nhân cho riêng SQLite hay JSON. SQLite dedupe index đã bỏ hàng nghìn file index nhỏ; chưa loại scan JSON lịch sử. Không dùng số shutdown idle để khẳng định job inference dừng tức thì: timeout/cancel/kill được kiểm tra riêng bằng lifecycle tests. Thread không hợp tác được báo lỗi deadline, không giả định có thể kill thread.

### Giới hạn phép đo media

Video fixture 32×32/2 FPS, voice tone 800 ms, 500/2.000 cue. Không suy ra hiệu năng HD/4K, GPU, model loading hoặc chất lượng TTS. Native-fallback report trước đó có tương tác 2k ~54 FPS; giữ lại ở `editor-native.json`, không dùng để đại diện ASS renderer đã sửa.

`artifacts/refactor-playback/editor-ass-memory.json`: 12 vòng × 100 seek cho mỗi dataset; heap sau GC 500 cue 6,79 → 7,91 MB, 6 vòng cuối 7,90–7,92 MB; 2k cue 8,76 → 9,69 MB, 6 vòng cuối 9,67–9,69 MB. Peak Blob URL ~2,46 MB, một AudioContext/project và đóng đủ khi về trang chính, không page error. Playback/drag/volume/seek segments đều ~60 FPS; số FPS này không bao gồm mọi vòng memory hoặc thời gian GC cưỡng bức. Đo main-page JS heap, chưa bao gồm heap riêng của renderer worker.

Hook cuối giữ 0 RAF khi paused, 0 graph tạo thêm khi chỉnh volume, peak load 3 và cleanup blob/fetch về 0. Có một long task 57 ms ở hook fixture 500 cue; editor ASS production không có long task trong các đoạn tương tác ngắn đã đo.

Cache budget giới hạn Blob URL được giữ, không bao gồm mọi response Blob tạm, decoder, WebAssembly hoặc GPU allocation. Heap sau GC trong một phiên hữu hạn chỉ cho biết xu hướng quan sát được, không chứng minh không thể rò rỉ ở mọi phiên dài.

## Rollback và bảo toàn dữ liệu

- Snapshot đầu vào: `artifacts/refactor-start/worktree.patch`; các thay đổi sẵn có của người dùng được giữ. Chưa commit, tạo PR, deploy hoặc chạy migration trên DB người dùng.
- Schema SQLite bổ sung, giữ legacy payload/ID; `CONTENT_BOT_INDEXED_ITEM_QUERIES=false` chuyển về fallback read path. Không chạy hai write path cạnh tranh để benchmark.
- Backup: `backend/scripts/backup_sqlite.py SOURCE DESTINATION` dùng SQLite online backup, integrity check, từ chối ghi đè; test restore với WAL đã commit qua.
- Job `.dedupe.sqlite3` là index dựng lại được từ JSON, không thay file kết quả; test xóa/rebuild index giữ cold dedupe với cache RAM bằng 0. Đổi cấu trúc nội bộ không nâng guarantee Mongo standalone thành transaction.

## Phạm vi chưa cam kết

Mongo contract chạy với mongomock, chưa benchmark Mongo server thật. CI đa nền tảng đã cấu hình nhưng chưa có kết quả remote. Các mục kế hoạch gốc ghi trì hoãn (warm model worker, process pool/database/concurrency mới, clustering mới, GPU profiling lịch sử) tiếp tục là nghiên cứu dựa trên tải thực tế. Không coi các mục đó là đã triển khai.


## Chạy lại kiểm tra

Từ root repository trên Windows (môi trường cần cài dependency theo `backend/pyproject.toml`; browser scripts cần Playwright/Chromium và FFmpeg):

```powershell
backend/.venv/Scripts/python.exe -m pytest -q
backend/.venv/Scripts/ruff.exe check backend
backend/.venv/Scripts/python.exe backend/scripts/benchmark_ingestion.py --size 100000 --count 1000 --output artifacts/refactor-storage/ingestion-repeat.json
backend/.venv/Scripts/python.exe backend/scripts/benchmark_item_api.py --size 100000 --repeats 30 --output artifacts/refactor-item-query/api-repeat.json
backend/.venv/Scripts/python.exe backend/scripts/benchmark_lifecycle.py --output artifacts/refactor-lifecycle/history-repeat.json
npm --prefix frontend run lint
npm --prefix frontend run test
npm --prefix frontend run desktop:test
npm --prefix frontend run build
```

Browser hook scripts dùng Vite port 5187; editor/app production dùng preview port 5188. `benchmark_voice_playback.py --output artifacts/refactor-playback/after-repeat.json` tạo `fixture.mp4` cần cho editor/semantics scripts. Chạy lần lượt các benchmark, không đồng thời với suite nặng. Báo cáo JSON trong `artifacts/` là output local ignored; scripts và tài liệu là phần reviewable của worktree.
