# Kế hoạch xây dựng Content Bot Crawler Engine độc lập

> Trạng thái 2026-08-13: kế hoạch đang ở Phase 11, tức giai đoạn thứ 12/12 vì lộ trình đánh số từ 0 đến 11. Nền tảng/adapter tối thiểu cho đủ 17 nguồn đã có mã và regression; cutover/xóa MediaCrawler vẫn chờ các gate live-auth/approval/two-canary được ghi ở cuối tài liệu.
>
> Ngày khảo sát: 2026-08-12
>
> MediaCrawler được khảo sát tại commit: 071c8c0acaece3e82f2532cffb19faeddc9ec1c3, ngày 2026-08-05
>
> Phạm vi đích: đủ 17 nguồn hiện có trên dashboard — YouTube, Game news, Steam reviews, Bluesky, Mastodon, Reddit, X, Xiaohongshu/RedNote, Douyin, Kuaishou, Bilibili, Weibo, Baidu Tieba, Zhihu, TikTok, Facebook và Instagram

## 1. Kết luận điều hành

Content Bot không nên nhúng, fork rồi đổi tên, hoặc mang toàn bộ MediaCrawler vào lõi dự án. Hướng phù hợp là xây một engine/provider layer mới, tạm gọi **Content Bot Crawler Engine** hay **CBCE**, tích hợp trực tiếp với pipeline hiện hữu của Content Bot và quản lý đủ 17 nguồn trong một registry.

Phạm vi 17 nguồn gồm ba nhóm:

- **Giữ và nâng cấp sáu connector hiện hữu:** YouTube, Game news, Steam reviews, Bluesky, Mastodon và Reddit.
- **Thay bridge MediaCrawler bằng bảy adapter độc lập:** Xiaohongshu/RedNote, Douyin, Kuaishou, Bilibili, Weibo, Baidu Tieba và Zhihu.
- **Bổ sung bốn nguồn bằng provider chính thức theo quyền:** X, TikTok, Facebook và Instagram.

Engine mới chỉ học các ý tưởng kiến trúc cấp cao đã chứng minh hữu ích:

- Adapter độc lập theo nền tảng.
- Trình duyệt giữ phiên đăng nhập; lớp transport lấy dữ liệu trong phạm vi phiên đó.
- Ba kiểu tác vụ chính: tìm kiếm, lấy nội dung cụ thể và lấy nội dung của creator.
- Phân trang có cursor, checkpoint và giới hạn cứng.
- Chuẩn hóa dữ liệu trước khi lưu.
- Đăng nhập bằng cửa sổ trình duyệt hiển thị, lưu profile riêng theo nền tảng.
- Hủy tác vụ và đóng đúng cây tiến trình do ứng dụng tạo ra.

Engine mới không tái sử dụng mã, selector, endpoint catalog, payload, chữ ký request, fingerprint, GraphQL document, fixture hay thuật toán vượt challenge của MediaCrawler. Việc xóa thư mục vendor sau khi sao chép mã không làm mất nghĩa vụ của một sản phẩm phái sinh.

Content Bot đã có phần lớn lớp ứng dụng cần thiết: RunManager, EventBus, MongoDB, checkpoint, dedupe, metric snapshot và UI theo dõi tiến trình. Vì vậy không xây lại CLI, FastAPI, WebUI, scheduler hoặc hệ thống lưu trữ riêng giống MediaCrawler.

Quyết định mặc định:

1. X dùng API chính thức trước; không dùng browser scraping làm đường mặc định.
2. TikTok tách biệt hoàn toàn với Douyin và dùng provider chính thức phù hợp quyền truy cập.
3. Facebook và Instagram dùng các provider Graph API/Meta được duyệt; không dùng cookie browser để thay cho quyền API.
4. Sáu connector đang chạy được bọc vào contract mới, không rewrite chỉ để đồng nhất kiến trúc.
5. Các nền tảng thật sự cần trình duyệt dùng Cốc Cốc/Chromium do Content Bot quản lý, profile riêng và đăng nhập thủ công.
6. Không tự giải CAPTCHA, slider, QR challenge hay né rate limit.
7. Mọi khả năng đều được khai báo theo từng operation, provider và quyền hiện có; giao diện không hứa một chức năng mà provider không cung cấp.
8. Triển khai cuốn chiếu theo từng adapter, chạy song song với bridge cũ, rồi mới tháo MediaCrawler.

## 2. Phạm vi nghiên cứu và bằng chứng

### 2.1 Snapshot đã khảo sát

Repo MediaCrawler tại vendor/mediacrawler sạch ở commit 071c8c0acaece3e82f2532cffb19faeddc9ec1c3. Snapshot có bảy adapter:

- Xiaohongshu/RedNote, mã nền tảng xhs.
- Douyin, mã nền tảng dy.
- Kuaishou, mã nền tảng ks.
- Bilibili, mã nền tảng bili.
- Weibo, mã nền tảng wb.
- Baidu Tieba, mã nền tảng tieba.
- Zhihu, mã nền tảng zhihu.

Snapshot này không có X, TikTok quốc tế, Facebook, Instagram hoặc sáu connector công khai/API sẵn có của Content Bot. Douyin và TikTok là hai sản phẩm, domain, auth, payload và chính sách khác nhau; không được coi một adapter là tên khác của adapter còn lại.

Các điểm vào đại diện đã đối chiếu:

- Factory và lifecycle: vendor/mediacrawler/main.py:50-67, 100-161.
- Hợp đồng nền mỏng: vendor/mediacrawler/base/base_crawler.py:26-126.
- Cấu hình toàn cục: vendor/mediacrawler/config/base_config.py:21-141.
- CLI ghi đè cấu hình: vendor/mediacrawler/cmd_arg/arg.py:154-367.
- CDP và browser lifecycle: vendor/mediacrawler/tools/cdp_browser.py:97-195, 250-505.
- Signal, cancellation và cleanup: vendor/mediacrawler/tools/app_runner.py:32-109.
- API subprocess manager: vendor/mediacrawler/api/services/crawler_manager.py:30-284.

Các dòng trên là dấu vết audit cho snapshot cụ thể, không phải dependency cần giữ lại sau cutover.

### 2.2 Catalog 17 nguồn hiện tại của Content Bot

API dựng catalog theo thứ tự trong backend/app/services/connectors.py:967-1020; frontend lấy danh sách động rồi render, không có một mảng 17 card riêng. Source ID phải được giữ ổn định để không phá dữ liệu:

| Thứ tự | Source ID | Nhãn | Trạng thái quan sát | Implementation hiện tại |
|---:|---|---|---|---|
| 1 | youtube | YouTube | ready khi có key | YouTube Data API v3 |
| 2 | web | Game news | ready | RSS/Atom, mặc định Google News RSS |
| 3 | steam | Steam reviews | ready | Steam public store/review endpoints |
| 4 | bluesky | Bluesky | ready | Public AT Protocol AppView |
| 5 | mastodon | Mastodon | ready | Public hashtag timeline trên một số instance |
| 6 | reddit | Reddit | not_configured nếu thiếu credential | Reddit application-only OAuth |
| 7 | x | X | embed có sẵn; API scan chưa cấu hình | Public embed + provider mới cần xây |
| 8 | xhs | Xiaohongshu | bridge ready cục bộ | MediaCrawler bridge |
| 9 | douyin | Douyin | bridge ready cục bộ | MediaCrawler bridge, transport alias dy |
| 10 | kuaishou | Kuaishou | bridge ready cục bộ | MediaCrawler bridge, transport alias ks |
| 11 | bilibili | Bilibili | bridge ready cục bộ | MediaCrawler bridge, transport alias bili |
| 12 | weibo | Weibo | bridge ready cục bộ | MediaCrawler bridge, transport alias wb |
| 13 | tieba | Baidu Tieba | bridge ready cục bộ | MediaCrawler bridge |
| 14 | zhihu | Zhihu | bridge ready cục bộ | MediaCrawler bridge |
| 15 | tiktok | TikTok | not_configured | No-op placeholder |
| 16 | facebook | Facebook | not_configured | No-op placeholder |
| 17 | instagram | Instagram | not_configured | No-op placeholder |

“Ready” hiện chủ yếu cho biết prerequisite cục bộ tồn tại; nó không bảo đảm credential còn hợp lệ, quota còn đủ, phiên đã đăng nhập hoặc endpoint từ xa khỏe. Thiết kế mới phải tách capability tĩnh khỏi availability theo operation.

Alias dy, ks, bili và wb chỉ được dùng ở transport/migration. Persisted source ID canonical vẫn là douyin, kuaishou, bilibili và weibo.

### 2.3 Kiến trúc thực tế của MediaCrawler

~~~text
Typer CLI hoặc WebUI
        |
        v
CrawlerFactory và config module toàn cục
        |
        v
Platform core
  + Browser persistent/CDP
  + QR, cookie hoặc phone login
  + Cookie bridge
  + HTTP, browser fetch hoặc internal web API
  + search, detail hoặc creator
        |
        v
Platform-specific normalizer
        |
        v
CSV, JSON, JSONL, Excel, SQL, Mongo hoặc media files
~~~

Đây là mô hình hybrid, không phải crawler DOM thuần:

1. Mở browser để tạo hoặc khôi phục phiên.
2. Kiểm tra trạng thái đăng nhập.
3. Chờ người dùng đăng nhập nếu cần.
4. Đồng bộ cookie vào client.
5. Gọi web endpoint hoặc browser fetch để lấy dữ liệu.
6. Chuẩn hóa theo từng nền tảng.
7. Ghi thẳng vào sink được chọn.

Tieba là ví dụ khác biệt: một số JSON request được thực thi ngay trong page bằng browser fetch. XHS, Douyin, Kuaishou, Bilibili và Zhihu có cơ chế ký request riêng. Đây là các vùng không được sao chép sang engine mới.

### 2.4 Điểm mạnh đáng tái tạo

- Profile đăng nhập lâu dài theo nền tảng.
- Browser bootstrap kết hợp client lấy dữ liệu.
- Adapter sở hữu logic đặc thù nền tảng.
- Hỗ trợ search, detail, creator, comment và subcomment.
- Cursor pagination và callback lưu từng batch.
- Semaphore cho fan-out, retry/backoff ở một số adapter.
- Chuẩn hóa trước khi persistence.
- Privacy fork đã bỏ creator profile nhạy cảm và băm định danh.
- Cleanup khi nhận signal là một yêu cầu hạng nhất.

### 2.5 Điểm yếu phải loại khỏi thiết kế mới

- Cấu hình là biến module mutable, không an toàn khi có nhiều run.
- Interface chung quá mỏng; workflow bị lặp ở từng core.
- Enum/capability lặp giữa CLI, API và UI, dễ lệch.
- Nhiều adapter tự nâng giới hạn lên kích thước một trang, nên max=1 vẫn có thể lấy 10 hoặc 20.
- Comment con thường không có budget riêng và có thể làm tổng kết quả vượt max.
- Không có cursor no-progress guard thống nhất.
- Retry, rate limit, lỗi auth và challenge không có taxonomy chung.
- Một lỗi item có thể hủy cả batch do gather không cô lập lỗi.
- Có chỗ gọi time.sleep trong async, làm nghẽn event loop.
- Một số creator loop không có global max hoặc delay.
- Cookie có thể xuất hiện trên command line, process list hoặc log.
- API không có auth nhưng có thể bind 0.0.0.0.
- File sink không dedupe; JSON rewrite toàn file; lock có phạm vi instance sai.
- SQL dùng select-then-write và nhiều external ID không có unique constraint.
- WebUI, API, DB và exporter của MediaCrawler trùng trách nhiệm với Content Bot.
- Dependency all-in-one kéo theo nhiều gói không cần thiết.

## 3. Ranh giới giấy phép, điều khoản nền tảng và clean-room

### 3.1 MediaCrawler

[License của snapshot MediaCrawler đã khảo sát](https://github.com/NanmiCoder/MediaCrawler/blob/071c8c0acaece3e82f2532cffb19faeddc9ec1c3/LICENSE) là giấy phép học tập phi thương mại, không phải giấy phép permissive thông dụng. Bản được khảo sát cho phép sao chép/sửa/merge cho mục đích học tập phi thương mại, yêu cầu giữ notice và không cấp quyền thương mại nếu chưa có chấp thuận bằng văn bản.

Do đó:

- Có thể dùng nghiên cứu này như đặc tả hành vi và bài học kiến trúc.
- Không chép Python, JavaScript, TypeScript, comment, test, fixture hoặc cấu trúc module đặc thù.
- Không đổi tên class/function rồi coi là mã mới.
- Không chép chữ ký XHS, a_bogus Douyin, capture hook Kuaishou, WBI Bilibili, MD5 secret Tieba hay x-zse/x-zst Zhihu.
- Không chép selector login, anti-detection script, fingerprint, endpoint constants hoặc GraphQL document.
- Mọi dependency bên thứ ba được cân nhắc phải được review trực tiếp theo license riêng của dependency đó.

Nếu dự án có mục đích thương mại, nên áp dụng clean-room nghiêm:

1. Nhóm nghiên cứu chỉ phát hành capability, input/output, error-state và test requirement.
2. Người triển khai không đọc source vendor; dựa trên tài liệu chính thức và traffic/DOM do chính tài khoản thử nghiệm được phép của dự án tạo ra.
3. Mỗi endpoint, field, selector hoặc dependency có một bản ghi provenance.
4. Fixture do dự án tự thu, được sanitize và không lấy từ test của MediaCrawler.
5. Trước cutover có similarity scan và review thủ công.
6. Khi còn nghi ngờ về quyền sử dụng, dừng provider đó và xin đánh giá pháp lý.

### 3.2 X

Nhận định “X chỉ cấm đăng bài tự động nên chỉ đọc dữ liệu không ảnh hưởng” **không đúng** với tài liệu hiện hành. [Điều khoản X](https://x.com/en/tos) hạn chế scraping nếu không có chấp thuận bằng văn bản, và [quy tắc automation của X](https://help.x.com/en/rules-and-policies/x-automation?lang=browser) yêu cầu dùng API, đồng thời cấm automation website ngoài API.

Vì vậy:

- Provider mặc định là [X API](https://docs.x.com/x-api/getting-started/about-x-api).
- Search dùng [Post Search](https://docs.x.com/x-api/posts/search/introduction) theo tier/quyền được cấp.
- Timeline dùng [Timelines API](https://docs.x.com/x-api/posts/timelines/introduction).
- Nội dung nhúng công khai có thể dùng [oEmbed](https://docs.x.com/x-for-websites/oembed-api) trong đúng phạm vi của endpoint đó.
- Chi phí/quota phải lấy động từ cấu hình và đối chiếu [pricing chính thức](https://docs.x.com/x-api/getting-started/pricing), không hardcode giả định cũ.
- Browser provider cho X mặc định disabled. Chỉ được bật như provider thử nghiệm khi chủ dự án có quyền rõ ràng, có policy gate và review riêng.
- Không reverse-engineer GraphQL nội bộ, token guest, chữ ký client, challenge hoặc cơ chế chống bot của X.

Đăng nhập trình duyệt không tự tạo ra quyền scrape.

### 3.3 TikTok

TikTok cần chọn provider theo loại dữ liệu và quyền:

- [Login Kit](https://developers.tiktok.com/doc/login-kit-overview/) chỉ giải quyết authorization, không tự cấp toàn bộ dữ liệu công khai.
- [Display API](https://developers.tiktok.com/doc/display-api-overview/) phù hợp lấy profile và video công khai của người dùng đã ủy quyền.
- [Research Tools](https://developers.tiktok.com/doc/about-research-api/) và [Research API](https://developers.tiktok.com/doc/research-api-get-started/) có query public data rộng hơn nhưng yêu cầu đơn vị/nghiên cứu viên đủ điều kiện và được phê duyệt.
- Query video phải theo [đặc tả chính thức](https://developers.tiktok.com/doc/research-api-specs-query-videos/) và quota được cấp.

Browser provider TikTok, nếu được cân nhắc, phải là đăng nhập thủ công, DOM/public-page có giới hạn, không vượt challenge và không tải media theo cách né bảo vệ/watermark.

### 3.4 Facebook và Instagram

Không có một Graph API sản phẩm chung cho phép quét toàn bộ Facebook và Instagram công khai theo từ khóa. Hai source này phải được chia thành provider theo access basis:

- **Facebook Page được user quản lý:** Facebook Login, Page access token, pages_show_list và pages_read_engagement. pages_read_user_content chỉ xin khi use case thật sự cần visitor posts/comments.
- **Facebook Page công khai không do user quản lý:** chỉ bật sau khi app được duyệt [Page Public Content Access](https://developers.facebook.com/docs/features-reference/page-public-content-access/), hoàn tất App Review và các yêu cầu xác minh liên quan.
- **Instagram Professional account được ủy quyền:** profile/media/comments/insights theo permission thực tế. Consumer/personal account không thuộc phạm vi provider này.
- **Instagram Business Discovery:** theo dõi allowlist tài khoản Business/Creator công khai; không phải global keyword search.
- **Instagram Hashtag Search:** discovery qua recent/top media, có quota hashtag và không phải firehose đầy đủ.
- **Meta Content Library/API:** adapter nghiên cứu riêng chỉ khi tổ chức/nghiên cứu viên đủ điều kiện và được duyệt; không coi là backend mặc định cho sản phẩm thương mại.

Tài liệu chính thức cần được pin trong provenance:

- [Pages API posts/feed](https://developers.facebook.com/docs/pages-api/posts/)
- [Pages API getting started](https://developers.facebook.com/docs/pages-api/getting-started/)
- [Meta permissions](https://developers.facebook.com/docs/permissions/)
- [Meta App Review](https://developers.facebook.com/docs/app-review/)
- [Page Webhooks](https://developers.facebook.com/docs/graph-api/webhooks/getting-started/webhooks-for-pages/)
- [Instagram API getting started](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/get-started/)
- [Instagram Hashtag Search](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/hashtag-search/)
- [Instagram Business Discovery](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery/)
- [Instagram Webhooks](https://developers.facebook.com/docs/instagram-platform/webhooks/)
- [Meta Content Library](https://transparency.meta.com/researchtools/meta-content-library/)
- [Meta Platform Terms](https://developers.facebook.com/terms/) và [Developer Policies](https://developers.facebook.com/devpolicy/)

Nếu permission/review chưa đạt, operation phải trả permission_required hoặc unsupported. Không fallback sang browser cookie scraping.

## 4. Mục tiêu và phi mục tiêu

### 4.1 Mục tiêu

- Bao phủ đủ 17 source ID trong một registry duy nhất, giữ tương thích dữ liệu hiện hữu.
- Giữ và nâng cấp sáu connector public/API hiện có thay vì viết lại không cần thiết.
- Thay bảy MediaCrawler bridge bằng adapter độc lập.
- Bổ sung X, TikTok, Facebook và Instagram bằng provider chính thức theo quyền.
- Hỗ trợ các mode khi provider cho phép: search, detail, creator, root comments, child comments và media metadata.
- Hỗ trợ keyword target và saved-channel target qua cùng manifest/run planner.
- Có thể bổ sung media download như capability riêng, không gắn cứng với crawl content.
- Tích hợp với RunManager, EventBus, MongoDB, checkpoint và UI hiện hữu.
- Chạy browser trong process cô lập, hủy an toàn và không để orphan process.
- Dùng profile riêng theo source/account.
- Giới hạn chính xác số item, request, comment, media byte và thời gian.
- Upsert idempotent, cập nhật metric snapshot và không tạo bản ghi trùng.
- Báo auth, challenge, rate limit và parser drift bằng lỗi có kiểu.
- Không lộ cookie, token, QR payload, profile path nhạy cảm hoặc response chứa PII trong log.
- Cho phép capability giảm cấp rõ ràng theo provider/quyền.

### 4.2 Phi mục tiêu

- Không tạo một ứng dụng MediaCrawler thứ hai bên trong Content Bot.
- Không xây WebUI/CLI/database/export engine riêng.
- Không thu thập follower/following graph.
- Không lưu mật khẩu, cookie thô, signature token hoặc browser storage vào MongoDB.
- Không tự giải CAPTCHA, slider hoặc challenge.
- Không xoay proxy để né giới hạn hay giả mạo fingerprint.
- Không cam kết full feature parity ngay lần phát hành đầu tiên.
- Không hứa global keyword search cho Facebook, Instagram hoặc TikTok khi provider hiện có chỉ hỗ trợ owned/allowlist/hashtag/research scope.
- Không coi mọi dữ liệu nhìn thấy trong browser là được phép tự động thu thập.
- Không dùng raw payload vô hạn như kho dữ liệu chính.

## 5. Hiện trạng Content Bot và điểm tích hợp

Content Bot đã có:

- RawContentItem tại backend/app/services/connectors.py:129.
- SourceConnector và SearchQuery trong cùng module.
- JsonlCommandConnector hiện là bridge tới MediaCrawler tại connectors.py:248.
- RunManager, EventBus và browser semaphore tại backend/app/services/runs.py:24-68.
- Checkpoint recent_ids và latest_published_at tại runs.py:362-498.
- Mongo upsert, unique index theo source_id/external_id và metric snapshots tại backend/app/mongo.py.
- Realtime progress được UI tiêu thụ.
- Cốc Cốc path, profile path, timeout và flow login hiển thị.

Audit connector hiện hữu phát hiện các khoảng trống P0:

- Global keyword source run luôn bắt đầu với checkpoint rỗng và không lưu provider cursor sau thành công; page_token YouTube, cursor Bluesky và after Reddit vì thế không resume giữa batch.
- Keyword connector và saved-channel scanner là hai registry/switch riêng, có thể lệch capability, domain, privacy và provider.
- Tieba chưa có domain detection; mobile Bilibili có thể không match; catch-all đường dẫn /@ có thể nhận nhầm Mastodon.
- Generic web URL có thể bị coi là feed dù không phải RSS/Atom.
- X bị loại bằng literal trong RunManager.
- Scheduler suy background safety chỉ từ requires_login.
- TikTok, Facebook và Instagram đang khai capability mong muốn trên no-op connector.
- Video library dùng alias dy/ks/bili trong khi ingestion dùng source ID canonical douyin/kuaishou/bilibili.
- Frontend chỉ xét source ready và requires_login; chưa xét operation thật sự được hỗ trợ.

Engine mới phải mở rộng các điểm đó thay vì thay thế chúng:

~~~text
Dashboard và API hiện hữu
          |
          v
RunManager + EventBus hiện hữu
          |
          v
CrawlerCoordinator mới
  + registry/capability
  + worker supervisor
  + checkpoint/budget
          |
          v
Platform adapter trong worker cô lập
  + browser session
  + provider transport
  + paginator/rate policy
          |
          v
Canonical crawl events
          |
          v
RawContentItem mapper + Mongo hiện hữu
~~~

Không special-case X trong RunManager. Khi adapter X đạt gate, bỏ filter đang loại source_id x tại backend/app/services/runs.py:142-146 và để registry/capability quyết định đường chạy.

Sáu implementation YouTube, Feed, Steam, Bluesky, Reddit và Mastodon tiếp tục dùng SourceConnector.search trong giai đoạn tương thích. Một ConnectorRegistration bọc implementation cũ và đăng ký cả keyword scanner lẫn channel scanner. Sau đó mới bổ sung ScanContext/report_checkpoint/report_warning mà không ép rewrite toàn bộ connector.

## 6. Ma trận capability và chiến lược provider

Ký hiệu:

- **Đủ**: có kế hoạch hỗ trợ bằng provider hợp lệ.
- **Một phần**: provider không bảo đảm toàn bộ dữ liệu.
- **Theo quyền**: chỉ bật khi tài khoản/app được cấp quyền.
- **Không mặc định**: không phát hành đường browser đó khi chưa qua policy gate.

| ID / nguồn | Discovery/search mục tiêu | Channel/creator | Detail/comments | Media | Provider ưu tiên | Auth |
|---|---|---|---|---|---|---|
| youtube / YouTube | Full-text video search | Channel uploads | Video detail; comment count trước, records sau | Thumbnail/video metadata | YouTube Data API v3 | API key hoặc OAuth nếu scope tương lai cần |
| web / Game news | RSS/Atom query từ feed allowlist | Saved feed | Entry detail qua canonical publisher URL; không comments | Enclosure/thumbnail nếu feed có | RSS/Atom public | Không |
| steam / Steam reviews | App lookup rồi recent reviews | Saved app | Review detail; comment count | Không ở MVP | Steam public endpoints | Không |
| bluesky / Bluesky | Full-text post search | Author feed | Post/thread; replies phase sau | Embed metadata | Public AT Protocol AppView | Không cho public data; auth tùy provider tương lai |
| mastodon / Mastodon | **Hashtag**, không quảng cáo full-text | Account feed theo instance | Status/context theo server hỗ trợ | Attachment metadata | Public Mastodon API từng instance | Không hoặc app token theo instance policy |
| reddit / Reddit | Submission search | Subreddit/user feed | Submission; comment tree có budget ở phase sau | Link/media metadata | Reddit official OAuth | Client credentials; user OAuth nếu scope cần |
| x / X | Theo tier/quyền X API | User timeline | Post/conversation/replies theo quyền | Media metadata | X API; oEmbed chỉ cho embed | Bearer/OAuth; browser disabled |
| xhs / Xiaohongshu | Theo web session được phép | Creator feed | Detail; root + child có budget | Metadata; download tùy chọn | Browser DOM/fetch được nghiên cứu độc lập | QR/cookie profile; challenge thủ công |
| douyin / Douyin | Theo provider/session | Creator feed | ID/URL/short URL; root + child | Metadata; download tùy chọn | Provider độc lập; không chép a_bogus | QR/cookie/phone nếu được hỗ trợ; slider thủ công |
| kuaishou / Kuaishou | Theo provider/session | Creator feed | Detail; root + child | Metadata trước | Provider độc lập; không chép capture hook | QR/cookie profile |
| bilibili / Bilibili | Keyword/time range | Creator videos; dynamics riêng | BV/URL; root + child | Metadata; bounded download | Public/official khi có; DOM/fetch fallback | Anonymous khi đủ; QR/cookie khi cần |
| weibo / Weibo | Theo loại search | Creator feed | Detail/long text; root, child có thể partial | Ảnh/video metadata theo provider | Public/mobile web trong phiên được phép | QR/cookie domain-scoped |
| tieba / Baidu Tieba | Keyword và forum là hai operation | Creator feed | Thread; root + child | Không ở MVP | DOM/browser fetch độc lập | QR/cookie profile |
| zhihu / Zhihu | Theo filter/type | Answers/articles/videos | Ba loại detail; root + child | Link/media metadata | Public/official hoặc browser DOM | QR/cookie profile |
| tiktok / TikTok | Research API nếu được duyệt; Display API không global search | Authorized user hoặc research account | Theo provider; comments chỉ khi scope cấp | Metadata; download disabled mặc định | Display API/Research API | OAuth + app/research approval |
| facebook / Facebook | Không global search chung; owned Page hoặc PPCA Page discovery | Owned/approved public Page feed | Posts/comments/engagement theo permission | Photo/video/link metadata | Graph API; PPCA; research provider tách biệt | Facebook Login, Page token, App Review |
| instagram / Instagram | Hashtag best-effort hoặc research; không full-text chung | Owned Professional hoặc Business Discovery allowlist | Media/comments/insights theo permission | Image/video/Reels metadata | Instagram API; research provider tách biệt | OAuth, Professional account, App Review |

Capability phải là dữ liệu runtime theo từng operation, không phải một boolean tĩnh. Ví dụ X có thể embed và lookup nhưng không search vì tier chưa đủ; TikTok Display API có creator của user đã ủy quyền nhưng không keyword search; Facebook có owned Page feed nhưng chưa có PPCA; Instagram có Business Discovery nhưng không được đọc consumer account.

### 6.1 Trạng thái operation

Mỗi operation trả ba lớp thông tin độc lập:

- **Coverage của provider:** full, partial hoặc unsupported.
- **Implementation state:** implemented hoặc planned.
- **Availability runtime:** ready, setup_required, auth_required, permission_required, degraded, rate_limited hoặc disabled_by_policy.

Một source có thể đồng thời có embed={full, implemented, ready}, search={full, implemented, permission_required} và creator={unsupported, implemented, disabled_by_policy}. Operation planned luôn bị disable và không có handler no-op. UI không được rút gọn các trạng thái đó thành một badge ready/not_configured duy nhất.

### 6.2 Mức bao phủ cam kết

“Đủ 17 nguồn” trong kế hoạch có nghĩa:

1. Đủ 17 ID trong canonical registry.
2. Mỗi source có ít nhất một operation hữu ích đã implemented, hoặc implemented nhưng permission_required với đường cấu hình/phê duyệt thực tế.
3. Không có card ready trỏ vào no-op.
4. Keyword search, channel tracking, embed và detail được phân biệt; không ép mọi source phải có global search.
5. Provider bị giới hạn phải hiển thị coverage disclaimer.
6. Source chưa được cấp quyền vẫn nằm trong catalog nhưng không được tính là run thành công.

## 7. Kiến trúc mục tiêu

### 7.1 Cấu trúc thư mục đề xuất

~~~text
backend/app/crawlers/
  contracts.py
  errors.py
  events.py
  registry.py
  manifests.py
  target_detection.py
  run_planner.py
  coordinator.py
  normalization.py
  checkpoints.py
  privacy.py

  compat/
    legacy_connector.py
    legacy_channel_scanner.py

  runtime/
    supervisor.py
    worker_protocol.py
    browser_manager.py
    auth_state.py
    profiles.py
    transports.py
    cookies.py
    rate_limiter.py
    paginator.py
    budgets.py
    media.py
    redaction.py

  sources/
    youtube/
    web_feed/
    steam/
    bluesky/
    mastodon/
    reddit/
    xhs/
      manifest.py
      adapter.py
      parser.py
      auth.py
    douyin/
    kuaishou/
    bilibili/
    weibo/
    tieba/
    zhihu/
    x/
      manifest.py
      official_api.py
      oembed.py
    tiktok/
      manifest.py
      display_api.py
      research_api.py
    facebook/
      manifest.py
      owned_pages.py
      public_pages.py
      research_api.py
    instagram/
      manifest.py
      owned_professional.py
      business_discovery.py
      hashtag_search.py
      research_api.py

backend/scripts/
  crawler_worker.py

backend/tests/crawlers/
  contracts/
  fixtures/
  integration/
  live/

docs/crawler-provenance/
  README.md
  source-provenance-template.md
~~~

Tên module là đề xuất, có thể điều chỉnh theo convention của repo. Nguyên tắc bắt buộc là core/runtime không được chứa selector, endpoint hay signing đặc thù nền tảng. Sáu connector hiện hữu có thể tiếp tục nằm trong connectors.py ở phase đầu và được đăng ký qua compat wrapper; di chuyển file chỉ làm sau khi contract ổn định.

### 7.2 Một nguồn sự thật cho source

Mỗi source có một manifest được backend và frontend dùng chung qua API:

~~~text
SourceManifest
  id
  legacy_aliases
  label
  order
  group
  domains
  providers
  content_kinds
  operations
  auth_modes
  metrics
  target_canonicalizers
  channel_scanner
  default_budgets
  background_safe_operations
  config_requirements
  coverage_disclaimer
  policy_state
~~~

Mỗi operation có provider, coverage, implementation state, runtime availability, auth requirement, checkpoint codec và CTA. Registry không rải enum source ở connectors.py, channel_scans.py, adapter script, video library và frontend. UI lấy manifest từ endpoint backend và ẩn/disable đúng action, kèm lý do như planned, permission_required, auth_required, partial hoặc unsupported.

Registry cũng là owner của:

- Domain/subdomain/short-link detection có boundary an toàn.
- Canonical source ID và legacy alias migration.
- Keyword scanner, channel scanner, detail resolver và embed renderer.
- Metric label/semantics, ví dụ Reddit score không tự động được gọi là like.
- Default-source policy và background safety.
- Health probe theo operation.

Contract test phải chứng minh có đúng 17 ID, đúng thứ tự, không duplicate và mọi action UI bật đều có backend handler thật.

### 7.3 Hợp đồng tác vụ

**CrawlRequest** tối thiểu:

- protocol_version, run_id, source_run_id.
- source_id, provider, operation.
- target_kind: keyword, feed, channel, creator, content_url, content_id hoặc hashtag.
- keyword/query hoặc target URL/ID.
- creator target nếu mode creator.
- account/profile reference, tuyệt đối không chứa cookie.
- sort, time range và content kind.
- item_budget, request_budget, deadline.
- max_root_comments, max_children_per_root, max_total_comments.
- media policy: none, metadata hoặc download.
- max_media_files, max_media_bytes.
- checkpoint và feature flags.

**OperationCapability** trả về trạng thái cho từng action:

- coverage: full, partial hoặc unsupported.
- implementation: implemented hoặc planned.
- availability: ready, setup_required, auth_required, permission_required, degraded, rate_limited hoặc disabled_by_policy.
- Provider đang dùng và lý do giảm cấp.
- Quota còn lại nếu provider cung cấp.
- Loại nội dung và metric được hỗ trợ.

**SourceAdapter** cung cấp các thao tác phù hợp:

~~~text
manifest()
probe(context)
normalize_target(target)
ensure_session(context)
search(request, cursor)
scan_channel(request, cursor)
fetch_detail(request, reference)
list_creator(request, cursor)
list_comments(request, reference, cursor, parent)
close()
~~~

Unsupported method không được giả yield rỗng; registry không route vào method đó. Mỗi thao tác phân trang trả records, next_cursor, has_more, request_cost và warnings. Không thao tác nào được tự đọc config toàn cục hoặc tự thay đổi max.

### 7.4 Run planner và target detection

Run planner nhận source + operation + target, không suy đường chạy từ một boolean requires_login:

1. Resolve source ID/legacy alias qua registry.
2. Canonicalize URL/ID và xác định target kind.
3. Chọn provider được cấu hình và policy cho phép.
4. Kiểm operation capability/availability.
5. Chọn execution lane: in-process API/public, isolated browser worker hoặc embed-only.
6. Áp budget, scheduler safety và checkpoint codec.
7. Trả executable plan hoặc typed reason/CTA.

Keyword target và channel target có thể cùng tồn tại trong một topic; có channel không được làm mất global-source runs. Channel run phải probe provider trước khi queue. Generic HTML URL không được route vào RSS parser nếu chưa xác minh content type/feed discovery.

Target detection dùng bảng domain chính xác, kiểm boundary chống tên miền gần giống, có test mobile/short-link. Tieba và m.bilibili.com phải được bao phủ; không dùng quy tắc “URL có /@ thì là Mastodon” nếu chưa xác minh instance.

### 7.5 Model chuẩn

**ContentRecord**:

- source_id, provider, content_kind và external_id.
- canonical_url.
- title, body, language/locale và hashtags.
- author_ref đã áp privacy policy.
- published_at UTC và observed_at UTC.
- metrics typed: view, like/reaction, comment, share, favorite/collect, repost, danmaku khi phù hợp.
- media_refs có type, public URL nếu được phép, dimensions, duration và content hash nếu đã tải.
- reply/repost/quote relationship khi nền tảng cung cấp.
- provenance gồm provider, parser_version và capture time; không chứa secret.

**CommentRecord**:

- source_id, external_id, content_external_id.
- parent_comment_id và root_comment_id.
- body, published_at, observed_at.
- author_ref đã áp privacy policy.
- like_count, reply_count và media refs.
- depth và partial flag.

**CreatorRef** chỉ giữ định danh tối thiểu phục vụ liên kết:

- source_id, stable pseudonymous ID và display label đã mask theo policy.
- Không lưu profile đầy đủ, follower graph, giới tính, IP location hoặc chữ ký cá nhân nếu không có requirement được duyệt.

Khóa idempotency:

- Content: source_id + entity_type + external_id.
- Comment: source_id + comment + external_id.
- Media: source_id + content_external_id + normalized URL hoặc content hash.

Raw payload mặc định không lưu. Nếu cần debug parser, chỉ lưu allowlist đã redact trong artifact mã hóa, có TTL và không đưa vào log/Mongo nghiệp vụ.

### 7.6 API catalog, health và cấu hình

Endpoint catalog trả schema versioned:

~~~text
SourceDescriptor
  manifest
  provider_selection
  operations[]
    capability
    availability
    reason_code
    safe_message
    action
    checked_at
  health_summary
~~~

Health có hai mức:

- **Local readiness:** dependency, executable, config reference và profile có tồn tại.
- **Remote/deep probe:** credential/session/quota/endpoint có dùng được; chỉ chạy theo cache/TTL để không tốn quota mỗi lần mở UI.

Không dùng isinstance(UnconfiguredConnector) để suy default source. Connector vẫn được đăng ký dù thiếu credential; auth/config provider quyết định availability. Các secret cần hỗ trợ:

- YouTube API key.
- Reddit client ID/secret và user agent.
- X bearer/OAuth credentials.
- TikTok client credentials/OAuth state.
- Meta app credentials, encrypted long-lived token references và webhook secret.
- Browser profile reference cho bảy adapter login-gated.

API không trả tên biến secret có giá trị, token suffix hoặc profile filesystem path. Config CTA chỉ nêu field cần thiết và đường thao tác an toàn.

### 7.7 UI và topic/run UX

- Card giữ đúng 17 nguồn, thứ tự từ manifest.
- Một source có thể có nhiều nút: Quét từ khóa, Theo dõi kênh, Xem embed, Kết nối OAuth, Đăng nhập trình duyệt hoặc Xin quyền.
- Nút được bật theo operation availability, không chỉ source state.
- Auth instruction đến từ provider: API key, OAuth, QR/browser hoặc research approval; không hardcode mọi login thành “Cốc Cốc 20–30 giây”.
- Topic form có source picker thật và channel list riêng; không luôn gửi source_ids rỗng.
- Global sources và channels trong cùng topic tạo cả hai nhóm run.
- X embed trở thành renderer capability, không là nhánh literal rải trong generic component.
- Metric label lấy từ descriptor: view, like, reaction, score, comment, share, favorite, repost, danmaku.
- Partial/best-effort/permission-required hiển thị rõ coverage disclaimer.
- CTA không gửi người dùng đến scan khi backend operation là no-op.
- Backend schema và TypeScript type được generate hoặc có parity test bắt buộc.

### 7.8 Scheduling policy

Background-safe được khai theo operation/provider:

- API key, app OAuth, public feed/API và profile browser còn valid có thể background-safe nếu provider cho phép.
- Interactive QR/challenge/OAuth consent không background-safe.
- Webhook provider dùng event trigger nhưng vẫn có reconciliation schedule.
- Quota reset/cost có thể hoãn run thay vì fail.
- Scheduled run gặp auth_required/permission_required được đánh skipped có lý do, không mở UI/browser và không retry vòng lặp.
- Một account/profile/browser lane chạy tuần tự; API lanes dùng per-provider concurrency.

## 8. Worker protocol và cô lập tiến trình

Browser crawler chạy ở subprocess riêng để lỗi Playwright, memory leak hoặc browser crash không làm chết FastAPI.

### 8.1 Luồng IPC

- Parent khởi chạy worker không kèm cookie/token trong argv.
- Parent gửi request versioned qua stdin sau khi process sẵn sàng.
- Stdout chỉ chứa NDJSON event hợp lệ.
- Stderr chỉ chứa diagnostic đã redact và có tail giới hạn.
- Parent có thể gửi cancel, auth_continue và shutdown qua control channel/stdin.
- Worker phát heartbeat; supervisor kill cây process nếu quá deadline.

Event tối thiểu:

- ready
- run_started
- browser_opening
- auth_required
- authenticated
- challenge_required
- page_scanned
- item
- comment
- media
- checkpoint
- rate_limited
- warning
- error
- cancelled
- complete

Mỗi event có protocol_version, sequence, run_id, source_run_id, source_id, provider, operation, timestamp và phase. Event item phải chứa canonical record đã validate; log text không được giả làm record.

### 8.2 Quy tắc hoàn tất

- Complete chỉ phát sau khi mọi record trước đó đã flush và checkpoint có thể commit.
- Error có error_code, retryable, safe_message và diagnostics_ref; không chứa raw response.
- Parent bỏ event trùng theo sequence và run_id.
- Nếu worker chết không có complete, source run là failed/cancelled; checkpoint cũ vẫn còn nguyên.
- Bounded queue tạo backpressure, không giữ vô hạn record hoặc WebSocket event trong RAM.

### 8.3 Cancellation trên Windows

- Worker và browser được đưa vào process group/Job Object do Content Bot sở hữu.
- Hủy mềm trước: gửi cancel, đóng page/context/browser, flush sự kiện.
- Quá grace period mới terminate worker.
- Quá hard deadline mới kill đúng process tree đã ghi nhận.
- Không kill browser ngoài ownership scope và không attach profile cá nhân theo mặc định.

## 9. Browser, profile và auth state machine

### 9.1 Browser manager

- Dùng executable Cốc Cốc được cấu hình hoặc auto-discovery có kiểm tra.
- CDP chỉ bind loopback với cổng ngẫu nhiên nếu thật sự cần.
- Không dùng remote-debugging-address 0.0.0.0.
- Không bật no-sandbox theo mặc định.
- Không tự attach context/tab đầu tiên của browser cá nhân.
- Mỗi source/account có profile riêng và một file lock.
- Một profile chỉ có tối đa một browser owner.
- Profile v2 nằm ở namespace mới để không tranh lock với bridge cũ.

### 9.2 State machine

~~~text
closed
  -> opening
  -> checking_session
  -> ready
       hoặc auth_required -> waiting_for_user -> checking_session
       hoặc challenge_required -> waiting_for_user -> checking_session
  -> running
  -> closing
  -> closed
~~~

Auth timeout trả lỗi AUTH_TIMEOUT có thể tiếp tục ở run mới. Người dùng tự thao tác QR, phone, slider và CAPTCHA trong browser hiển thị. Engine chỉ quan sát trạng thái, không tải/proxy QR về server và không phát sinh trajectory hoặc solver.

Scheduled run:

- Chỉ chạy nếu profile hiện có được probe là ready mà không cần tương tác.
- Nếu cần login/challenge, trả needs_auth và không mở browser tương tác trong background.
- Không biến lỗi auth thành retry vô hạn.

### 9.3 Cookie và secret

- Cookie jar scope theo domain và chỉ tồn tại trong RAM worker.
- Không truyền cookie qua CLI, URL, Mongo, event hoặc log.
- OAuth token dùng secret store/config hiện hữu, chỉ truyền reference vào worker nếu có thể.
- Redactor xử lý Authorization, Cookie, Set-Cookie, QR data, access_token, refresh_token, signature và platform token đã biết.
- File profile có quyền tối thiểu phù hợp Windows user hiện tại.

## 10. Transport, pagination, rate limiting và media

### 10.1 Transport

Core cung cấp bốn primitive:

- OfficialApiTransport cho X/TikTok và API công khai khác.
- BrowserDomTransport cho dữ liệu render công khai trong page.
- BrowserFetchTransport cho request same-origin trong phiên được phép.
- HttpTransport cho endpoint công khai/chính thức với cookie scope hợp lệ.

Signing là module provider tùy chọn có provenance và license review riêng. Core không biết thuật toán ký. Nếu chưa có cách triển khai độc lập hợp lệ, operation là planned/disabled hoặc provider coverage là unsupported; permission_required chỉ dùng khi implementation đã có nhưng quyền truy cập còn thiếu. Không lấy mã từ MediaCrawler để lấp chỗ trống.

### 10.2 Paginator và budget

Mọi paginator dùng chung các guard:

- Exact item limit, không ép lên page size.
- Request budget.
- Deadline.
- Max empty pages.
- Cursor/page no-progress detection.
- Seen-ID loop detection.
- Max consecutive parser failures.
- Checkpoint chỉ tiến sau khi record đã upsert.
- Root, child và total comment budget tách biệt.
- Creator feed luôn có global cap.

Adapter có thể request một page 20 item nhưng chỉ emit phần còn lại của budget. Dữ liệu thừa không được tính là đã xử lý trong checkpoint nếu điều đó làm bỏ mất item ở run sau.

### 10.3 Retry và rate policy

Rate limiter dùng source_id + provider + account key:

- Token bucket/concurrency nhỏ theo manifest.
- Tôn trọng Retry-After và quota header chính thức.
- Exponential backoff có jitter, nhưng tổng retry nằm trong retry budget.
- Circuit breaker khi liên tục gặp auth, risk-control hoặc parser drift.
- Retry network timeout, 408, 425, 429 và một số 5xx.
- Không retry mù 401/403/challenge/not found.
- Không xoay proxy mặc định để né rate limit.
- Static proxy chỉ là cấu hình mạng; browser và HTTP phải dùng cùng identity.

### 10.4 Media

Media metadata tách khỏi download. Download chỉ khi request bật rõ ràng:

- Kiểm tra allowlist scheme/domain và redirect.
- HEAD/stream với giới hạn byte, timeout và MIME.
- Ghi file tạm rồi atomic rename.
- Dedupe bằng content hash.
- Không làm mất ContentRecord nếu media lỗi.
- Không bỏ watermark hoặc vượt cơ chế bảo vệ.
- Có quota theo run và cleanup file tạm khi cancel.

## 11. Kế hoạch theo từng nguồn

Thứ tự mục dưới đây nhóm theo chiến lược triển khai, không thay đổi thứ tự canonical của registry ở mục 2.2.

### 11.1 YouTube

Giữ YouTubeConnector và channel scanner hiện hữu, bọc vào SourceManifest.

Provider chuẩn là [YouTube Data API v3](https://developers.google.com/youtube/v3/getting-started); search và page token bám theo [search.list](https://developers.google.com/youtube/v3/docs/search/list). Quota/cost phải lấy từ tài liệu và response hiện hành, không giữ giả định quota cũ trong code.

Việc cần làm:

- [x] Giữ YouTube Data API v3 là provider chính thức duy nhất.
- [x] Tách health local_configured khỏi deep probe key_invalid/quota_exhausted.
- [x] Đưa region, relevance language và cửa sổ publishedAfter 90 ngày vào fingerprint/config descriptor.
- [x] Giữ toán tử OR bằng ký tự | theo search.list, URL-encode đúng và có test query nhiều alias.
- [x] Lưu pageToken theo query fingerprint; reset cursor khi query/filter/time window đổi.
- [x] Channel uploads dừng sớm theo overlap watermark và luôn kiểm tra frontier trước backlog.
- [x] Chuẩn hóa tag thành hashtag có dấu # thay vì chỉ giữ tag vốn đã có #.
- [x] Raw payload dùng allowlist, không giữ response dư thừa.
- [x] Theo dõi quota bucket riêng của search.list và general read bằng per-run request budget.
- [x] Root comment qua commentThreads.list; child reply luôn dùng comments.list khi totalReplyCount yêu cầu, với root/child/total/request budget độc lập.

Gate: invalid key/quota có lỗi typed; keyword và channel resume đúng; max chính xác; cùng video qua search/channel có cùng canonical ID; quota budget được test.

### 11.2 Game news

Giữ FeedConnector nhưng định nghĩa rõ đây là RSS/Atom, không phải crawler arbitrary website.

Google News RSS mặc định là một feed source đang dùng, không được coi là API có SLA hay nguồn canonical của publisher.

Việc cần làm:

- [x] Allowlist hostname/template thật sự và validate HTTPS/redirect.
- [x] Saved URL chỉ được scan khi content type/XML xác nhận RSS/Atom.
- [x] Hỗ trợ JSON/list config giữ được comma trong URL, đồng thời đọc tương thích legacy comma format.
- [x] Một feed XML lỗi tạo warning cho feed đó, không làm mất kết quả từ mọi feed.
- [x] Parse RFC 822/3339/ISO date bằng thư viện chuẩn, không suy loại date từ dấu phẩy.
- [x] Sanitize summary/content HTML.
- Một phần có chủ đích: giữ publisher name/homepage provenance và chỉ canonicalize article URL do feed cung cấp. Không reverse-engineer Google News aggregator payload hoặc tự fetch arbitrary publisher redirect.
- [x] Hỗ trợ ETag/Last-Modified và conditional request, chỉ checkpoint khi document được consume đầy đủ.
- [x] Chuẩn hóa enclosure/thumbnail thành media metadata nếu có.

Gate: feed hợp lệ/invalid/timeout/redirect/duplicate đều có fixture; generic HTML không bị coi là feed; partial feed failure hiển thị succeeded_with_warnings.

### 11.3 Steam reviews

Giữ public Store search và appreviews provider.

Việc cần làm:

- [x] Tách app discovery khỏi review crawl; saved `/app/<id>` channel pin game và ambiguity fail-closed tránh chọn nhầm DLC/demo/game gần tên.
- [x] Persist cursor theo app + filter + language + purchase type và luôn đọc newest frontier trước backlog.
- [x] Saved app scan phân trang quá 100 với exact remaining-item cap.
- [x] Dùng external ID app_id:recommendation_id. Không tạo per-review deep link vì nó đòi giữ reviewer Steam ID trái privacy policy; giữ app review-list URL.
- [x] Định nghĩa votes_up/helpfulness riêng trong descriptor/raw allowlist, với legacy mapper like_count tường minh.
- [x] Áp privacy/redaction giống nhau cho keyword và channel; cả hai dùng chung normalizer và không giữ author object.
- [x] App lookup và review page có request budget riêng.

Gate: app ambiguity, pagination >100, repeated cursor, privacy snapshot, exact max và second-run dedupe pass.

### 11.4 Bluesky

Giữ public AT Protocol AppView provider.

Pin lexicon/reference chính thức cho [searchPosts](https://docs.bsky.app/docs/api/app-bsky-feed-search-posts) và [getAuthorFeed](https://docs.bsky.app/docs/api/app-bsky-feed-get-author-feed) trong provenance.

Việc cần làm:

- [x] Dùng AT URI/rkey làm stable external identity; CID là version/content hash vì có thể đổi khi edit.
- [x] Cursor lưu riêng theo normalized search term và query fingerprint.
- [x] Không nuốt 403/rate-limit/transport thành EOF thành công; emit typed failure.
- [x] Author feed hỗ trợ pagination quá một page và checkpoint.
- [x] Chuẩn hóa reply/repost/quote relations.
- [x] Global và channel cùng raw allowlist; bỏ DID/profile blob.
- [x] Media embed/record facets được normalize có giới hạn.
- [x] User-Agent minh bạch, không giả browser nếu API không cần.
- [x] Public `getPostThread` reply tree có resolve-handle chính thức, stable hashed hierarchy, HMAC author và root/child/total/depth/physical-request budgets; provider ordering được khai báo đúng thay vì giả lập sort.

Gate: edited record giữ identity, CID update thành version mới; multi-term resume; partial outage; author pagination và dedupe pass.

### 11.5 Mastodon

Giữ public API nhưng sửa cách mô tả capability.

[Mastodon hashtag timeline](https://docs.joinmastodon.org/methods/timelines/#tag) là API theo từng instance; public preview có thể bị instance tắt và khi đó cần app token. Không suy một instance đại diện toàn fediverse.

Việc cần làm:

- [x] Operation hiện tại là hashtag discovery, không phải general full-text search.
- [x] Instance list cấu hình được; saved account chỉ chạy sau instance validation.
- [x] Health sâu trả aggregate per-instance status; tất cả instance chết không được báo ready.
- [x] Dùng canonical origin URL/URI làm identity, không gắn fetching instance vào ID.
- [x] Parse Link/max_id pagination và checkpoint theo instance + hashtag/account.
- [x] Unwrap/reconcile reblog, reply và original status.
- [x] Dùng HTMLParser/entity decoder thay regex/list thủ công.
- [x] Một instance lỗi tạo partial warning, không silent success rỗng.
- [x] Loại bỏ catch-all /@ domain detection.
- [x] Stored-status public context dùng exact configured instance, xác minh root qua status endpoint, rebuild parent-first hierarchy và áp root/child/total/depth/physical-request budgets với hashed identity/HMAC author.

Gate: federated duplicate, origin identity, instance outage, Link pagination, hashtag normalization và HTML sanitization pass.

### 11.6 Reddit

Giữ official OAuth provider.

Provider phải tuân theo [Reddit API reference](https://www.reddit.com/dev/api/) và [Data API Terms hiện hành](https://redditinc.com/policies/data-api-terms). Mục đích thương mại, nghiên cứu vượt rate limit hoặc use case ngoài quyền được cấp có thể cần thỏa thuận riêng; health/config không được ngụ ý credential đồng nghĩa mọi use case đều được phép.

Việc cần làm:

- [x] Cache application token đến gần expiry; token request cũng có retry/backoff/redaction.
- [x] Lưu after theo query fingerprint và từng term; channel subreddit/user có pagination.
- [x] Tách Reddit score khỏi like_count trong metric descriptor, giữ mapper legacy tường minh trong thời gian migration.
- [x] Chuẩn hóa author retention/pseudonym policy.
- [x] Global và channel dùng cùng raw allowlist, không để channel giữ full post.
- [x] Comment tree dùng depth/root/child/request budget và tuần tự `/api/morechildren`; normalized hierarchy được persist/cascade.
- Xử lý quarantined/private/banned subreddit và revoked/invalid credential bằng typed error.

Gate: missing/invalid credentials, token refresh, 429, multi-term checkpoint, channel >100, privacy parity và score semantics pass.

### 11.7 Bilibili

Baseline nghiên cứu:

- Có keyword search thường và theo khoảng ngày, detail BV/URL, creator videos, dynamics, root/child comments và download video.
- Browser dùng QR/cookie; phone là stub.
- Client tham chiếu WBI signing.

Thiết kế mới:

- Bắt đầu bằng content search/detail/creator video và comments.
- Tách dynamics thành capability content_kind=dynamics, không dùng cờ creator có nghĩa đảo.
- Ưu tiên nguồn public/chính thức; browser DOM/fetch chỉ trong phiên được phép.
- Không chép WBI mixin key/table/salt hoặc wrapper endpoint.
- Dùng canonical BV/AV relation nhưng external_id chọn một dạng ổn định.
- Media download là capability tùy chọn, có byte quota.

Lỗi phải có regression test:

- Max nhỏ hơn 20 vẫn trả đúng max.
- Bật child comment không làm mất root hoặc vô hiệu total cap.
- Creator pagination có exact cap.
- Mongo hỗ trợ cùng entity mà manifest tuyên bố; không tuyên bố dynamics nếu sink chưa có.
- Không thu fans/followings.

Gate: search, detail và creator đều emit cùng schema cho cùng video; run thứ hai không duplicate; auth/risk-control typed; hủy không để browser.

### 11.8 X

Provider 1, mặc định: X API.

Lỗi hiện tại từ api.x.com với HTTP 402 không chứng minh endpoint chính thức bị hỏng; nó cho biết project/tài khoản chưa có billing/credits/access phù hợp với request. Engine không thể sửa điều này bằng thuật toán crawler. Provider phải preflight configuration/tier khi có thể, map 402 thành PAYMENT_OR_ACCESS_REQUIRED và giữ operation disabled cho đến khi quyền hợp lệ.

- Search recent/all theo quyền.
- Post lookup/detail.
- User timeline/creator.
- Conversation/replies theo field và endpoint được cấp.
- Media metadata và public metrics theo response.
- OAuth/bearer token không qua argv.

Provider 2: oEmbed, chỉ để enrich/embed URL cụ thể trong phạm vi endpoint.

Provider browser không thuộc MVP và disabled nếu chưa có chấp thuận rõ ràng. Không dùng GraphQL nội bộ, guest token extraction hoặc giả lập client web.

Chi tiết dữ liệu:

- external_id là Post ID.
- quote/reply/repost liên kết bằng referenced post.
- canonical URL tạo từ author username khi có, nhưng identity vẫn dựa Post ID.
- Conversation crawl có depth/request budget và seen-ID guard.
- Deleted/protected/withheld là trạng thái typed, không biến thành parser error.

Gate: capability phản ánh đúng tier; 402/403/429 có thông báo/CTA rõ và không bị retry lãng phí; embed vẫn dùng được khi search chưa được cấp; X được bỏ khỏi filter hiện tại chỉ sau khi run planner, integration và policy tests đạt.

### 11.9 Weibo

- Tách keyword search, detail và creator rõ ràng.
- Mobile/PC cookie jar domain-aware.
- Long text là enrichment có budget và cờ text_truncated.
- Root comments và child comments khai báo partial nếu chỉ có preview.
- Creator feed có global max, delay và repeated since_id guard.
- Media policy phải đồng nhất giữa search/detail/creator.
- HTML sanitizer xử lý entity và giá trị null.
- Không hardcode giả định 10 item/page để quyết định hoàn tất.

Gate: card trực tiếp, nested group, quảng cáo, long text và parser drift có fixture tự thu; cùng post qua ba mode cho cùng canonical ID.

### 11.10 Tieba

- Tách mode keyword search và forum listing; không tự động chạy cả hai.
- Detail và creator là mode riêng.
- Search type/sort phải thực sự được provider dùng hoặc manifest đánh unsupported.
- Browser same-origin transport là primitive; không chép MD5 secret, tbs/sign flow, header impersonation hay anti-detection script.
- Root/child comments dùng budget chung và công thức page không off-by-one.
- Không hardcode fingerprint khác với browser thật.
- Không hỗ trợ media ở phase đầu.

Gate: boundary 1/29/30/31 item, comment count 9/10/11/20, repeated page, deleted thread và auth expiry đều được test; không có secret nền tảng trong repo.

### 11.11 Xiaohongshu/RedNote

- Manifest phân biệt domain Xiaohongshu và RedNote quốc tế.
- Search có sort general/popularity/time, detail giữ context token chỉ trong session, creator feed có exact cap.
- Detail có thể dùng DOM/embedded state fallback do dự án tự nghiên cứu.
- Không chép X-S/X-T wrapper, xhshow wrapper, crypto helper hoặc fixture.
- CAPTCHA/risk page chuyển waiting_for_user/challenge_required.
- Root/child comment budget rõ ràng.
- image_list/tag_list giữ kiểu list, không double encode.
- Không lưu xsec token vào canonical record.
- Canonical URL dùng đúng domain.

Gate: page cuối có has_more=false vẫn được xử lý; max=1/19/20/21 chính xác; repeated cursor dừng; challenge không bị retry mù.

### 11.12 Douyin

- Search, detail ID/URL/short URL và creator là ba flow độc lập.
- Không chép libs/douyin.js, a_bogus implementation, fingerprint cố định hoặc slider solver.
- Chọn official/public/DOM provider có provenance; nếu một action bắt buộc cơ chế chưa thể triển khai hợp lệ thì khai unsupported.
- Browser và transport giữ session identity nhất quán.
- Creator không bao giờ crawl vô hạn.
- Empty page cùng has_more phải có no-progress guard.
- Media metadata ưu tiên; download tùy chọn.

Gate: short redirect allowlist, exact max, creator cap, child budget, cancel khi chờ login và manual slider flow đều có test.

### 11.13 Kuaishou

- Search, detail và creator.
- Không chép GraphQL documents, Object.prototype hook, encode capture hoặc endpoint payload từ MediaCrawler.
- Browser và HTTP cùng proxy/identity nếu static proxy được dùng.
- Không dùng blocking sleep trong async.
- Comment failure không được tự cancel toàn bộ task rồi bỏ dữ liệu im lặng.
- Chuẩn hóa view_count, không phát sinh field sai chính tả viewd_count.
- Media metadata ở phase đầu; download chỉ khi đã có provider ổn định.

Gate: rate-limit backoff kiểm thử bằng fake clock; task lỗi cô lập; root/child exact cap; creator repeated cursor và global cap.

### 11.14 Zhihu

- Search theo time/sort/type.
- Detail phân loại answer, article và video.
- Creator cho phép chọn answers, articles, videos hoặc all, nhưng vẫn có global budget.
- Shared semaphore thực sự dùng chung giữa các detail task.
- Comment có root/child/total cap.
- Không chép libs/zhihu.js hoặc x-zse/x-zst implementation.
- Auth state phân biệt cookie đăng nhập và cookie/signing requirement; báo đúng reauth/risk.
- Không log raw response.
- Storage luôn dùng content_id chuẩn, không trộn note_id.

Gate: regression Mongo content_id, embedded state thiếu/private/deleted, cursor lặp và 403 đều pass.

### 11.15 TikTok

Provider Display API:

- OAuth với Login Kit.
- Profile và public videos của user đã ủy quyền.
- Không tuyên bố global keyword search/comments nếu API không cấp.

Provider Research API:

- Chỉ bật khi dự án/tài khoản được duyệt.
- Query public videos/accounts/comments theo scope và quota thực tế.
- Lưu provider, query window và observed_at để giải thích độ trễ dữ liệu.

Browser provider:

- Không thuộc MVP.
- Chỉ nghiên cứu sau policy review, đăng nhập thủ công và không bypass challenge.

TikTok model riêng, không dùng payload/schema Douyin:

- Canonical video ID và creator reference.
- description, create time, hashtags, music metadata nếu response cho phép.
- like/comment/share/view metrics tùy field.
- duet/stitch/reply relation khi API cung cấp.
- Media metadata; download disabled mặc định.

Gate: OAuth refresh/redaction, scope thiếu, quota, pagination, partial capability và revoked authorization đều có contract test.

### 11.16 Facebook

Không xây một “Facebook global crawler” giả định. Tách ba provider:

**meta_pages_owned**

- Facebook Login → Page list → Page access token.
- Backfill Page posts/feed bằng cursor.
- pages_show_list + pages_read_engagement là base capability.
- pages_read_user_content chỉ bật module visitor UGC khi đã được review.
- Webhook là trigger; periodic reconciliation xử lý event trễ/mất và update/delete.
- Token được mã hóa, có expiry/revoke state và không vào worker argv/log.

**meta_pages_public**

- Chỉ bật sau Page Public Content Access approval.
- Page search/allowlist, public Page posts/comments/engagement đúng feature được cấp.
- Tách feature flag và test bằng Page ngoài app roles.
- Personal profile, private content và Group không được suy rộng từ PPCA.

**meta_research_library**

- Project/provider cô lập dành cho Meta Content Library/API nếu đủ tư cách.
- Credential, storage boundary, retention và allowed purpose tách khỏi production provider.
- Không dùng như fallback khi Graph API thiếu quyền.

Canonical record giữ post ID, Page reference, message/story, created/updated time, permalink, attachments và reaction/comment/share metrics theo field được phép. access_basis là owned, ppca hoặc research; coverage_disclaimer luôn được lưu cùng provenance.

Gate:

- Owned Page backfill, pagination, webhook create/update/delete, reconciliation và token revoke pass.
- Thiếu pages_read_user_content chặn đúng UGC operation.
- PPCA trước phê duyệt trả permission_required; sau phê duyệt đọc Page ngoài app roles theo test asset.
- Không operation nào truy cập profile/private/group ngoài scope.
- App Review package có screencast, test asset/credential, privacy policy và user-data deletion flow.

### 11.17 Instagram

Tách bốn provider:

**instagram_owned**

- Professional Business/Creator account được ủy quyền.
- Profile/media là base module; comments, mentions và insights là module permission riêng.
- Webhook + periodic reconciliation.
- Personal/consumer account trả unsupported rõ ràng.

**instagram_business_discovery**

- Theo dõi allowlist username/ID Professional công khai.
- Profile và media fields endpoint cung cấp; không hứa toàn bộ comments hoặc personal account.
- Cursor/watermark/dedupe theo media ID.

**instagram_hashtag**

- Resolve hashtag ID rồi recent_media/top_media.
- Quota hashtag unique trong rolling window được quản lý tập trung theo IG user; giới hạn hiện hành phải được đọc từ provider policy/config và đối chiếu docs trước release.
- Cache hashtag ID, không resolve lại vô ích.
- Gắn best_effort=true; không tuyên bố full history/firehose/completeness.

**meta_research_library**

- Chỉ cho dự án nghiên cứu được duyệt và tách khỏi provider sản phẩm.

Permission được xin tối thiểu theo operation, ví dụ instagram_basic, pages_show_list, pages_read_engagement; comments/insights chỉ thêm khi module tương ứng cần và review cho phép. Không bundle mọi scope.

Canonical record giữ IG media ID, permalink, caption, media/product type, timestamp, owner ref theo policy, children/carousel, hashtag và metric theo permission. access_basis là owned, business_discovery, hashtag hoặc research.

Gate:

- Business và Creator account test pass; personal account bị từ chối rõ.
- Business Discovery phân biệt Professional, personal và nonexistent.
- Hashtag ID cache, top/recent cursor, rolling quota, reset window và best-effort UI pass.
- Comments/insights bị chặn đúng khi thiếu permission.
- OAuth revoke/expiry, webhook và data-deletion flow pass.

## 12. Những lỗi quan sát được phải biến thành regression test

| Nhóm | Vấn đề quan sát | Yêu cầu mới |
|---|---|---|
| Core hiện tại | Global source run không persist provider cursor | Checkpoint per query/term/target được commit sau upsert |
| Core hiện tại | Keyword và channel dùng hai registry | Một SourceManifest/run planner duy nhất |
| Core hiện tại | X bị filter bằng literal; scheduler chỉ nhìn requires_login | Operation capability/background_safe quyết định |
| Core hiện tại | dy/ks/bili/wb lệch source ID canonical | Alias migration + contract canonical ID |
| Catalog/UI | No-op source vẫn khai capability mong muốn | Không card ready/action enabled nếu không có handler |
| Target detection | Thiếu Tieba/mobile Bilibili, catch-all Mastodon | Table-driven domain boundary tests |
| YouTube | Health chỉ kiểm key tồn tại; cursor không persist | Deep probe tùy chọn + quota/auth state + resume |
| Game news | Generic web có thể vào XML parser; parser date yếu | Feed validation + robust date/sanitize |
| Steam | Channel giữ author raw và cap một page | Privacy parity + pagination |
| Bluesky | Transport/403 bị nuốt thành EOF; CID làm identity | Typed partial/failure + AT URI identity |
| Mastodon | Hashtag bị gọi global search; lỗi instance bị nuốt | Capability đúng + per-instance warning |
| Reddit | Token không cache/retry; score gọi là like | Token lifecycle + metric semantics |
| Giới hạn | Nhiều adapter ép max lên page size | Luôn emit đúng item budget |
| XHS | Break trước khi xử lý page cuối có has_more=false | Xử lý records trước khi xét dừng |
| XHS | List/tag double encoding và hardcode domain | Typed list, canonical domain đúng |
| Douyin | Creator không global max; cursor rỗng có thể spin | Cap + no-progress guard |
| Douyin | Slider automation | Chỉ manual challenge |
| Kuaishou | time.sleep trong async, cancel comment tasks | Async backoff và lỗi cô lập |
| Kuaishou | Browser/proxy identity lệch | Một NetworkIdentity cho cả session |
| Bilibili | Child comments làm semantics max sai | Root/child/total budget rõ |
| Bilibili | Mongo không lưu dynamics dù flow có | Manifest và sink parity |
| Weibo | Child comments chỉ là preview nhúng | partial=true hoặc implementation đầy đủ |
| Weibo | Creator interval=0 và không cap | Rate policy + global cap |
| Tieba | Forum max=30 có thể lấy 60 | Boundary test và exact emit |
| Tieba | SearchNoteType bị bỏ qua | Provider dùng thật hoặc unsupported |
| Zhihu | Mỗi task tạo semaphore riêng | Shared run semaphore |
| Zhihu | Comment không áp max | Comment budgets |
| Zhihu | Mongo đọc note_id thay vì content_id | Schema contract test mọi sink |
| Core | Cookie trên argv/log | IPC + redaction test |
| Core | Config mutable toàn cục | Immutable RunContext |
| Core | Process child có thể orphan | Ownership + Job Object test |
| Meta | Facebook/Instagram placeholder không có config/handler | Provider thật hoặc permission_required, không no-op |
| Meta | Dễ ngụ ý global search từ visibility công khai | access_basis + coverage disclaimer bắt buộc |

## 13. Lưu trữ, checkpoint, dedupe và privacy

### 13.1 Tận dụng Mongo hiện hữu

Crawler không gọi Mongo trực tiếp. Worker emit canonical event; parent map ContentRecord sang RawContentItem và để RunManager/MongoStore thực hiện upsert, metric snapshot và keyword matching. Các connector API/public chạy in-process cũng đi qua cùng normalizer và repository boundary.

Comment/media cần collection và repository riêng chỉ khi phase tương ứng được bật. Không tạo database song song theo từng source.

Trước khi sửa video library hoặc dữ liệu cũ:

- Chọn youtube, web, steam, bluesky, mastodon, reddit, x, xhs, douyin, kuaishou, bilibili, weibo, tieba, zhihu, tiktok, facebook, instagram làm canonical source ID.
- dy, ks, bili và wb là read aliases có thời hạn.
- Viết dry-run migration đếm collision trước khi đổi content_items, source_runs, channels, filters và metric snapshots.
- Khi hai record alias/canonical va chạm, merge metric/history theo quy tắc xác định; không overwrite mù.
- Sửa video-library query/branch dùng source ID canonical và test dữ liệu legacy.

### 13.2 Commit checkpoint

Checkpoint gồm:

- schema_version.
- source_id, provider và operation.
- query_fingerprint gồm normalized target, terms, filters, provider version và time window.
- provider cursor theo từng term/target/instance nếu provider cho phép lưu.
- latest_published_at.
- bounded recent_ids.
- last_successful_page và observed_at.
- checkpoint codec/version.

Quy tắc:

1. Nhận page.
2. Normalize và validate.
3. Upsert tất cả record được chấp nhận.
4. Commit metric snapshot.
5. Sau cùng mới commit checkpoint.

Nếu process chết giữa bước 3 và 5, run sau có thể đọc lại nhưng upsert idempotent sẽ không duplicate. Không ghi checkpoint trước dữ liệu.

Checkpoint có hai lớp độc lập:

- **Provider cursor:** pageToken/after/cursor/max_id/offset, scope theo query fingerprint.
- **Generic watermark:** recent_ids/latest_published_at dùng để dừng sớm và chống duplicate.

Global keyword run và saved-channel run đều phải persist checkpoint. Một scalar cursor không được dùng cho nhiều alias; mỗi normalized term có cursor riêng. Khi query fingerprint hoặc checkpoint codec đổi, provider cursor bị invalidate có chủ đích nhưng generic watermark vẫn có thể dùng.

Run partial không tự động commit cursor qua page lỗi. Nếu các page trước đã upsert, checkpoint chỉ tiến đến page cuối đã xác nhận hoàn chỉnh và event phải ghi succeeded_with_warnings.

### 13.3 Privacy

- Dùng keyed HMAC cho pseudonymous creator ID nếu cần liên kết nội bộ; không dùng SHA-256 không salt bị đoán lại.
- Secret HMAC nằm trong secret configuration, có version để rotate.
- Display name có thể mask hoặc bỏ theo product requirement.
- Không lưu raw user ID nếu không cần.
- Không lưu avatar, bio, giới tính, địa chỉ/IP location, follower graph mặc định.
- Log chỉ dùng stable run ID và hashed target.
- Data retention và delete flow bao gồm content, comments, media, debug artifacts và browser profiles, nhưng xóa profile phải có xác nhận người dùng.
- OAuth/Meta user-data deletion callback phải map được account/provider reference đến toàn bộ dữ liệu cần xóa.
- access_basis và allowed_purpose đi cùng provenance cho X/TikTok/Meta research providers.

## 14. Observability và taxonomy lỗi

### 14.1 Lỗi chuẩn

| Code | Retry mặc định | Ý nghĩa |
|---|---:|---|
| CONFIG_REQUIRED | Không | Thiếu key/client/app/provider config |
| NOT_IMPLEMENTED | Không | Operation còn planned; không có network fallback |
| AUTH_REQUIRED | Không | Cần đăng nhập thủ công/OAuth |
| AUTH_EXPIRED | Một lần sau reauth | Phiên hết hạn |
| CHALLENGE_REQUIRED | Không | Người dùng phải xử lý challenge |
| PERMISSION_REQUIRED | Không | App/tài khoản thiếu scope/tier |
| PAYMENT_OR_ACCESS_REQUIRED | Không | API chính thức yêu cầu billing/credits/access khác |
| POLICY_DISABLED | Không | Operation bị tắt theo policy dự án/nền tảng |
| QUOTA_EXHAUSTED | Theo reset | Quota chính thức đã hết |
| RATE_LIMITED | Theo retry_after | Quota hoặc giới hạn tạm thời |
| NOT_FOUND | Không | Nội dung bị xóa/không tồn tại |
| PROTECTED | Không | Nội dung riêng tư/withheld |
| PARSER_CHANGED | Không tự động dài hạn | Contract/DOM thay đổi |
| TRANSPORT_ERROR | Có budget | Network/timeout |
| PLATFORM_ERROR | Tùy mã | Business error được map |
| STORAGE_ERROR | Có kiểm soát | Không commit checkpoint |
| CHECKPOINT_INCOMPATIBLE | Reset có kiểm soát | Query/codec/provider version đã đổi |
| BUDGET_EXHAUSTED | Không | Dừng thành công có giới hạn |
| CANCELLED | Không | Người dùng/hệ thống hủy |
| INTERNAL_ERROR | Không mù | Lỗi implementation |

### 14.2 Metric và log

Trạng thái source run tối thiểu là queued, running, waiting_for_user, succeeded, succeeded_with_warnings, failed, cancelled và skipped. “Không có record” chỉ là thành công khi provider đã trả một trang hợp lệ và không có warning.

Metric tối thiểu:

- Runs theo source/provider/operation/state/access_basis.
- Auth wait duration và auth failure.
- Request count/latency/status.
- Items fetched/emitted/upserted/duplicate.
- Pages, empty pages và repeated cursors.
- Retry/rate-limit/circuit-break count.
- Parser success/drop/unknown field.
- Comments root/child/partial.
- Media bytes/success/failure.
- Worker/browser startup và cleanup duration.
- Orphan process detection.
- Checkpoint load/commit/invalidate và resume distance.
- Quota remaining/reset khi API chính thức cung cấp.
- Partial coverage/provider disclaimer count.

Log có run_id, source_run_id, source_id, provider, operation, phase và error_code; không log query response nguyên vẹn.

## 15. Chiến lược kiểm thử

### 15.1 Unit

- URL/ID normalization và domain allowlist.
- Model validation và canonical URL.
- Exact budget allocation.
- Cursor no-progress, empty page và seen-ID.
- Retry classification với fake clock/jitter.
- Cookie scoping và redaction.
- HMAC/pseudonym version.
- Media MIME/size/redirect.
- Query fingerprint và checkpoint codec migration.
- Canonical/legacy source alias resolution.
- Operation capability + runtime availability state transitions.

### 15.2 Adapter contract

Mọi adapter chạy phần suite tương ứng với operation manifest tuyên bố:

- Manifest nhất quán với method thực tế.
- Search/channel/detail/creator độc lập khi supported.
- Max 0, 1, page_size-1, page_size và page_size+1.
- Page cuối vẫn emit dữ liệu.
- Duplicate ID trong một page và giữa page.
- Repeated cursor/page.
- 401/403/404/429/5xx, timeout và malformed response.
- Root/child/total comment budgets.
- Cancellation ở auth, request, normalize, store và media.
- Same item qua nhiều mode cho cùng canonical ID.
- Permission-required operation không gọi network fallback.
- Partial provider phải emit coverage disclaimer.

### 15.3 Fixture

- Fixture do dự án tự thu bằng account thử nghiệm được phép.
- Sanitize cookie, token, username, URL ký, tracking ID và PII.
- Không copy fixture MediaCrawler.
- Có parser_version và capture date.
- Unknown field được bỏ an toàn; missing field quan trọng tạo typed parser signal.

### 15.4 Integration

- Worker stdin/stdout protocol.
- Bounded queue và backpressure.
- Process crash trước/sau checkpoint.
- Mongo upsert lần một/lần hai.
- Metric snapshots.
- Profile lock và concurrent run.
- Windows cancellation/Job Object; không còn child/browser sau deadline.
- API/UI manifest từ một nguồn.
- Scheduled run không mở interactive auth.
- Exact 17 source IDs, order, uniqueness và stable IDs.
- Keyword scanner/channel scanner/target detector/adapter parity.
- Topic chứa đồng thời global sources và channels lập đủ run plan.
- Checkpoint resume nhiều term/instance/target và invalidation khi query đổi.
- Legacy alias migration dy/ks/bili/wb, collision dry-run và video-library regression.
- Mọi UI action enabled đều queue được một backend operation supported.
- Mọi metric descriptor đều render đúng, gồm share/favorite/reaction/Reddit score.
- X embed vẫn hoạt động khi X API search chưa được cấp.
- Facebook/Instagram owned, public/hashtag/business-discovery/research capability không bị gộp.

### 15.5 Security và provenance

- Snapshot test chứng minh cookie/token/QR/signature không có trong log, event, argv và Mongo.
- CDP chỉ loopback.
- Browser không attach profile cá nhân mặc định.
- Dependency/license scan.
- Similarity scan với vendor trước khi xóa.
- Review provenance cho endpoint/field/selector/dependency.
- App/OAuth permission minimization, revoke và deletion callback tests.
- Meta App Review test assets/screencast checklist.

### 15.6 Live canary

- Opt-in thủ công, không chạy trong CI mặc định.
- Tài khoản/test asset được phép, quota nhỏ, một keyword/URL/owned asset kiểm soát.
- Không challenge bypass.
- Tự dừng khi parser drift, rate limit hoặc auth bất thường.
- Ghi metric tổng hợp, không ghi raw response.
- Public/API canary cho sáu connector hiện hữu; browser canary cho bảy adapter thay bridge; official-provider canary cho X/TikTok/Meta theo quyền.

## 16. Lộ trình triển khai

Mỗi phase có deliverable và gate; không chuyển phase chỉ vì code đã viết. Số phase dùng để tổ chức deliverable, không ép mọi việc chạy nối đuôi. Các hồ sơ X/TikTok/Meta, App Review và research access là external lead-time track, phải bắt đầu từ Phase 0 dù code adapter nằm ở phase sau.

Quan hệ phụ thuộc:

| Workstream | Phụ thuộc | Có thể chạy song song |
|---|---|---|
| Unified registry + sáu connector | Phase 0 | External approval tracks |
| Browser worker/runtime | Unified registry/contracts | X API và Meta owned-provider work |
| X official provider | Unified registry + X credentials/tier | Browser runtime/Bilibili |
| Bilibili vertical slice | Browser runtime | X/Meta approval |
| Facebook/Instagram providers | Unified registry + từng approval/test asset | Browser adapters |
| Sáu browser adapter còn lại | Browser runtime + Bilibili lessons | X/TikTok/Meta work |
| TikTok providers | Unified registry + TikTok approval | Browser adapters |
| Hardening/cutover | Minimum-operation gates của đủ 17 nguồn | Không |

Như vậy nhu cầu X không phải chờ hoàn tất Bilibili: X API có thể bắt đầu ngay sau Phase 1 trong khi Phase 2–3 hoàn thiện browser vertical slice.

### Phase 0 — Governance và đặc tả

Độ lớn: M.

Việc làm:

- Chốt tên CBCE và ranh giới clean-room.
- Ghi snapshot/provenance của nghiên cứu.
- Chọn mục đích thương mại/phi thương mại và policy X/TikTok/Meta.
- Chốt danh sách 17 canonical source ID và legacy aliases.
- Viết SourceManifest, operation capability, canonical models, error taxonomy và budgets.
- Lập data-retention/privacy policy.
- Tạo feature flag cbce_enabled và provider override theo source/operation.
- Tạo namespace profile v2, chưa đụng profile cũ.
- Mở hồ sơ X developer, TikTok developer/research và Meta App Review/PPCA/Content Library nếu phù hợp.
- Tạo provenance ledger template và rule fixture tự thu.

Gate:

- Architecture review, security review và license review đạt.
- Không có code từ vendor trong scaffold.
- Đủ 17 manifest draft, không có capability mong muốn gắn vào no-op.
- X, TikTok, Facebook và Instagram có provider/access-basis decision.

### Phase 1 — Unified registry và củng cố sáu connector hiện hữu

Độ lớn: XL.

Việc làm:

- SourceRegistry, SourceManifest, OperationCapability và run planner.
- Compat registration cho YouTube, Game news, Steam, Bluesky, Mastodon và Reddit.
- Hợp nhất keyword scanner, channel scanner, target detection và default-source policy.
- Persist checkpoint global/channel theo query fingerprint và từng term/target.
- Sửa canonical identity, error swallowing, raw privacy parity và health states của sáu connector.
- Thêm source picker/operation CTA trong UI dựa manifest.
- Sửa aliases/video-library mismatch dy/ks/bili/wb bằng migration có dry-run.
- Bỏ type checks/literal source logic nơi registry có thể quyết định.
- Mapper ContentRecord sang RawContentItem.
- Tích hợp progress event với RunManager/EventBus.

Gate:

- Exact 17 registry contract pass.
- Sáu connector cũ không mất chức năng và pass keyword + channel tests.
- Global checkpoint thật sự resume giữa hai batch.
- Topic có cả global source và channel lập đủ run plan.
- Mọi UI action enabled có backend handler thật.
- Legacy alias migration/video-library regression pass.

### Phase 2 — Core runtime trình duyệt và worker protocol

Độ lớn: XL.

Việc làm:

- Immutable RunContext và typed crawler contracts.
- Worker supervisor, NDJSON protocol, heartbeat và bounded queue.
- Browser manager, profile lock, auth state machine và profile v2.
- Transport abstractions, paginator, budgets, rate limiter và redaction.
- Cancellation, Windows Job Object/process-tree ownership.
- Fake browser adapter và failure injection.

Gate:

- Fake adapter pass conformance suite.
- Kill/cancel ở mọi phase không để orphan.
- Cookie/token/QR không xuất hiện trong argv/log/event/Mongo.
- Crash trước/sau checkpoint giữ idempotency.
- Scheduled run không mở interactive auth.

### Phase 3 — Bilibili vertical slice

Độ lớn: L.

Lý do chọn: có đủ search/detail/creator/comments/media để kiểm chứng gần toàn bộ kiến trúc nhưng vẫn có thể tách media thành bước sau.

Việc làm:

- Provider/provenance spike.
- Search, detail, creator videos.
- Root comments, sau đó child comments.
- Media metadata, optional bounded download.
- Shadow comparison với bridge cũ bằng aggregate, không copy fixture.

Gate:

- Ba mode, exact limits, dedupe/checkpoint, login/cancel đạt.
- Không có WBI implementation hoặc constants sao chép.

### Phase 4 — X official provider và xử lý đúng lỗi 402

Độ lớn: M-L tùy tier API.

Việc làm:

- X API client, auth/secret redaction và dynamic capability.
- Post lookup, timeline và search theo quyền.
- Conversation/replies và media metadata nếu tier cho phép.
- Preflight provider/tier khi API cho phép; map 402/403/429 thành thông báo actionable.
- Bỏ filter X trong RunManager khi gate đạt.
- Giữ oEmbed làm enrichment cho URL cụ thể, không thay search.

Gate:

- Không còn raw URL/error 402 lộ ra UI; 402 trở thành PAYMENT_OR_ACCESS_REQUIRED và operation bị disable có CTA.
- Không tự chuyển sang browser scraping khi billing/tier chưa đủ.
- Capability UI đúng với tier.
- Quota, checkpoint, dedupe và protected/deleted states pass.

### Phase 5 — Facebook và Instagram official providers

Độ lớn: XL, phụ thuộc App Review/feature approval.

Việc làm:

- Meta OAuth/token vault/revoke/deletion integration.
- Facebook owned Pages provider, webhook và reconciliation.
- Facebook PPCA provider chỉ sau approval; capability gate trước approval.
- Instagram owned Professional provider.
- Instagram Business Discovery allowlist.
- Instagram Hashtag provider và rolling hashtag budget.
- Meta research provider chỉ khi dự án đủ điều kiện, trong execution/storage boundary riêng.
- UI hiển thị access basis và coverage disclaimer.

Gate:

- Owned Facebook Page và owned Instagram Professional test assets pass.
- Scope thiếu chặn đúng operation, không browser fallback.
- Webhook + reconciliation + token revoke + deletion flow pass.
- PPCA/Business Discovery/Hashtag/Research chỉ ready khi approval tương ứng có thật.
- Facebook/Instagram card không hứa global search.

### Phase 6 — Weibo và Tieba

Độ lớn: XL.

Việc làm:

- Weibo search/detail/creator, long-text enrichment và partial child status.
- Tieba tách keyword/forum/detail/creator.
- BrowserFetchTransport và domain cookie scoping.
- Parser fixtures tự thu.

Gate:

- Exact creator/forum cap.
- No repeated cursor.
- Không có Tieba signing secret, anti-detection code hoặc selector sao chép.

### Phase 7 — XHS, Douyin và Kuaishou

Độ lớn: XXL; chia thành ba merge train độc lập.

Việc làm:

- XHS/RedNote domain-aware adapter.
- Douyin short URL, content/creator/comments.
- Kuaishou content/creator/comments.
- Manual challenge state.
- Provider provenance riêng từng action.

Gate:

- Không có signing/challenge code từ vendor.
- Mọi unsupported action được khai báo trung thực.
- Exact max, rate policy, parser drift và cancellation đạt từng adapter.

### Phase 8 — Zhihu

Độ lớn: L.

Việc làm:

- Search/detail cho ba content kind.
- Creator answers/articles/videos/all.
- Bounded root/child comments.
- Shared semaphore và Mongo schema parity.

Gate:

- Regression content_id đạt.
- Không dùng copied x-zse/x-zst.
- Không log raw payload.

### Phase 9 — TikTok

Độ lớn: L nếu Display API; XL nếu thêm Research API.

Việc làm:

- Login Kit/OAuth.
- Display API provider.
- Research API provider chỉ khi được duyệt.
- Dynamic capability và scope/quota handling.
- TikTok canonical model riêng.

Gate:

- Revoked OAuth/scope/quota tests.
- Không giả vờ có global search khi provider không cấp.
- Browser fallback vẫn disabled nếu chưa policy-approved.

### Phase 10 — Feature parity có chọn lọc và hardening

Độ lớn: XL.

Việc làm:

- Child comments đầy đủ nơi provider cho phép.
- [x] Reddit official root/child comment tree có manual execution API, separate root/child/total/depth/request budgets, tuần tự `/api/morechildren`, HMAC author và Mongo cascade.
- [x] Bluesky public AppView reply tree dùng `resolveHandle` + `getPostThread`, kiểm parent/root AT URI trong bộ nhớ, không persist DID/AT URI và dùng chung manual API/Mongo cascade.
- [x] Mastodon public status context dùng status + context endpoint trên exact configured instance, xác minh root identity và persist parent-first descendants với partial coverage tường minh.
- [x] Shared media downloader có SSRF boundary, MIME allowlist, atomic file, dedupe và file/byte quota; per-source operation vẫn chỉ bật sau retention/host acceptance riêng.
- Time-range, sort/type filters.
- [x] Parser drift alerts: typed source-run fields, `failed/parser_drift`, redacted SSE event và UI label; external notification policy vẫn là deployment gate.
- Performance/backpressure/load tests.
- Privacy/retention/delete flow.
- Comment/media operations cho sáu connector public/API khi product cần.
- Live canary nhỏ cho đủ 17 source/provider trạng thái được cấp.
- Manifest/frontend generated type và metric rendering audit.

Gate:

- Source/operation acceptance matrix được ký duyệt.
- Sáu connector giữ lại, bảy adapter thay bridge và bốn source official-provider đều có trạng thái đúng.
- Không có P0/P1 security, data-loss hoặc orphan-process issue.

### Phase 11 — Cutover và tháo MediaCrawler

Độ lớn: M.

Việc làm:

- Chuyển default từng source/operation sang registry/provider mới bằng feature flag.
- Theo dõi ít nhất hai chu kỳ run thành công cho từng adapter thay bridge.
- Dừng bridge cũ nhưng giữ rollback window có thời hạn.
- Chốt provenance/similarity/license audit.
- Gỡ submodule và dependency sau khi mọi gate đạt.

Gate:

- Đủ 17 source có trạng thái per-operation rõ và không có no-op quảng cáo ready.
- Bảy source cũ của MediaCrawler không còn vô tình gọi vendor.
- Sáu connector hiện hữu vẫn đạt regression suite.
- X/TikTok/Facebook/Instagram phản ánh đúng tier/scope/approval.
- Backend tests và frontend build đạt.
- Backup/rollback plan được thử.

## 17. Kế hoạch coexistence và cutover

Trong thời gian chuyển đổi:

- Bảy source vendor hỗ trợ provider legacy_bridge và cbce, nhưng chỉ một provider được ghi dữ liệu cho một source run.
- Sáu connector public/API giữ provider hiện tại sau compat wrapper; không shadow qua browser.
- X/TikTok/Facebook/Instagram có provider flag theo operation và approval.
- Shadow run chỉ so sánh count, ID hash, timestamp range và field completeness; không ghi duplicate vào production.
- Mỗi source/operation có flag chọn provider.
- Profile v2 tách khỏi profile bridge để tránh browser lock/corruption.
- Không xóa profile hay dữ liệu cũ trong cùng commit đổi provider.
- Shadow comparison không coi MediaCrawler là oracle đúng tuyệt đối; sai khác phải được phân loại do coverage, parser hoặc bug hai bên.

Tiêu chí đổi default một source/operation:

1. Contract test pass.
2. Hai live canary nhỏ pass ở hai thời điểm khác nhau.
3. Exact limit, checkpoint và dedupe pass.
4. Auth/permission/challenge/cancel pass theo provider.
5. Policy/provenance review pass.
6. UI hiển thị đúng capability/partial state.
7. Có rollback flag không cần deploy lại.
8. Nếu provider cần approval, approval thật đã được xác minh; không dùng test-role success làm bằng chứng production access.

## 18. Checklist gỡ MediaCrawler khỏi dự án

Chỉ thực hiện sau Phase 11 và trong commit riêng:

- Gỡ entry vendor/mediacrawler khỏi .gitmodules và git index đúng quy trình submodule.
- Gỡ backend/scripts/mediacrawler_adapter.py.
- Gỡ backend/scripts/mediacrawler_runner.py.
- Thay JsonlCommandConnector bridge bằng CBCE coordinator/connector.
- Gỡ config mediacrawler_command, mediacrawler_profile_dir và mediacrawler_timeout_seconds sau migration.
- Gỡ launcher action setup-mediacrawler và doctor checks liên quan.
- Gỡ marker data/mediacrawler-ready.
- Gỡ isolated venv/dependency setup cũ.
- Cập nhật README, ROADMAP, .env.example và UI label.
- Chuyển hoặc lưu trữ profile cũ chỉ sau khi người dùng xác nhận; không xóa tự động.
- Cập nhật test bridge cũ thành test worker protocol mới.
- Chạy tìm kiếm toàn repo với mediacrawler/MediaCrawler.
- Cho phép tài liệu audit này còn nhắc tên MediaCrawler như nguồn nghiên cứu; không coi đó là runtime dependency.
- Chạy license/dependency/similarity scan.
- Chạy backend test suite, frontend test/build và launcher doctor.
- Kiểm tra git status để không xóa nhầm thay đổi hiện có của người dùng.

## 19. Risk register

| Rủi ro | Xác suất | Tác động | Giảm thiểu |
|---|---:|---:|---|
| Điều khoản/API thay đổi | Cao | Cao | Provider abstraction, policy review và capability động |
| X API tier không có search cần thiết | Cao | Cao | Hiển thị permission_required; không fallback trái phép |
| TikTok Research API không được duyệt | Trung-cao | Cao | Display API scope nhỏ; product chấp nhận partial |
| Meta App Review/PPCA bị từ chối hoặc kéo dài | Cao | Cao | Owned assets trước; capability gate; không browser fallback |
| Facebook/Instagram không có global search thương mại | Cao | Cao | Allowlist/hashtag/owned/research providers và coverage disclaimer |
| Regression sáu connector đang chạy | Trung | Cao | Compat wrapper, golden/contract tests và staged flags |
| Cursor global hiện chưa persist | Cao | Cao | Phase 1 checkpoint migration + resume tests |
| Registry/channel/domain tiếp tục drift | Trung | Cao | Một SourceManifest + generated/parity tests |
| DOM/internal contract đổi | Cao | Cao | Fixture version, parser telemetry, circuit breaker |
| License contamination | Trung | Rất cao | Clean-room, provenance ledger, similarity/manual review |
| Challenge/risk-control | Cao | Trung-cao | Manual state, low rate, không bypass |
| Session/profile hỏng | Trung | Cao | Profile lock, v2 namespace, backup có xác nhận |
| Orphan browser trên Windows | Trung | Cao | Job Object, ownership registry, integration test |
| Duplicate/mất dữ liệu do checkpoint | Trung | Cao | Atomic upsert rồi mới commit checkpoint |
| Comment/creator crawl vô hạn | Trung | Cao | Request/item/time/cursor budgets |
| Secret/PII trong log | Trung | Rất cao | Redaction allowlist và snapshot security tests |
| Một adapter kéo dependency nặng | Trung | Trung | Extras theo provider, worker isolated environment nếu cần |
| API quota/chi phí tăng | Trung-cao | Cao | Cost budget, conditional fetch, cache, user-visible quota |
| Mastodon/federated instance partial outage | Cao | Trung | Per-instance status, partial warnings, origin identity |
| Full parity kéo dài | Cao | Trung | Source coverage trước, feature parity theo phase |

## 20. Backlog ưu tiên

### P0 — Bắt buộc trước code nền tảng

- GOV-001: phê duyệt clean-room và mục đích sử dụng.
- GOV-002: policy decision cho X/TikTok/Facebook/Instagram.
- GOV-003: mở external approval tracks và provenance ledger.
- ARC-001: SourceManifest + operation capability/availability schema.
- ARC-002: Canonical Content/Comment/Media models.
- ARC-003: Error taxonomy và RunContext.
- ARC-004: exact 17 canonical IDs + legacy alias migration.
- ARC-005: target detector + run planner + background safety.
- DAT-001: idempotent mapper/checkpoint transaction order.
- DAT-002: per-query/per-term provider cursor persistence.
- UI-001: manifest-driven source/operation controls.
- TST-001: generic adapter conformance + exact-17 contract.

### P1 — Giữ ổn định sáu connector và xây runtime

- YT-001: health/quota/config descriptor.
- YT-002: keyword/channel checkpoint và early-stop.
- WEB-001: feed validation/allowlist/date/canonical publisher.
- STM-001: app pinning/channel pagination/privacy parity.
- BSKY-001: AT URI identity/error taxonomy/checkpoint.
- MAST-001: hashtag capability/instance health/origin identity/pagination.
- RED-001: token cache/retry/checkpoint/privacy/score metric.
- REG-001: hợp nhất keyword + channel registries.
- REG-002: domain/canonicalizer table và hostile-domain tests.
- MIG-001: dy/ks/bili/wb dry-run migration + video library fix.
- RUN-001: Worker protocol version 1.
- RUN-002: Process supervisor/Job Object/cancellation.
- RUN-003: Browser profile lock và auth state.
- RUN-004: Secret redaction.

### P2 — Vertical slice, X và Meta

- BIL-001: Bilibili provider/provenance spike.
- BIL-002: Search/detail/creator.
- BIL-003: Root/child comments.
- X-001: X API auth/health/capability.
- X-002: Lookup/timeline/search.
- X-003: Conversation/media metadata.
- FB-001: Meta OAuth/token/revoke/deletion.
- FB-002: Owned Pages + webhook/reconciliation.
- FB-003: PPCA provider/approval gate.
- IG-001: Owned Professional media/comments/insights.
- IG-002: Business Discovery.
- IG-003: Hashtag provider/quota.
- META-001: Research provider boundary nếu được duyệt.
- OBS-001: Structured crawl events và metrics.

### P3 — Thay sáu MediaCrawler adapter còn lại

- WB-001 đến WB-003.
- TB-001 đến TB-004.
- XHS-001 đến XHS-004.
- DY-001 đến DY-004.
- KS-001 đến KS-004.
- ZH-001 đến ZH-004.
- Mỗi nhóm gồm provider research, parser, modes, comments và acceptance.

### P4 — TikTok, parity và cutover

- TT-001: OAuth/Login Kit.
- TT-002: Display API.
- TT-003: Research API nếu được duyệt.
- MED-001: Shared bounded downloader.
- [x] CMT-001: Comment hierarchy collections.
- [x] OPS-001: Live canary runner và parser drift alert.
- CUT-001: Source/operation-by-source/operation cutover.
- CUT-002: Remove MediaCrawler runtime.
- CUT-003: final license/provenance/similarity audit.

## 21. Định nghĩa hoàn tất

Engine chỉ được coi hoàn tất khi:

- Registry có đúng 17 source ID ổn định, không duplicate và không card no-op báo ready.
- Sáu connector hiện hữu pass regression keyword/channel và dùng chung registry/checkpoint.
- Bảy source MediaCrawler cũ chạy bằng adapter độc lập, không còn runtime vendor.
- X, TikTok, Facebook và Instagram có ít nhất một provider/operation đã implemented, dù availability có thể là permission_required với đường cấu hình/phê duyệt rõ ràng.
- permission_required chỉ được chấp nhận khi adapter, config flow, probe và contract tests đã tồn tại và có thể chuyển sang ready bằng credential/approval hợp lệ; static UnconfiguredConnector/no-op không đạt.
- Mỗi operation hiển thị đúng coverage, implementation state và runtime availability; operation tối thiểu cam kết của mỗi source không còn planned.
- Không có code, selector, signing, fixture hoặc payload constants sao chép từ MediaCrawler.
- Search/channel/detail/creator/comments/media hoạt động đúng nơi manifest tuyên bố; unsupported không silent no-op.
- Exact item/comment/request/time/media budget được kiểm thử.
- Global/channel checkpoint và upsert idempotent qua batch mới, crash và retry.
- Canonical/legacy IDs đã migration an toàn; video library đọc đúng source mới/cũ.
- User-visible login/challenge, không bypass.
- Cookie/token không xuất hiện trong argv, log, event hoặc database.
- Browser/process cleanup đạt trên Windows.
- X dùng provider chính thức mặc định.
- TikTok không bị trộn với Douyin và không quảng cáo capability ngoài scope.
- Facebook/Instagram không quảng cáo global search và không browser fallback khi thiếu quyền.
- UI được điều khiển bởi manifest: action, CTA, auth instruction, metric và coverage disclaimer.
- Backend, integration, security, provenance và frontend build gates đều đạt.
- MediaCrawler submodule và bridge đã được gỡ mà không xóa nhầm profile/dữ liệu người dùng.

## 22. Các quyết định cần chốt trước Phase 1

| Quyết định | Mặc định đề xuất |
|---|---|
| Mục đích thương mại hay nội bộ phi thương mại | Coi là có khả năng thương mại và áp clean-room nghiêm |
| X provider | X API; browser disabled |
| TikTok provider | Display API trước; Research API khi được duyệt |
| Facebook provider | Owned Pages trước; PPCA sau approval; research tách biệt |
| Instagram provider | Owned Professional + Business Discovery/Hashtag; research tách biệt |
| Sáu connector hiện hữu | Compat wrapper rồi cải tiến; không rewrite toàn bộ |
| Source IDs | Giữ 17 ID hiện tại; alias dy/ks/bili/wb chỉ để migration |
| Checkpoint | Provider cursor per query/term + generic watermark |
| Browser | Cốc Cốc managed, profile v2 riêng |
| CAPTCHA/slider | Người dùng xử lý thủ công |
| Proxy | Không bật mặc định; static proxy chỉ vì nhu cầu mạng |
| Raw payload | Không lưu mặc định; artifact redact + TTL khi debug |
| Creator PII | HMAC ID tối thiểu, không profile/follower graph |
| Comments | Root trước, child có budget ở phase sau |
| Media | Metadata trước, download opt-in có quota |
| Cutover | Theo từng source/operation bằng feature flag |
| Xóa vendor | Chỉ sau acceptance + provenance/similarity audit |

## 23. Bước tiếp theo được khuyến nghị

Không bắt đầu bằng việc sao chép adapter Bilibili hay XHS. Bước kế tiếp nên là Phase 0:

1. Chốt policy và clean-room.
2. Tạo SourceManifest đủ 17 nguồn, canonical IDs, operation matrix và run planner.
3. Bọc sáu connector hiện hữu; sửa checkpoint global/channel và registry drift trước.
4. Chạy song song hai lane: X API provider để thay đường 402, và fake browser adapter để hoàn thiện worker protocol/cancellation/redaction.
5. Dùng Bilibili làm browser vertical slice đầu tiên sau khi runtime pass.
6. External Meta/TikTok approval và provider scaffolding chạy song song từ Phase 0.

## 24. Nhật ký triển khai

### 2026-08-12 — Phase 0–1 backend và UI foundation

Đã hoàn thành:

- SourceManifest/SourceRegistry canonical đúng 17 nguồn, alias đọc tương thích `dy/ks/bili/wb` nhưng chỉ persist ID canonical.
- Capability theo từng operation/provider; X `render_embed` tách khỏi search, TikTok/Facebook/Instagram không còn quảng cáo no-op như handler thật.
- Runtime binding validation: operation `implemented` phải có connector, channel scanner hoặc renderer đã đăng ký.
- Target detection theo ranh giới DNS, có Tieba, mobile/short-link Bilibili và không nhận domain gần giống độc hại hoặc `/@` tùy ý là Mastodon.
- Run planner hợp nhất global source và saved channel thành hai selector độc lập; topic v1 được giữ chế độ channel-only để tránh đổi hành vi ngầm.
- Checkpoint envelope v1 theo source/provider/operation/query fingerprint và hashed cursor scope; YouTube, Steam, Bluesky, Reddit và channel pagination có cursor riêng.
- Checkpoint chỉ commit sau khi item đã qua persistence; failure/cancel giữ checkpoint durable cũ; timestamp không còn là hard reject gây mất bài đến trễ.
- Canonical pseudonymous identity cho Bluesky/Mastodon; raw payload channel được thu nhỏ đồng nhất với keyword path.
- Script `scripts/migrate_source_ids_v2.py` mặc định dry-run, chặn apply khi có unique-key collision.
- UI lấy operation status từ manifest, chỉ bật action có handler thật, có global source picker và chạy union global + channel.

Kết quả gate tại checkpoint này:

- 122 backend regression tests ngoài phạm vi subtitle pass.
- Frontend TypeScript/Vite production build pass.
- Ruff và `git diff --check` pass trên phạm vi crawler đã thay đổi.

### 2026-08-12 — Phase 2 runtime foundation

Đã thêm dưới feature flag `CONTENT_BOT_CBCE_ENABLED=false`:

- Immutable RunContext, RunBudgets, ContentRecord và event protocol.
- Typed failure taxonomy, cancellation token và safe event error.
- Bounded paginator với exact item/request/deadline budgets, empty-page guard và repeated-cursor detection.
- Supervisor với bounded event queue/backpressure, persistence-before-checkpoint ordering, cancellation và cleanup timeout.
- Fake adapter/failure injection tests cho success, storage failure, cooperative cancellation và queue backpressure.
- Provider override config được validate theo canonical source/provider/operation; profile root v2 tách khỏi profile bridge cũ.
- ProfileNamespace v2 dùng canonical source ID và account hash; ProfileLock khóa chéo tiến trình, không dùng chung profile với bridge cũ hoặc browser cá nhân.
- AuthStateMachine phân biệt session ready, cần đăng nhập và challenge; background run không tự mở luồng tương tác, interactive run chỉ chờ người dùng xử lý trên browser hiển thị và có timeout/cancel rõ ràng.
- Giao thức worker NDJSON `cbce.worker.v1` có identity, sequence, giới hạn kích thước, heartbeat, bounded backpressure và lọc duplicate/out-of-order event.
- Worker process nhận request/control qua stdin, stdout chỉ dành cho protocol, stderr được redact; secret bị cấm trong argv và environment. Supervisor sở hữu process group và kiểm thử hủy cả cây tiến trình con trên Windows.
- BrowserSession chỉ mở persistent profile do ứng dụng sở hữu qua một driver boundary, từ chối external profile/headless interactive session, giữ lock suốt vòng đời và force-close tài nguyên sở hữu khi graceful close quá hạn.

Gate hiện tại: 230 backend regression tests ngoài phạm vi subtitle pass, 1 live test opt-in được skip mặc định; Ruff, `git diff --check` và frontend TypeScript/Vite production build pass. CBCE vẫn tắt mặc định và chưa platform thật nào được cutover.

Bước kế tiếp là driver implementation trong worker và adapter Bilibili clean-room đầu tiên. Adapter chỉ được dùng hợp đồng/fixture do dự án tự thu; không sao chép WBI, selector, endpoint wrapper hoặc fixture của MediaCrawler. Legacy bridge tiếp tục là fallback cho đến khi các acceptance gate của Bilibili pass.

### 2026-08-13 — Bilibili vertical slice bắt đầu

- Thêm optional direct dependency `playwright==1.61.0` dưới extra `browser`; driver dùng persistent context riêng, browser hiển thị, sandbox bật, không custom fingerprint/anti-detection flags và không CDP attach.
- Worker supervisor dùng Windows Job Object với kill-on-close và `taskkill /T` fallback; integration test xác nhận descendant process không còn sau cancellation.
- Provenance ledger Bilibili tách Open Platform được ủy quyền khỏi global discovery. Provider chính thức không quảng cáo global keyword search; browser provider vẫn planned/disabled.
- Target parser nhận video/BV/av, creator, dynamic/opus và short URL; canonicalization có hostile-domain/userinfo/port/part-index regression tests.
- Provider-neutral Bilibili search contract và normalizer đã chạy xuyên qua CrawlerSupervisor bằng fake provider: exact limit, persistence-before-checkpoint, stable HMAC pseudonym, metric allowlist và parse-drift failure đều có tests.
- Sanitized contract-observation model chỉ giữ domain/path pattern đã khử ID, tên query/header không nhạy cảm và JSON shape không có value; hostile domain, userinfo, port và secret-leak đều có regression tests.
- Playwright browser extra đã được cài trong môi trường backend; smoke-test Cốc Cốc thật mở/đóng profile v2 tạm thành công mà không điều hướng, không truy cập cookie và không để lại profile.
- Rollout preflight kiểm browser dependency/executable/profile namespace và từ chối provider override nếu operation còn planned, kể cả `cbce_bilibili` và `bilibili_open_platform`.
- Từ quan sát clean-room bằng profile tạm đã triển khai provider public DOM search: page 1 không thêm `page=1`, cursor `term/page/offset` giữ chính xác phần còn lại của trang, parser compact count hỗ trợ `万/亿/K/M`, không gọi internal API hay triển khai WBI.
- Direct live canary và full isolated-worker canary đều lấy đúng 3 record công khai rồi đóng profile/process sở hữu; opt-in live smoke pass. Worker đóng adapter trước khi phát `complete`, hủy không phát complete và lỗi startup trước event đầu không còn làm wrapper chờ vô hạn.
- Shadow gate chỉ giữ HMAC digest trong RAM và serialize count/overlap/timestamp range/field completeness. Hai query/checkpoint độc lập, không commit checkpoint hay item; scratch bridge nằm trong UUID riêng và được xóa sau success/timeout.
- Baseline bridge chưa phát dữ liệu trong deadline tương tác. Retry 15 giây trả `BASELINE_TIMEOUT`, tự dọn toàn bộ process tree và scratch. Vì chưa có aggregate overlap và chưa đủ hai canary ở hai thời điểm, `cbce_bilibili` vẫn planned/disabled trong manifest và legacy bridge vẫn là default.

Phần search vertical slice đã có implementation clean-room và bằng chứng live, nhưng chưa đạt gate cutover. Việc tiếp theo là hoàn tất shadow comparison khi baseline đăng nhập sẵn, chạy canary thứ hai ở thời điểm khác, rồi mới đổi manifest search sang `implemented`. Detail, creator, comments và media vẫn phải đi qua provenance/contract riêng; không suy rộng quyền từ DOM search.

### 2026-08-13 — Bilibili operation parity hoàn tất ở mức experimental

- Bổ sung detail public DOM, creator video listing và cursor `page/offset`; cả ba mode search/detail/creator dùng canonical video identity và HMAC pseudonym policy thống nhất.
- Bổ sung root-comment và child-comment contract từ open Shadow DOM của trang. Root và child dùng cursor riêng; worker áp `max_root_comments`, `max_children_per_root` và `max_total_comments` độc lập, tránh lỗi semantics giới hạn của baseline.
- Phiên khách chỉ render preview/hot comments và hiện limit mask. Adapter trả `AUTH_REQUIRED` khi declared count lớn hơn dữ liệu render; không đánh dấu preview là complete và không gọi internal endpoint.
- Media mới dừng ở metadata công khai: HTTPS cover + duration có giới hạn. Player URL tạm thời và video download chưa bật.
- Search, detail, creator và comments đã dùng chung application-owned profile `bilibili/default`, giúp trạng thái login được tái sử dụng đúng mô hình browser-session của MediaCrawler mà không dùng profile cá nhân hay truyền cookie qua argv.
- Creator/comments đã được nối vào worker protocol với identity/operation validation, exact item/request/deadline budget, checkpoint event và cleanup trước terminal event.
- Focused Bilibili/worker suite: 76 tests pass; lint và diff whitespace check pass. Không sửa bất kỳ file subtitle nào.

Phase 3 đã đạt parity code ở mức experimental nhưng **chưa qua cutover gate**: còn thiếu logged-in comments canary, shadow overlap với legacy baseline và canary thứ hai ở thời điểm khác. Để tăng tốc tổng lộ trình, các phase provider kế tiếp có thể triển khai trên runtime đã ổn định trong khi legacy Bilibili tiếp tục là rollback path; manifest chưa chuyển sang `implemented`.

### 2026-08-13 — Phase 3 Bilibili second-canary gate

- Opt-in direct DOM smoke chạy lại ở checkpoint thời gian khác: exact `3` search records, canonical video URLs và detail của record đầu tiên đều pass; profile tạm được xóa sau run.
- Isolated `cbce.worker.v1` canary mới trả aggregate `{count: 3, all_canonical: true, persisted: false}` và đóng sạch process/profile sở hữu.
- Shadow comparison được thử lại với `max_items=3`, deadline legacy `30s`; candidate không persist nhưng legacy MediaCrawler vẫn trả typed `BASELINE_TIMEOUT` trước khi có aggregate overlap.
- Cleanup audit sau timeout: shadow scratch còn `0` child và không có process Python/Chrome/Cốc Cốc của run còn sống.
- Hai canary search ở thời điểm khác nhau nay đã đạt. Gate chưa đạt còn lại là responsive legacy overlap và logged-in comment coverage; không hạ gate hoặc tự suy “candidate pass = cutover pass”.

Phase 3 Bilibili hiện **two-canary complete, shadow-baseline blocked**. `cbce_bilibili` tiếp tục experimental/opt-in và legacy bridge vẫn là default cho tới khi baseline phát aggregate so sánh hoặc có quyết định governance thay thế gate bằng bằng chứng độc lập tương đương.

### 2026-08-13 — Phase 4 X official search contract

- Thêm `X_BEARER_TOKEN` riêng và connector official read-only; không dùng cookie/browser fallback và không có write/like/follow/message operation.
- Recent search dùng X API v2, capability `partial` do cửa sổ recent-search và access/credit hiện hành. Operation mặc định `manual_only` để scheduler không tự tiêu credit.
- Cursor versioned theo term, provider token hoặc `until_id`. Khi API trả tối thiểu nhiều record hơn item budget còn lại, checkpoint tiếp tục từ ID cuối đã emit, không nhảy qua record chưa persist.
- Chuẩn hóa post ID/canonical URL, metrics, hashtags và media metadata; author ID thành HMAC pseudonym, không lưu profile/display-name blob.
- Client primitives và adapter cho lookup một post, creator posts và recent conversation replies đã có. Target parser chặn hostile host/userinfo/port; creator/reply cursor dùng provider token hoặc `until_id` để giữ exact limit. Các operation vẫn `planned` cho đến khi có run-planner handler và cost-policy gate.
- Map 401/402/403/404/429/5xx thành taxonomy an toàn. `usage-capped` và 402 trở thành `PAYMENT_OR_ACCESS_REQUIRED`; raw provider detail/token/URL không xuất hiện trong lỗi.
- X embed vẫn là operation độc lập và luôn view-only. Khi thiếu token, catalog báo search `setup_required`; không gọi API rồi trả lỗi 402 thô.
- 12 provider tests và 23 kiểm thử X/catalog/planner liên quan pass. Full backend regression không gồm subtitle: 272 pass, 1 live opt-in skip. Không sửa file subtitle.

Phase 4 đang ở trạng thái **search implementation hoàn tất, detail/creator/replies adapter-ready, external-access gated**. Gate còn lại là token/credits thật, cost policy, live canary và run-planner binding cho ba operation bổ sung. Vì gate này phụ thuộc bên ngoài, Phase 6 browser adapters được phép triển khai tiếp trên runtime hiện có thay vì chờ.

### 2026-08-13 — Phase 4 X saved-creator channel integration

- `x_api/scan_channel` đã chuyển từ planned sang `implemented/partial`, dùng User Posts client/creator adapter đã có; public embed vẫn là operation độc lập và hoạt động khi không có token.
- Channel URL bắt buộc là account X/Twitter hợp lệ. Post/status URL bị từ chối ở save-time thay vì xếp nhầm thành channel.
- Timeline cursor versioned được scope theo username. `include_replies` và `include_reposts` ánh xạ thành official exclusions, đồng thời đi vào query fingerprint để thay đổi filter không resume cursor cũ.
- Run planner kiểm dynamic channel mode: thiếu `X_BEARER_TOKEN` thì không queue API source-run và giữ embed-only; có token thì provider/checkpoint provenance là `x_api/scan_channel`.
- Operation vẫn `manual_only` vì X API read pay-per-use. Không scheduler tự động, browser fallback, write/like/follow/message action hoặc raw provider response persistence.
- Focused X/channel/planner suite `48 passed`; full backend regression ngoài subtitle/Gemini `479 passed, 1 skipped`; Ruff và whitespace gate sạch.

Phase 4 hiện **search + saved creator timeline implementation-ready, external credits/live-canary gated**. Standalone detail và conversation replies vẫn planned ở run planner dù adapter contract đã có; bước đó cần target-aware execution surface và cost-policy riêng.

### 2026-08-13 — Phase 5 Instagram official hashtag provider

- Thêm official Graph API transport cho Instagram hashtag discovery; không có browser/cookie fallback. Graph version bắt buộc pin tường minh, bearer token chỉ đi trong Authorization header và injected HTTP client bị chặn nếu base URL không đúng `graph.facebook.com` hoặc bật redirect.
- Luồng gồm hashtag ID lookup và bounded `recent_media`; cursor versioned theo term/hashtag/provider `after`, exact item limit và natural transition sang term kế tiếp.
- Chuẩn hóa Graph media ID + canonical permalink, caption, UTC timestamp, like/comment counts và HMAC pseudonym. Không persist `media_url`/thumbnail URL tạm thời hoặc query token.
- Coverage được đánh dấu `best_effort`; lỗi auth/permission/App Review/rate/not-found/transport/parse đều typed và không nhúng provider response detail.
- Thêm settings trống cho pinned API version, Meta token và Instagram Professional user ID. Runtime chỉ `ready` khi cả ba giá trị hợp lệ; thiếu quyền vẫn fail typed và không fallback browser.
- Đã nối connector/run planner/manifest cho riêng hashtag search, thêm rolling ledger 30 unique hashtag/7 ngày theo Professional account và cache hashtag ID. Ledger chỉ giữ digest/timestamp/public Graph ID, corrupt state fail closed; owned/Business Discovery/comments/Facebook vẫn planned.
- Provenance và acceptance gates nằm tại `docs/crawler-provenance/META.md`; live canary vẫn chờ approved test asset.

### 2026-08-13 — Phase 6–8 contract acceleration

- Thêm manual login bootstrap dùng chung cho đúng bảy nguồn MediaCrawler: XHS, Douyin, Kuaishou, Bilibili, Weibo, Tieba, Zhihu. Mỗi nguồn dùng application-owned `source/default`; không attach profile cá nhân, không đọc/in cookie, không tự vượt challenge.
- Logged-out Weibo search redirect sang `passport.weibo.com`; logged-out Tieba search trả 403. Hai trường hợp được ghi provenance như auth/challenge gate, không coi zero item là success.
- Weibo/Tieba có strict target parser, canonical ID, provider-neutral post/thread contract, cursor `term/page/offset`, HMAC privacy và metric allowlist; DOM providers chờ authenticated observation.
- Weibo/Tieba DOM provider hiện đã contract-driven và test được toàn bộ lifecycle, exact offset/page/term cursor, typed parse-drift, auth callbacks, metric/timestamp extraction và privacy boundary. Selector mặc định vẫn để trống cho tới khi authenticated value-free DOM probe xác nhận; không dùng selector MediaCrawler.
- Thêm `cbce.dom-structure.v1` probe: chỉ giữ final host, tên tag/class/attribute và số lượng có giới hạn; không đọc text, URL, attribute value, UID hay cookie. Probe CLI chỉ cho phép Weibo/Tieba và app-owned profile.
- XHS/RedNote, Douyin, Kuaishou và Zhihu có strict target parser, gồm short-link classification nhưng chưa tự resolve. RedNote được thêm vào canonical domain manifest.
- Thêm shared browser-video contract cho XHS/Douyin/Kuaishou: platform-specific canonicalizer/provider vẫn riêng, chỉ dùng chung lifecycle normalizer cho title/body/author HMAC/metrics/media metadata.
- Vòng worker search đã được tách thành executor dùng chung cho mọi `SearchAdapter`: validate source/provider, exact item/request/deadline budget, checkpoint sau từng batch và luôn đóng browser trước terminal event. Bilibili đã chuyển sang executor này; Weibo/Tieba và nhóm video chỉ cần gắn provider, không lặp worker lifecycle.
- Shared owned-browser navigation nay có manual-auth continuation: khi redirect sang login host đã khai báo, worker có thể phát `auth_required`, chờ tối đa có giới hạn, tự nhận host hợp lệ sau khi user đăng nhập rồi phát `authenticated`. Không đọc cookie, không tự giải CAPTCHA và fail closed nếu redirect ra ngoài domain policy.
- Focused gates: login/profile 12 pass; Weibo/Tieba target+adapter 32 pass; Phase 7 target/shared adapter 38 pass. Không sửa file subtitle.
- Strict targets cho TikTok/Facebook/Instagram và shared owned-browser navigation/error mapping cũng đã có regression tests. Gate tổng mới nhất: 350 backend tests pass, 1 live opt-in skip; ruff sạch; frontend production build pass. Các cảnh báo line-ending ở file dirty có sẵn không phải thay đổi subtitle của crawler work.

Phase 6–8 hiện **adapter-contract ready, authenticated observation pending**. Việc tiếp theo sau khi user đóng phiên manual login là thu selector/state đã sanitize từ chính profile CBCE rồi gắn DOM provider; không dùng selector/signing của MediaCrawler.

### 2026-08-13 — Phase 6 Tieba keyword vertical slice

- Probe giá trị-rỗng do dự án tự chạy đã xác nhận các landmark semantic của danh sách kết quả Tieba. Chỉ class/tag signature và path shape đã khử ID được giữ lại; không lưu text, ID, cookie, header, response body hay fixture từ MediaCrawler.
- Contract mặc định Tieba nhận diện result container, thread card, canonical thread link, title, abstract và reply count. Control “load more” của khối hot-topic đã bị loại sau khi canary chứng minh nó không phải pagination của thread list.
- `cbce_tieba` đã đi xuyên isolated worker, shared bounded executor, manual-auth events, checkpoint và cleanup. Manifest chỉ đánh dấu keyword `search` là `implemented/partial`; forum/detail/creator/comments/media vẫn `planned` và không có nút chạy giả.
- Hai canary tách biệt đều trả `4` record và `4` reply metrics rồi đóng toàn bộ tài nguyên sở hữu. Shadow không persist đã trả `BASELINE_TIMEOUT` sau 60 giây vì MediaCrawler baseline không emit item; scratch và process tree được dọn sạch.
- Rollout vẫn là opt-in kép (`CONTENT_BOT_CBCE_ENABLED` + override `tieba.search=cbce_tieba`). Legacy bridge còn là default cho tới khi có aggregate overlap từ baseline phản hồi.
- RunManager second-run acceptance đã pass: cùng canonical Tieba thread chỉ tạo một content item/match; lần quan sát sau vẫn cập nhật metric và thêm snapshot nhưng không tăng `new_item_count`. Trong lúc khóa gate này đã sửa việc checkpoint v1 sai fingerprint mang theo `recent_ids` cũ và việc refresh existing match bị đếm thành bài mới.
- Weibo probe trên đúng origin `s.weibo.com` tiếp tục trả `AUTH_REQUIRED` và timeout an toàn khi profile chưa được đăng nhập. Vì vậy selector mặc định/capability Weibo chưa được bật hoặc suy đoán.
- Full backend regression ngoài phạm vi subtitle: `410 passed, 1 skipped`; Ruff/diff whitespace gate sạch cho phần crawler vừa đổi.

Phase 6 hiện **Tieba keyword experimental-ready, Weibo authentication-gated**. Bước Tieba còn lại trước default cutover là một shadow aggregate có baseline MediaCrawler phản hồi; bước Weibo cần người dùng là đăng nhập cửa sổ của profile ứng dụng để thu observation giá trị-rỗng.

### 2026-08-13 — Phase 7 live authentication boundary

- Mở rộng probe giá trị-rỗng cho XHS, Douyin và Kuaishou bằng đúng profile `source/default` và public search target; 12 kiểm thử probe/shared DOM liên quan pass.
- XHS tới đúng domain nhưng có `0` link `/explore/`, `0` note item và `6` login controls. Douyin có `0` link `/video/`. Kuaishou có video-list shell nhưng `0` link `/short-video/`; tám `a.item` quan sát được nằm ngoài video list.
- Cả ba được phân loại `authentication-gated`, không phải empty-success hoặc parse-success. Không selector mặc định, worker binding hay manifest capability nào được bật từ layout logged-out.
- Provenance riêng nằm tại `docs/crawler-provenance/XHS_DOUYIN_KUAISHOU.md`; red zone vẫn gồm XHS signing, Douyin `a_bogus`/slider automation và Kuaishou capture hook.

Phase 7 hiện **shared lifecycle ready, platform contracts authentication-gated**. Sau khi người dùng đăng nhập từng profile ứng dụng, quy trình tiếp theo là authenticated value-free observation → source-specific contract → direct/worker canary → shadow gate; không nhảy thẳng từ khung chung sang capability `ready`.

### 2026-08-13 — Phase 7 same-host authentication acceleration

- Runtime browser nay nhận biết thêm login gate cùng domain, không chỉ redirect sang login host. Policy giữ selector tối thiểu theo từng source và grace window có giới hạn để bắt modal tải trễ.
- Probe độc lập xác nhận XHS QR container, Douyin telephone-login form và Kuaishou login detail panel. Cả ba hiện trả typed `AUTH_TIMEOUT` khi chưa hoàn tất đăng nhập, thay vì `0 items` hoặc `PARSE_CHANGED` sai nguyên nhân.
- Không đọc số điện thoại, QR payload, cookie hay nội dung trang; ảnh chẩn đoán tạm đã được xóa sau khi xác nhận trạng thái. Không có slider/challenge automation.
- Focused auth/probe tests pass; full backend regression ngoài subtitle đạt `412 passed, 1 skipped`. Phần subtitle không bị sửa bởi slice crawler này.

Phase 7 hiện **auth state machine ready, authenticated platform selectors pending**. XHS/Douyin/Kuaishou có thể tiếp tục ngay khi người dùng hoàn tất QR/login trong ba profile ứng dụng.

### 2026-08-13 — Phase 8 Zhihu probe started

- Đã thêm Zhihu vào value-free DOM probe với neutral search target và profile ứng dụng `zhihu/default`.
- Live navigation đầu tiên trả HTTP 403; runtime phân loại `CHALLENGE_REQUIRED` trước khi khai báo selector hoặc coi zero item là thành công.
- Strict answer/article/zvideo target parser vẫn là phần đã sẵn sàng. Search/detail/creator/comments tiếp tục planned cho tới khi challenge/login được hoàn tất trong cửa sổ ứng dụng.
- Provenance nằm tại `docs/crawler-provenance/ZHIHU.md`; không dùng copied `x-zse/x-zst`, JS, endpoint wrapper hay fixture của MediaCrawler.

Phase 8 hiện **probe-ready, challenge-gated**; contract/provider implementation sẽ tiếp tục ngay sau authenticated observation.

### 2026-08-13 — Phase 9 TikTok Display API acceleration

- Đã triển khai clean-room official TikTok Login Kit primitives: authorization URL có state validation, server-side code exchange, refresh-token rotation và revoke trên exact `open.tiktokapis.com`; redirect bị tắt và secret/token không vào URL hoặc repr.
- Đã triển khai `/v2/video/list/` và `/v2/video/query/` với exact cap 20, millisecond cursor, typed auth/scope/rate/transport/parse errors, UTC normalization, HMAC author pseudonym và metric allowlist.
- Connector/manifest/channel scanner chỉ cho phép `@username` đúng tài khoản đã OAuth; account khác bị từ chối. Video/short URL không thể lưu nhầm thành channel. Display API không được quảng cáo là global search.
- Runtime config mới giữ client metadata/token server-side; source vẫn `setup_required` nếu thiếu access token, open ID, authorized username hoặc scope `video.list`.
- Research API search/comments tiếp tục `planned/disabled` vì cần approval dự án riêng. Browser fallback vẫn disabled.
- Provenance và official references nằm tại `docs/crawler-provenance/TIKTOK.md`.

Phase 9 hiện **Display provider + connector + encrypted token vault + automatic refresh + Web Login Kit callback/UI implementation-ready**. Vault dùng AES-256-GCM, atomic replace, tách key/ciphertext, ưu tiên credential đã mã hóa và lưu refresh token mới sau rotation. Callback bắt buộc cùng approved HTTPS origin, dùng state một lần kết hợp HttpOnly/Secure/SameSite=Lax cookie, không đưa code/token về frontend. Catalog nay gửi `primary_operation`, vì vậy thẻ TikTok chạy theo `scan_channel`, không còn nhìn nhầm global search. Gate còn lại là live canary bằng app/test account đã được TikTok duyệt; Research API vẫn phụ thuộc phê duyệt riêng.

- Account binding không còn tin username do UI nhập: consent yêu cầu `user.info.profile`, callback gọi official `/v2/user/info/`, so khớp cả `open_id` và username, rồi revoke ngay nếu user đăng nhập nhầm creator.

- Verification khi tạm dừng: `436 passed, 1 skipped` cho toàn bộ backend test ngoài subtitle/Gemini; Ruff sạch trên toàn bộ file TikTok và wiring liên quan. Không sửa chức năng subtitle trong slice này.

### 2026-08-13 — Phase 10 operation truth, retention và delete hardening

- Registry nay resolve provider theo operation thực thi thay vì dùng mù `default_provider_id`. X search checkpoint thuộc `x_api`, Instagram hashtag thuộc `instagram_hashtag`, còn provider override CBCE giữ đúng provenance riêng.
- Availability của CBCE được gate theo từng provider/operation bằng feature flag, provider selection và browser/profile preflight. Catalog và run planner không còn bật clean-room operation chỉ vì legacy MediaCrawler bridge đang healthy.
- Instagram hashtag đã chuyển từ adapter rời sang connector chính thức có handler thật. Catalog chỉ bật khi pinned Graph version/token/Professional account ID đầy đủ; coverage luôn `partial/best_effort`, không quảng cáo full-text Instagram.
- Retention content crawler mặc định 90 ngày, chạy định kỳ và cascade content item, keyword match, metric snapshot cùng normalized comments. Media/debug artifacts chưa có per-source persistent operation được bật; khi bổ sung phải đăng ký cùng retention boundary trước rollout.
- API xóa dữ liệu theo canonical source cascade content/match/snapshot/source-run, reset global/channel checkpoint nhưng giữ topic/channel configuration. Câu xác nhận chính xác là bắt buộc.
- Browser profile ứng dụng không bao giờ bị auto-retention. Xóa profile cần xác nhận mạnh hơn, lock toàn bộ account profile của source, từ chối khi crawler đang chạy và chặn session mới bằng delete marker trước khi recursive delete exact source subtree.
- Full backend regression ngoài subtitle/Gemini: `460 passed, 1 skipped`; focused lint và whitespace gates sạch. Frontend ESLint, `28/28` tests và production build pass. Không chỉnh sửa chức năng subtitle trong Phase 10.

Phase 10 internal hardening ở trạng thái **implemented**. External canary/approval gates còn lại không thể tự hoàn tất trong repo: TikTok approved app/test creator, Instagram approved Professional test asset, X token/credits, Meta Page permissions, cùng authenticated browser observations cho XHS/Douyin/Kuaishou/Weibo/Zhihu. Các operation đó tiếp tục `setup_required`, `planned` hoặc experimental thay vì bị khai báo ready giả.

- Gate nội bộ mới nhất: `449 passed, 1 skipped` cho backend ngoài subtitle/Gemini; frontend `eslint`, `28` Vitest tests và production build đều pass. Live canary duy nhất còn thiếu cần TikTok app/scopes/callback được duyệt và test creator opt-in.

### 2026-08-13 — Phase 5 Facebook authorized Page provider

- Thêm Graph API transport độc lập cho feed của đúng một Facebook Page được cấu hình/ủy quyền; không dùng MediaCrawler, browser cookie hoặc fallback scraping. Provider không quảng cáo global Facebook search, personal profile, Group hay arbitrary public Page.
- Graph version bắt buộc pin; Page token chỉ đi trong Authorization header; transport khóa exact `graph.facebook.com`, tắt redirect và phân loại an toàn auth/permission/App Review/rate/not-found/transport/parse errors.
- Endpoint Page feed dùng explicit field allowlist, page size 1–100 và cursor versioned. Connector chỉ nhận Page ID/username trùng cấu hình; URL post/reel/video/watch/short-link không thể bị lưu nhầm thành channel.
- Normalizer giữ Graph post ID, canonical HTTPS Facebook permalink, text/time, reaction/comment/share metrics và HMAC author pseudonym. Không persist raw Page name, token, media URL hoặc raw response.
- Manifest `meta_pages/scan_channel` chuyển sang `implemented/partial`; `search`, PPCA, profile/Group, detail và comments vẫn disabled/planned. Frontend nhận biết đây là nguồn chỉ-quét-kênh và hướng sang mục Kênh đã lưu thay vì gọi nhầm global search.
- Tài liệu Meta chính thức trả HTTP 429 trong lần automated refresh ngày 2026-08-13; provenance ghi rõ cần human review theo Graph version/scopes hiện hành và live canary bằng Page test asset được duyệt trước rollout.
- Gate nội bộ cuối: backend ngoài subtitle/Gemini `474 passed, 1 skipped`; Ruff và whitespace gate sạch. Frontend ESLint sạch, `28/28` Vitest tests và production build pass. Slice này không thay đổi chức năng subtitle.

Phase 5 Facebook hiện **implementation-ready, external permission/live-canary gated**. Việc còn lại không thể hoàn tất chỉ bằng mã nguồn là cấp Page access token hợp lệ, xác nhận Page task/App Review/Advanced Access theo use case và chạy canary nhỏ trên Page đã ủy quyền. PPCA là provider riêng và không được suy ra từ thành công của owned Page.

### 2026-08-13 — Phase 6 Tieba detail/forum/creator acceleration

- Observation giá trị-rỗng do dự án tự chạy đã chốt ba contract semantic: thread detail, forum `.thread-card` listing và creator `.thread-card` listing. Không lưu URL/ID/text/account value từ phiên quan sát.
- Thêm adapter/provider cho `fetch_detail`, `scan_channel` forum và `list_creator`; cursor listing được ràng buộc bằng kind + HMAC-free SHA-256 digest của canonical target để cursor của forum/creator này không dùng nhầm cho target khác.
- Cả ba operation dùng chung `tieba/default`, manual-auth events, exact item/request/deadline budgets, HMAC author policy và `cbce.worker.v1`. Direct canary và isolated-worker canary đều đạt `detail=1, forum=4, creator=4`; ba terminal worker đều `complete`.
- Sửa channel canonicalization để giữ canonical `?kw=` của forum Tieba và từ chối thread URL làm channel. RunManager canary trên mongomock đạt `batch/source=succeeded`, fetched/new/items đều `4`, không ghi production DB.
- Manifest `cbce_tieba` hiện đánh dấu search/detail/forum/creator/root-comments là `implemented/partial`, vẫn chỉ chạy khi bật CBCE + override rõ ràng. Root comment dùng numeric `data-id` từ ancestor đã quan sát; worker canary đạt `5/5` stable identities và terminal `complete`. Probe kế tiếp thấy `.pb-lzl-item` nhưng node con không có numeric `id/data-id`; ancestor numeric gần nhất chính là root comment. Canary child vì vậy trả 0 theo fail-closed và operation tiếp tục `planned`; không dùng root/index/nội dung làm ID giả.

Phase 6 Tieba hiện **search/detail/forum/creator/root-comments experimental-ready**. Hai gate còn lại là child-comment contract và responsive MediaCrawler shadow overlap trước default cutover; Weibo vẫn authentication-gated.

### 2026-08-13 — Phase 1 Reddit OAuth/privacy hardening

- Application-only OAuth token grant đã chuyển sang exact official host `www.reddit.com`; data request tiếp tục chỉ dùng `oauth.reddit.com` và mọi redirect đều bị tắt.
- Thêm token cache dùng chung cho keyword và saved-channel scan: cache theo credential fingerprint, làm mới trước expiry, chống request stampede bằng async lock và retry bounded cho lỗi transport/408/425/429/5xx.
- Token/secret bị loại khỏi repr, URL, checkpoint, raw payload và provider error. Auth/rate/transport/parse failure trả taxonomy an toàn thay vì log response chi tiết.
- Global và channel đã đồng nhất HMAC author pseudonym, score/comment metrics và raw allowlist; raw Reddit username/full response không còn được lưu ở channel path.
- `after` được checkpoint riêng theo term hoặc subreddit/user target; natural exhaustion ghi tombstone và channel có thể paginate quá 100 trong item budget.
- Deterministic regression khóa cache reuse/expiry/credential rotation/concurrency/retry/redaction, endpoint chính thức và privacy parity keyword-channel. Provenance nằm tại `docs/crawler-provenance/REDDIT.md`.

RED-001 hiện **implemented internally**. Gate ngoài repo còn lại là live canary với app/use case được Reddit chấp thuận; comment tree và typed fixtures cho community bị quarantine/private/banned vẫn là công việc kế tiếp, không được quảng cáo là đã hỗ trợ.

### 2026-08-13 — Phase 1 YouTube health/checkpoint/quota hardening

- YouTube source manifest nay khai đúng API-key auth, partial rolling-window coverage, saved-channel capability và disclaimer về project quota; local configured không còn được mô tả như remote healthy.
- `GET /api/v1/sources?deep=true` chạy probe `i18nLanguages.list` một unit mỗi attempt với retry bounded và trả operation-level `auth_required`, `rate_limited` hoặc `degraded` có reason code an toàn. Catalog mặc định vẫn chỉ probe local để không âm thầm tiêu quota.
- Keyword và uploads-channel scan luôn đọc frontier mới nhất trước. Khi gặp trang toàn canonical ID đã quan sát, connector mới tiếp tục cursor backlog cũ; natural overlap/EOF xóa cursor, `invalidPageToken` fail-safe xóa cursor, còn item/quota exhaustion giữ đúng continuation chưa xử lý.
- Fingerprint YouTube gồm provider contract, newest-first ordering, region, relevance language và rolling-90-day policy; đổi semantics tự reset cursor.
- Thêm per-run budget tách `search.list` và general reads, redirect-off transport, typed/redacted auth/quota/rate/not-found/parse/transport errors, hashtag normalization và raw payload allowlist chung giữa global/channel.
- Deterministic tests khóa frontier-before-backlog, expired cursor, exact continuation, saved-channel parity, separate quota buckets, deep-health catalog state và official error taxonomy. Provenance nằm tại `docs/crawler-provenance/YOUTUBE.md`.

YT-001 và YT-002 hiện **implemented internally**. Gate ngoài repo còn lại là live canary bằng API key/quota allocation thật; comment records và media download vẫn là operation riêng chưa được quảng cáo.

### 2026-08-13 — Phase 1 Game news RSS/Atom hardening

- `web` được khóa thành RSS/Atom provider, không còn là arbitrary HTML crawler. Template phải HTTPS, public literal host, chỉ dùng `{query}`; JSON array/newline config giữ được comma trong URL trong khi legacy comma input vẫn đọc được.
- HTTP redirect tự động bị tắt. Feed transport giới hạn ba hop, chỉ chuyển sang host thuộc allowlist cấu hình, giới hạn body 2 MB và từ chối HTML/non-feed XML/local literal target.
- Parser hỗ trợ RSS/Atom links, author, RFC 822/3339/ISO timestamp về UTC, sanitized summary/content, publisher provenance và enclosure/thumbnail metadata. Raw payload chỉ giữ provider/feed digest/host/term/publisher/media allowlist.
- Conditional ETag/Last-Modified checkpoint dùng scope hash theo feed template + term và chỉ commit validator sau khi consume toàn document; item cap không thể đánh dấu nhầm các entry chưa emit.
- Lỗi một feed phát warning event, giữ kết quả feed khác và kết thúc source phase `completed_with_warnings`; toàn bộ feed invalid mới trả typed failure. Saved URL trên configured host vẫn phải vượt content-type/XML validation trước khi được ingest.
- Deterministic fixtures khóa JSON template có comma, unsafe scheme/local host, same-host/cross-host redirect, HTML rejection, partial failure, Atom/RSS/date/publisher/media, conditional 304 và warning-visible RunManager behavior. Provenance nằm tại `docs/crawler-provenance/GAME_NEWS_FEEDS.md`.

WEB-001 hiện **implemented internally**, ngoại trừ việc giải mã Google News aggregator article URL bị chủ động loại khỏi clean-room provider vì không có contract được hỗ trợ. Feed-provided publisher article URL vẫn được canonicalize; aggregator link giữ nguyên cùng provenance rõ ràng.

### 2026-08-13 — Phase 1 Steam discovery/pagination/privacy hardening

- Steam keyword discovery và review crawl đã tách policy. Exact normalized title thắng; tied/low-confidence match phát warning và không tự chọn DLC/demo/soundtrack. Saved Store/Community App URL được canonicalize thành `/app/<numeric-id>/`, tạo explicit pinned-game channel qua UI hiện hữu.
- Keyword và saved-app scan luôn đọc `cursor=*` frontier trước; chỉ quay lại backlog sau overlap với recent compound IDs. Cursor scope/fingerprint gồm app, filter, language, purchase type và provider-contract version.
- Saved app phân trang quá 100, request đúng remaining item count, dừng repeated/empty cursor và giữ continuation khi item/request budget hết. Discovery/review có hai budget riêng.
- `app_id:recommendation_id` là canonical external identity. Global/channel dùng chung normalizer/raw allowlist; author object, Steam ID, ownership/playtime và unexpected fields bị loại. `helpful_votes` giữ đúng semantics trong descriptor/raw, legacy `like_count` mapper vẫn explicit.
- Per-review deep link bị chủ động bỏ vì cần nhúng reviewer account identifier; canonical URL giữ public app review list để ưu tiên privacy. Deterministic tests khóa ambiguity, pinned target, frontier-before-backlog, >100/exact cap, repeated cursor, request budgets và privacy parity. Provenance nằm tại `docs/crawler-provenance/STEAM.md`.

STM-001 hiện **implemented internally**. Gate còn lại là live canary schema/rate của public Store surfaces; filter/language per-topic UI có thể thêm sau nếu nhu cầu sản phẩm yêu cầu, không ảnh hưởng checkpoint correctness hiện tại.

### 2026-08-13 — Phase 1 Bluesky identity/cursor/transport hardening

- Keyword và saved-author scan chỉ dùng public AppView `public.api.bsky.app`, redirect-off transport và User-Agent nhận diện Content Bot. Lỗi 400/401/403/404/429/5xx/transport/JSON được phân loại, retry bounded và không còn biến outage thành EOF thành công.
- Canonical external ID được băm ổn định từ AT URI `app.bsky.feed.post`; CID chỉ nằm trong raw allowlist như content version nên edit không tạo record mới. DID và full profile blob không được persist.
- Mỗi normalized term có cursor scope riêng. Mỗi polling cycle đọc newest frontier trước, chỉ nối backlog sau overlap recent IDs; invalid cursor được xóa hẹp, repeated cursor và request budget đều dừng hữu hạn.
- Saved-author feed canonicalize đúng một profile target, phân trang quá 100, giữ include-reply/include-repost policy và không coi trang chỉ chứa repost bị lọc là EOF giả.
- Reply parent/root, quoted post và repost discovery được normalize thành stable pseudonymous relation IDs. Ảnh, video và external embed qua HTTPS allowlist với field/length/count bounds; keyword/channel dùng chung raw payload normalizer và exact like/reply/repost/quote metrics.
- Deterministic regression khóa edit identity/CID version, multi-term resume, frontier-before-backlog, typed outage, invalid/repeated cursor, filtered repost pagination, author feed >100, relation/media bounds và target validation. Provenance nằm tại `docs/crawler-provenance/BLUESKY.md`.

BSKY-001 hiện **implemented internally**. Manifest ghi coverage partial/best-effort; gate ngoài repo còn lại là live canary định kỳ đối chiếu AppView schema/rate behavior, không được diễn giải public search như full firehose.

### 2026-08-13 — Phase 1 Mastodon federation/pagination/health hardening

- `MASTODON_INSTANCES` trở thành exact runtime allowlist (comma/newline/JSON), chỉ nhận public HTTPS origins; target detector không còn nhận mọi website có `/@`. Saved target phải là đúng một profile trên instance đã cấu hình, status URL bị từ chối.
- Capability được mô tả đúng là hashtag discovery theo từng instance. Hashtag normalize Unicode-alphanumeric có bound; query fingerprint khóa provider contract và ordered instance list.
- Keyword và account scan dùng chung redirect-off typed transport, Content Bot User-Agent và per-run request budget. 401 public-preview-disabled, 403, 404, 429, 5xx, transport và parse failure không còn bị nuốt thành zero-result success.
- `GET /api/v1/sources?deep=true` probe `/api/v2/instance` theo từng host; all-down và partial outage đều trả `degraded` với reason code, trong khi catalog mặc định vẫn chỉ kiểm cấu hình local.
- Pagination lấy `max_id` từ Link `rel=next`, cursor scope theo instance+hashtag hoặc account+filter, dừng repeated cursor và giữ exact cap. Một instance lỗi phát warning hữu hình rồi tiếp tục host khác; tất cả thất bại trả typed failure.
- Status remote/federated dùng canonical origin URI làm stable external ID và origin URL làm canonical link, nên local-copy ID/fetching instance không tạo duplicate. Boost được unwrap về original; reply local relation được pseudonymize; media/spoiler/metrics có allowlist và bounds.
- HTML content đi qua standard-library `HTMLParser` với entity decoding/block preservation. Keyword và channel dùng cùng normalizer, không persist account object/local account ID.
- Deterministic tests khóa configured/hostile targets, origin duplicate/edit identity, reblog/reply/media, HTML entities, partial/all-instance outage, deep health, Link pagination >40, exact cap và cursor stall. Full backend regression ngoài phụ đề đạt `554 passed, 1 skipped`.

MAST-001 hiện **implemented internally**. Gate ngoài repo còn lại là opt-in live canary trên từng instance cấu hình và app-token provider riêng nếu một instance tắt public preview; không fallback browser/cookie và không quảng cáo coverage toàn Fediverse.

### 2026-08-13 — Browser profile gate revalidation after Phase 1

- Value-free DOM observation now reports bounded canonical path-shape counts without returning URL, ID, text or attribute value. Contracts are source-declared (`/explore/`, `/video/`, `/short-video/`, `/detail/`, `/p/`, `/question/`, `/zvideo/`) and only same-domain anchor counts leave the browser.
- Douyin, Kuaishou and Weibo profiles returned typed `AUTH_TIMEOUT`; they remain login-gated. XHS rendered zero `/explore/` links plus an independently observed login control, so its same-host login sentinel was corrected and it remains auth-gated rather than false empty-success.
- Zhihu rendered the independently observed `div.unhuman` challenge shell and zero answer/article/video paths. The probe now returns typed `CHALLENGE_REQUIRED` for this same-host state instead of accepting the challenge DOM.
- No source-specific content selector was declared from these logged-out/challenge shells. XHS/Douyin/Kuaishou/Weibo/Zhihu default provider states remain planned or experimental exactly as their available evidence supports.

### 2026-08-13 — Phase 10 shared bounded media downloader

- Thêm runtime primitive `BoundedMediaDownloader`; không tự bật `media_download` cho source nào. Adapter phải truyền exact host allowlist, passive MIME policy và per-run file/byte budgets.
- URL chỉ HTTPS/443/no-userinfo, redirect được follow thủ công có giới hạn và revalidate từng hop. HTML/SVG active content, host ngoài allowlist, private/local literal, oversized Content-Length và streaming overflow đều fail typed.
- Byte stream ghi `.part` cùng thư mục, flush+fsync rồi atomic replace; cancellation/error xóa partial. Artifact metadata bỏ query string để không persist signed URL/token.
- Duplicate URL không fetch lại trong run; URL khác cùng SHA-256 content dùng chung một file. Retention/delete registration vẫn là gate bắt buộc trước khi một source bật byte download.
- Focused downloader/runtime tests pass; full backend regression ngoài phụ đề sau probe+downloader đạt `570 passed, 1 skipped`. Provenance nằm tại `docs/crawler-provenance/SHARED_MEDIA_DOWNLOADER.md`.

### 2026-08-13 — Phase 10 Reddit comment hierarchy and persistence

- Thêm normalized Mongo `comments` collection với unique identity `(source_id, external_id)`, content/root/parent indexes, idempotent upsert và parent-content integrity check. Xóa item, xóa keyword orphan, xóa source, automatic retention và offline prune đều cascade comment; backup/restore tiếp tục tự bao gồm collection mới.
- Reddit `list_comments` chuyển sang `implemented/partial/manual_only` vì đã có callable connector và API thật, không chỉ là manifest placeholder. API manual nằm tại `POST /api/v1/items/{id}/comments/scan`; normalized result đọc tại `GET /api/v1/items/{id}/comments`.
- Official adapter dùng `/comments/{article}` và `/api/morechildren`, tối đa 100 child ID/call, chỉ một morechildren request tại một thời điểm, typed error và physical request budget. Root, child-per-root, total, depth được giới hạn độc lập; repeated/missing hierarchy fail closed.
- Reddit author chỉ rời adapter dưới HMAC pseudonym. Comment fullname, parent/root ID, body bounded, timestamp và score semantics được giữ; username, account object, token và raw response không được persist.
- Focused gate sau slice: `35 passed`; full backend regression ngoài subtitle/Gemini/text đạt `580 passed, 1 skipped`; Ruff và diff-whitespace sạch. Live canary vẫn cần Reddit app/use case được chấp thuận và post test cho private/quarantine/banned state. Không có chức năng subtitle nào được sửa trong slice này.

### 2026-08-13 — Phase 10 YouTube official comment tree

- `youtube_public/list_comments` chuyển sang `implemented/partial/manual_only` sau khi có callable connector, official adapter, generic manual API và shared Mongo persistence. API/key/quota availability tiếp tục dùng health hiện hữu; thiếu key không tạo operation ready giả.
- Strict target nhận video ID, watch, youtu.be, shorts/live/embed URL rồi canonicalize về watch URL; hostile host, HTTP và userinfo fail closed.
- Root dùng `commentThreads.list(part=snippet,textFormat=plainText)`. Adapter không tin embedded reply preview; khi `totalReplyCount > 0`, child luôn đi qua `comments.list(parentId=...)`, tối đa 100/page với repeated-cursor guard.
- Root, replies-per-root, total và physical request budget độc lập. `commentsDisabled` có typed `UNSUPPORTED`; parent mismatch/response drift bị từ chối trước persistence. Author display name không được giữ; chỉ `authorChannelId.value` đi qua HMAC trong bộ nhớ.
- YouTube và Reddit cùng dùng `POST /api/v1/items/{id}/comments/scan`, `GET /api/v1/items/{id}/comments`, hierarchy validation, idempotent upsert và retention/delete cascade. Media download vẫn là operation riêng.
- Full backend regression ngoài subtitle/Gemini/text sau lát cắt đạt `586 passed, 1 skipped`; focused Ruff sạch. Không thay đổi mã chức năng subtitle.

### 2026-08-13 — Phase 10 Tieba/Bilibili comment persistence boundary

- CBCE worker comment payload không còn rời connector dưới dạng dictionary thô. Shared decoder tái kiểm tra source/content/comment ID, UTC timestamp, counter, parent/root và chỉ giữ provider/contract/coverage provenance có bound; terminal `ERROR/CANCELLED` trở thành typed `CrawlerFailure` thay vì empty success.
- Tieba root comments đã nối vào manual comment API và Mongo hierarchy. API kiểm registry-selected provider cùng runtime rollout `cbce_tieba/list_comments`; feature/browser/profile chưa ready trả 503 trước khi mở worker hoặc ghi dữ liệu.
- Tieba child count làm kết quả `truncated=true` vì provider hiện chỉ có stable root identity. Child comment tiếp tục planned; không dùng root ID, index hay hash body để tạo ID giả.
- `CbceBilibiliConnector` có internal root/child scan result sẵn cho persistence, giữ budget/cursor tách biệt và truyền typed auth error. Manifest/API vẫn không bật Bilibili comments do logged-in canary và responsive shadow overlap chưa đạt.
- Focused connector/API gates đạt `25 passed`; full backend regression ngoài subtitle/Gemini/text đạt `591 passed, 1 skipped`. Không chỉnh sửa chức năng subtitle.

### 2026-08-13 — Phase 10 Bluesky public reply tree

- `bluesky_public/list_comments` chuyển sang `implemented/partial/manual_only` sau khi nối đủ manifest binding, connector, public AppView adapter, shared manual API và Mongo persistence. Không dùng browser, cookie, private PDS hay endpoint nội bộ.
- Canonical post URL được parse fail-closed. Handle chỉ được đổi sang DID qua `com.atproto.identity.resolveHandle`; cây phản hồi lấy qua `app.bsky.feed.getPostThread` với `parentHeight=0` và depth hữu hạn theo request.
- Root, descendants-per-root, total, depth và từng physical HTTP attempt có budget độc lập. Missing/blocked node, declared count lớn hơn node render, repeated ID hoặc nhánh vượt budget đều tạo `truncated=true`; provider không giả lập top/new sort mà giữ `provider_defined`.
- Root response phải khớp AT URI được yêu cầu; từng reply phải khai đúng parent/root relation. API còn so resolved content identity với stored item kể cả cây rỗng, nên handle bị tái sử dụng không thể ghi reply sang bài cũ.
- DID/AT URI chỉ tồn tại trong adapter để kiểm identity/hierarchy. Content/comment/parent/root được băm ổn định; author DID đi qua HMAC rồi bị loại cùng profile blob/response. Provenance chỉ giữ provider, contract, coverage và ordering có bound.
- Focused Bluesky/API/storage gate đạt `52 passed`; full backend regression ngoài subtitle/Gemini/text đạt `609 passed, 1 skipped`. Frontend lint, `28/28` tests và production build đều đạt. Không chỉnh sửa chức năng subtitle.

### 2026-08-13 — Phase 10 Mastodon public status context

- `mastodon_public/list_comments` chuyển sang `implemented/partial/manual_only`. Operation chỉ nhận status URL trên exact `MASTODON_INSTANCES`; remote origin ngoài allowlist trả lỗi thay vì tự mở arbitrary host hoặc fallback browser.
- Adapter gọi đúng hai public surface: status read để xác minh local/root/canonical identity, rồi status context để lấy descendants. Physical retry attempt được tính vào request budget; không có cursor giả vì Context entity không cung cấp descendant pagination.
- Context được dựng lại parent-first từ local `in_reply_to_id`, nhưng persisted comment/content/parent/root ID tiếp tục là digest của canonical public status URI. Duplicate, self-reference, root repetition hoặc root identity mismatch fail closed; orphan/budget/depth/count gap tạo `truncated=true`.
- Root, descendants-per-root, total, depth và request limit độc lập. Ordering được khai `provider_defined`; actor URI chỉ làm HMAC input trong bộ nhớ, còn account object/local ID/full response không đi vào comment provenance.
- Focused provider/API/registry gate đạt `54 passed` sau hardening self-reference. Full backend regression ngoài subtitle/Gemini/text đạt `623 passed, 1 skipped`; frontend lint, `28/28` tests và production build đều đạt. Không chỉnh sửa chức năng subtitle.

### 2026-08-13 — Phase 10 typed parser-drift alerts

- `RunManager` xử lý `CrawlerFailure` trước generic exception và persist taxonomy an toàn: provider, operation, error code, retryable/retry-after cùng safe message. `details`, response body và provider payload không được ghi vào source run/event.
- `PARSE_CHANGED` có phase terminal riêng `parser_drift`, phát `parser-drift-alert` SSE rồi phát terminal source-run event. Frontend type nhận event, refresh snapshot, hiển thị label “Cấu trúc nguồn đã thay đổi” cùng typed error code.
- Regression tạo failure chứa sentinel raw payload trong `details` và chứng minh sentinel không xuất hiện trong Mongo source run hay event stream. Provenance nằm tại `docs/crawler-provenance/RUNTIME_OBSERVABILITY.md`.
- External notification destination/canary schedule vẫn là deployment-policy gate; implementation không tự gửi dữ liệu ra email/chat hoặc tự disable provider khi chưa có policy.
- Full backend regression ngoài subtitle/Gemini/text đạt `624 passed, 1 skipped`; frontend lint, `28/28` tests và production build đều đạt.

### 2026-08-13 — Phase 10 aggregate-only live canary runner

- Thêm `CrawlerCanaryRunner` và CLI `backend/scripts/crawler_canary.py`. CLI bắt buộc `--live`, nhận query/target bằng JSON stdin thay vì argv, chỉ chạy operation đã `implemented` và có binding thật; provider `legacy_only` cần opt-in lần hai `allow_legacy=true`.
- Canary cap cứng `max_items<=5`, `max_requests<=10`, `deadline_seconds<=300`, truyền budget vào `SearchQuery` và outer timeout đóng iterator khi đạt cap/deadline. Budget override đã được nối vào X, TikTok Display, Facebook Page, Instagram hashtag, YouTube, Steam, Bluesky, Reddit, Mastodon, feed/channel và worker CBCE.
- Report `cbce.live-canary.v1` không import Mongo/không tạo batch/checkpoint và luôn ghi `persisted=false`. Chỉ có count/unique count, completeness, metric-key, time bounds, warning/error code và SHA-256 input digest; không có query, URL, title/body/author, external ID, raw payload hoặc provider diagnostic.
- Typed parser/rate/auth/access failure dừng canary; parser `details` và unexpected exception text không ra report. `--output` chỉ cho filename `.json` trực tiếp dưới `data/cbce-canary-reports/` và ghi atomic.
- Provenance/vận hành nằm tại `docs/crawler-provenance/LIVE_CANARY.md`. Unit gate chứng minh cap item/deadline, typed parser drift redaction, legacy double opt-in, request schema allowlist, budget tổng YouTube/Steam và CLI không `--live` không chạy mạng.
- OPS-001 implementation đã hoàn tất; các live result thật vẫn là gate theo provider/test asset/quyền được duyệt, không được đánh đồng với test pass hoặc tự động nâng operation planned thành ready.

### 2026-08-13 — Phase 11 read-only cutover audit

- Thêm `CrawlerCutoverAuditor` và CLI `backend/scripts/crawler_cutover_audit.py`, schema `cbce.cutover-audit.v1`. Audit duyệt đúng 17 source, chọn minimum committed operation (X dùng official search thay vì embed), kiểm non-legacy implementation/handler, provider đang resolve, hai canary pass cách nhau tối thiểu một giờ và provenance file.
- Chỉ nhận canary `cbce.live-canary.v1` aggregate-safe: `persisted=false`, input digest hợp lệ và không có key query/target/content/external ID/raw payload/cookie/token. File hỏng, unsafe hoặc non-pass bị bỏ, không thể trở thành bằng chứng cutover.
- Audit liệt kê riêng `.gitmodules`, vendor tree, bridge scripts và readiness marker. Nó luôn ghi `destructive_actions_performed=false`; không đổi flag, không xóa submodule/profile/data và chỉ trả exit `0` khi cả cutover gate lẫn cleanup đã thật sự hoàn tất.
- Gate hiện tại trả đúng `17` source, `cutover_ready=false`, `cleanup_complete=false`. Blocker được chứng minh thay vì suy đoán: XHS vẫn active legacy, Weibo chưa có non-legacy implementation binding, toàn bộ provider chưa đủ hai aggregate canary tương ứng; legacy artifacts vẫn còn nên chưa được phép chạy checklist xóa.
- Tài liệu vận hành nằm tại `docs/crawler-provenance/CUTOVER_AUDIT.md`. Focused tests đạt `10 passed`; backend regression ngoài subtitle/Gemini/text sau slice audit đạt `634 passed, 1 skipped`. Không chỉnh sửa chức năng subtitle.

### 2026-08-13 — Phase 7/11 reviewed DOM contract runtime

- Thêm artifact schema/loader `cbce.observed-dom-search.v1` cho XHS, Douyin, Kuaishou, Weibo và Zhihu. Project không bundle selector: file ngoài source control phải đúng source/provider/mode, có observed time timezone-aware, SHA-256 evidence digest, reviewer, HTTPS official search template, login-detection selectors và selector schema allowlist.
- Loader cap 64 KiB, từ chối field lạ, metric lạ, host/userinfo/port/fragment lạ, future timestamp, symlink/path escape và JSON hỏng. Login selector chỉ phát hiện/pause để người dùng thao tác; không có challenge solver, signing hay payload constant từ MediaCrawler.
- Năm search operation đã có handler thật `cbce_<source>`, connector health, provider override, isolated worker, profile v2, budget/checkpoint/manual-auth events và cleanup chung. Contract root truyền như path reference; worker tự reload/validate artifact cố định theo source. Thiếu artifact trả `setup_required`, không fallback vendor trong worker.
- Shared browser adapter hỗ trợ nhiều content kind và identity builder; Zhihu namespace ID thành `answer:<id>`, `article:<id>`, `video:<id>` để tránh collision. Weibo dùng normalizer riêng nhưng cùng worker executor.
- Manifest chuyển riêng search của năm provider từ planned sang `implemented/partial`; legacy vẫn default rollback. Audit cutover hiện xác nhận cả năm có non-legacy candidate nhưng active provider vẫn `legacy_bridge` và thiếu hai canary, đúng với gate thật.
- Tài liệu review nằm tại `docs/crawler-provenance/OBSERVED_DOM_CONTRACTS.md`. Focused runtime/registry tests đạt `72 passed, 1 skipped`; không chỉnh sửa chức năng subtitle.

### 2026-08-13 — Phase 11 implementation/canary closure

- `cbce_bilibili/search` đã chuyển từ planned sang implemented và chỉ được chọn bằng explicit provider override; bridge vẫn là rollback mặc định. Audit hiện có non-legacy candidate thực thi cho đủ 17 source.
- Cutover auditor đã sửa hai lỗi làm mất bằng chứng: đọc đúng `CONTENT_BOT_DATA_DIR/cbce-canary-reports` và cho phép các bộ đếm số `observation.completeness.{title,body,author}` mà vẫn cấm nội dung/raw/token ở mọi vị trí khác. Auditor cũng resolve active provider từ cấu hình override thực tế.
- Canary aggregate-only đầu tiên đã pass cho YouTube, Game news, Steam, Bluesky, Mastodon, Bilibili CBCE và Tieba CBCE; mỗi run tối đa 2 item, 3 request, không persist nội dung. X trả typed `PAYMENT_OR_ACCESS_REQUIRED`; Reddit/TikTok/Meta chưa cấu hình. XHS và Douyin probe dừng đúng ở `AUTH_TIMEOUT`, không bypass đăng nhập/challenge.
- Bluesky deployment edge trả 403 ở cached public AppView; transport hiện chỉ failover một lần sang exact direct AppView hostname chính thức, tính vào request budget. Regression và live canary `2/2` đã pass sau sửa.
- Thêm review publisher cho năm external DOM contract: observation schema nay có UTC `observed_at`; publisher kiểm exact value-free fields/count/signature/host, gắn SHA-256 evidence digest + reviewer, validate bằng runtime loader trong thư mục cô lập rồi atomic replace. Artifact lỗi không thể ghi đè contract hợp lệ; workflow login → probe → review được ghi bằng lệnh PowerShell cụ thể.
- Thêm manual profile-login API/UI cho đủ bảy browser source: mỗi card có thể mở profile CBCE v2 độc lập, poll `opening/waiting_for_user/completed/failed`, hủy phiên và đóng owned browser khi API shutdown. Response không chứa cookie/profile path/browser diagnostic; browser preflight/profile lock vẫn fail closed. Bootstrap đăng nhập được phép trước feature flag, nhưng scan vẫn bắt buộc flag + explicit provider override.
- Backend crawler regression cuối sau wiring Bilibili, auditor, Bluesky failover, DOM review publisher và profile-login UI đạt `666 passed, 2 skipped`; Ruff sạch. Frontend lint, `28` test và production build đã pass; không chỉnh sửa chức năng subtitle trong lát cắt này.

### 2026-08-13 — Phase 11 X operation closure

- Official `x_api` hiện có handler thực thi manual-only cho `search`, `scan_channel`, `fetch_detail`, `list_creator` và `list_comments`. Không có browser scraping, posting/interaction hay fallback MediaCrawler cho X.
- Detail và creator dùng chung canonical numeric Post ID, HMAC author policy, typed official-API errors và bounded runtime. Conversation replies dùng recent-search conversation surface, giữ parent/root relation, sắp xếp parent-first trước persistence và đánh dấu truncated khi còn cursor hoặc có orphan không thể xác minh.
- Shared comment API/Mongo chấp nhận stored X post làm root anchor cho direct replies nhưng vẫn từ chối duplicate, self-reference, cross-content và child xuất hiện trước parent. Standalone media metadata tiếp tục `planned`, không quảng cáo handler giả.
- Focused X/registry/comment gate đạt `49 passed`; toàn bộ backend crawler regression ngoài subtitle/Gemini/text đạt `669 passed, 2 skipped`. Ruff sạch trên lát cắt X. Canary X của deployment hiện trả typed `PAYMENT_OR_ACCESS_REQUIRED`; operation chỉ được coi implemented-internally, chưa đạt live/cutover gate.

Phase hiện tại là **Phase 11, giai đoạn thứ 12/12 và cũng là phase cuối**. Phần cutover còn lại chỉ được thực hiện sau khi từng provider đạt đủ quyền/login và hai canary aggregate-only cách nhau tối thiểu một giờ. Cho đến lúc đó MediaCrawler bridge/vendor vẫn là rollback có kiểm soát và không được xóa sớm.

### 2026-08-13 — Phase 11 Bilibili comment-tree closure

- `cbce_bilibili/list_comments` chuyển sang `implemented/partial/manual_only`; registry chọn đúng clean-room provider và không dùng legacy bridge cho operation này.
- Shared manual comment API nay nhận Bilibili. Root và child DOM worker giữ budget riêng nhưng API phân phối chung `max_total_comments` và `max_requests`, phát root trước child, kiểm provider/content/parent/root identity rồi upsert idempotent vào Mongo.
- Child chỉ được gọi cho root có declared child count, bị cap theo `max_children_per_root`; hết item/request budget hoặc còn cursor thì trả `truncated=true`. Hoàn tất đúng declared stable children không còn bị đánh truncated chỉ vì root từng báo có child.
- CBCE feature/browser/profile rollout vẫn fail closed trước khi mở worker; chưa đăng nhập trả typed auth và không ghi dữ liệu. Focused manifest/worker/API/storage gate đạt `39 passed`; không sửa chức năng phụ đề.

### 2026-08-13 — Phase 11 clean-room similarity gate

- Thêm audit chỉ đọc so sánh crawler implementation với snapshot vendor bằng fingerprint cửa sổ 20 token và hash dòng dài; common expressions được loại theo tần suất. Report chỉ có path/count/digest/threshold/verdict, tuyệt đối không xuất source fragment hay dữ liệu người dùng.
- Report được ghi atomic dưới `CONTENT_BOT_DATA_DIR/cbce-audits`; cutover auditor yêu cầu schema/pass/no-fragment/zero-suspicious, vendor digest và project-tree digest hiện tại. Sửa crawler làm report stale; xóa vendor sau reviewed cutover vẫn giữ được attestation digest đã tạo trước đó.
- Gate hiện tại quét `117` file dự án và `211` file vendor, xét `20` candidate pair, tìm `0` suspicious pair; `cleanroom_audit.ready=true`. Đây là heuristic regression gate đi kèm manual provenance/license review, không được mô tả như ý kiến pháp lý.
- Unit/cutover audit gate đạt `9 passed`; tài liệu vận hành tại `docs/crawler-provenance/CLEANROOM_AUDIT.md`. Không sửa chức năng phụ đề.

### 2026-08-13 — Phase 11 retained rollback drill

- Thêm drill chỉ đọc chứng minh đủ bảy legacy search binding, ba artifact bridge/vendor, profile CBCE v2 tách legacy profile và cấu hình rollback rõ ràng (`CBCE_ENABLED=false`, overrides rỗng). Drill không mở browser/database, không sửa content/profile và chỉ giữ digest cấu hình.
- Report atomic được ràng buộc với cùng project-tree digest của clean-room audit. Cutover auditor yêu cầu cả similarity và rollback report còn hiệu lực; thay đổi crawler làm cả hai stale cho tới khi rerun trước lúc xóa vendor.
- Current retained drill pass: `7/7` legacy binding, artifact đầy đủ, namespace tách biệt, `content_data_mutation_required=false`, `destructive_actions_performed=false`. Focused rollback/cutover/similarity gate đạt `11 passed`; runbook tại `docs/crawler-provenance/ROLLBACK_DRILL.md`.
- Cutover report nay trả chính xác `next_canary_eligible_at` và `canary_wait_seconds` theo từng source có một evidence; canary chạy sớm không được làm tròn thành đủ một giờ. Đây là thông tin vận hành, không tự lên lịch hoặc tự mở network/browser.
- CBCE rollout cho detail/creator/comments nay bắt buộc cả source search provider và chính operation đó được override rõ ràng. Không còn trường hợp bật feature flag là action phụ tự thành ready trong khi connector vẫn là legacy; catalog/API trả `PROVIDER_NOT_SELECTED` hoặc `SOURCE_PROVIDER_NOT_SELECTED` và không mở worker.
- Regression cuối sau Bilibili tree budget, similarity/rollback audit và per-operation rollout đạt `676 passed, 2 skipped` cho toàn bộ backend crawler ngoài subtitle/Gemini/text. Frontend ESLint, `28/28` test và production build đều pass; không thay đổi chức năng phụ đề.
- Canary evidence được dedupe theo `canary_id`; copy report sang filename khác không tạo chu kỳ thứ hai. Timestamp vượt quá clock tolerance năm phút bị loại, nên không thể đặt giờ tương lai để vượt gate separation sớm.

### 2026-08-13 — Phase 11 first real source cutovers

- Canary lần hai pass, aggregate-only và `persisted=false`: YouTube `2/2`, Game news `2/2`, Steam `2/2`, Bluesky `2/2`, Mastodon `1/1`, Bilibili CBCE `2/2`, Tieba CBCE `2/2`. Audit xác nhận span `>=3600s` cho cả bảy.
- Năm public/API source đầu đã `ready=true` trên provider vốn active. Bilibili và Tieba được chuyển cấu hình search sang `cbce_bilibili`/`cbce_tieba`; fresh process dựng đúng hai CBCE connector và cutover audit trả `ready=true`, span `3663s`/`3666s`.
- Fresh application import sau cutover phát hiện manifest binding Bilibili `list_comments` thiếu iterator method; connector đã được bổ sung wrapper bounded/cursor-aware thay vì rollback cấu hình. Fresh-start focused gate đạt `21 passed`; full backend crawler suite với `.env` cutover thật đạt `677 passed, 2 skipped`.
- Cutover audit nay giữ latest typed canary state/error/time cho đúng candidate operation nhưng không giữ safe-message/provider diagnostic. X hiện được phân loại rõ `failed/PAYMENT_OR_ACCESS_REQUIRED`; Reddit là chưa có evidence, thay vì cả hai chỉ hiện chung “missing canary”.

### 2026-08-13 — Browser login-to-observation acceleration

- Manual profile session sau khi người dùng đóng tab chuyển sang `verifying`, tự mở lại đúng profile v2 và chạy value-free DOM probe. Observation atomic nằm ngoài source control tại `data/cbce-dom-observations/<source>-latest.json`; API/UI chỉ nhận boolean + SHA-256 digest, không path/URL/text/cookie.
- Typed `AUTH_REQUIRED`/`CHALLENGE_REQUIRED` làm session failed thay vì lưu shell logged-out. Cancel trong login hoặc verifying đều đóng owned resources. Review selector/contract vẫn là bước độc lập; hệ thống không tự suy selector từ class names và không tham khảo MediaCrawler.
- Focused login/probe/review gate đạt `22 passed`; frontend ESLint, `28/28` test và production build pass. Workflow cập nhật tại `docs/crawler-provenance/OBSERVED_DOM_CONTRACTS.md`; không sửa chức năng phụ đề.
- Full backend crawler regression với Bilibili/Tieba CBCE active sau lát cắt này đạt `679 passed, 2 skipped`.
- Comment operation Bilibili/Tieba không tự được bật theo search; vẫn cần override operation riêng và login/profile gate. Legacy artifacts chưa xóa, chỉ giữ làm rollback window; năm browser source XHS/Douyin/Kuaishou/Weibo/Zhihu vẫn dùng legacy cho đến khi authenticated contracts/canaries đạt.

### 2026-08-13 — X paid-access UX guard

- X saved-channel scan tiếp tục dùng official `x_api`; không thêm DOM/browser fallback để né billing/access tier. Public timeline embed và nút mở kênh vẫn độc lập với API scan.
- Sau typed failure `PAYMENT_OR_ACCESS_REQUIRED`, bulk scan tự loại kênh X đó để không lặp lại request chắc chắn thất bại. Card hiển thị hướng dẫn tiếng Việt và đổi action thành `Thử lại X API`, chỉ dành cho lần thử thủ công sau khi quyền/credits đã được cấp.
- Frontend ESLint, `28/28` tests và production build đều pass; không sửa chức năng phụ đề.

### 2026-08-13 — Chọn nhiều kênh trước khi quét

- Tab **Kênh theo dõi** nay có checkbox trên từng kênh, nút **Chọn tất cả/Bỏ chọn tất cả**, bộ đếm kênh có thể quét và nút **Quét kênh đã chọn**.
- Lựa chọn được gửi qua `channel_ids` hiện hữu; backend không đổi schema và một lần chạy có thể nhận nhiều kênh cùng lúc. Kênh `embed_only`, `manual` hoặc `setup_required` bị loại khỏi chọn tự động.
- Nút **Quét chủ đề** cũ vẫn giữ hành vi quét toàn bộ kênh đang bật; chọn nhiều kênh áp dụng cho thao tác riêng trong tab Kênh theo dõi.
- Frontend ESLint, `28/28` tests và production build đều pass; không sửa chức năng phụ đề.

Cách này tạo một nền móng dùng được cho đủ 17 nguồn và cho phép gỡ MediaCrawler sạch sẽ, thay vì thay một dependency lớn bằng nhiều bản sao khó bảo trì hoặc chỉ thêm card không có backend thật.
