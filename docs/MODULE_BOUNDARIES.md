# Ranh giới module sau refactoring

## Backend

- `app/main.py` chỉ dựng app, middleware, lifespan và đăng ký router. `AppServices` tạo tài nguyên lúc startup, giữ quyền sở hữu và thứ tự shutdown; handler lấy service từ request dependency.
- `api/runs.py`, `items.py`, `insights.py`, `subtitles.py`, `videos.py` giữ HTTP validation/response. `api/media_paths.py` dùng chung quy tắc tên/path media. Query, analysis, cache, job và thumbnail nằm trong service/storage; không tạo manager ở module router.
- `storage_protocol.py` mô tả contract chung cho SQLite/Mongo. `mongo.create_store()` chọn implementation; router không cần import loại database. Protocol không nâng guarantee Mongo standalone thành transaction. Revision cho cache là capability bổ sung của SQLite.
- `services/connector_contracts.py` chứa query, item, status, capabilities và interface connector. Module cần các kiểu này không cần import toàn bộ provider.
- `services/connector_<provider>.py` chứa từng implementation; `connector_command.py` xử lý adapter subprocess. `connector_support.py` chứa transport/retry và normalization dùng chung. `connectors.py` đăng ký provider và giữ public imports tương thích. Adapter/licensing/provenance hiện có được giữ nguyên.
- Test cần thay transport tại module sử dụng nó hoặc qua client factory của HTTP pool. Không thêm nhánh production để phát hiện monkeypatch.

## Frontend

- `App.tsx` điều phối lựa chọn và hành động của ứng dụng; `AppShell.tsx` trình bày điều hướng/tài khoản. `features/topics` sở hữu snapshot/dashboard refresh và giao diện phân tích. `features/library`, `features/sources`, `features/live-wall` chứa giao diện tương ứng; `features/shared/presentation.ts` là helper thuần dùng chung.
- `api/types.ts` chỉ chứa shared types. `transport/client.ts` sở hữu base URL, bearer header, JSON/error handling. `api/topics.ts`, `library.ts`, `subtitles.ts`, `settings.ts` xuất từng operation. `api.ts` giữ facade tương thích qua namespace exports để bundler loại được operation không dùng. SSE dùng `api/runsStream.ts` và `transport/eventStream.ts`.
- Studio dùng `subtitles/useJobPolling.ts`, `useDraftPersistence.ts`, `useSubtitleImport.ts`, `useRenderedVideoDownload.ts`, `StudioToolbar.tsx`, `studioConfig.ts`. State chỉnh sửa/timing/undo vẫn có một chủ sở hữu; hook điều phối lifetime và hủy request, toolbar nhận dữ liệu/hành động qua props.
- `voiceover/useVoicePlayback.ts` gắn vòng đời React với `VoicePlaybackEngine`; engine sở hữu audio graph/decks, `PlaybackIndex` tìm clip/cửa sổ và `AudioAssetCache` quản lý tải, LRU, byte budget và cleanup. Thay document/volume trong cùng project không tạo lại graph. Cache budget giới hạn Blob URL lưu giữ, không phải toàn bộ RAM của decoder/browser.
- Modal, library, live wall, login/approval và Studio được import khi cần. CSS Settings, login và Canva Studio đi cùng chunk feature. CSS shell, trạng thái đang xác thực và role badge vẫn nằm trong entry để tránh thiếu style ở màn hình ban đầu. Đây là giảm payload ban đầu, chưa phải phép đo tốc độ tương tác của toàn ứng dụng.

## Bằng chứng

- OpenAPI trước/sau tách router giống nhau cho 77 path; snapshot local ở `artifacts/refactor-modules/openapi-before.json`.
- `app_modals_smoke.py` kiểm tra app production thật trong Chromium với API/auth fixture, gồm Settings/Admin/Update, library/video, Studio/theme, login và pending approval. Có kiểm tra computed CSS và module chưa tải trước khi mở.
- `studio_hooks_smoke.py` kiểm tra React hook thật: single-flight, callback mới nhất, bỏ stale response, terminal stop, unmount abort, debounce draft, hủy import cũ và bearer token khi desktop export.
- `test_subtitle_jobs.py` tái hiện thứ tự future hoàn tất trước done callback. Shutdown chủ động retire dưới lock; callback đến muộn vẫn an toàn.
- Chạy các script browser bằng môi trường Python có Playwright; hook smoke cần Vite, app smoke nhận `--url` của Vite preview. Fixture không gọi tài khoản/API/database người dùng. Kết quả local không thay thế CI remote hoặc profiling playback.
