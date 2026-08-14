# Reviewed DOM search contracts

## Why contracts are external artifacts

Xiaohongshu, Douyin, Kuaishou, Weibo and Zhihu now have a complete clean-room
search execution path, but Content Bot deliberately ships no selectors for
them. Logged-in layouts must be observed independently in an application-owned
profile and reviewed before the operation can become available. This prevents
MediaCrawler selectors, signing logic or payload constants from entering the
new implementation.

Contracts live outside source control by default under
`data/crawler-contracts-v1/<source_id>.search.json`, configurable with
`CONTENT_BOT_CBCE_CONTRACT_ROOT`. They are not credentials and must never embed
cookies, tokens, response bodies or captured user content.

## Schema

Every artifact uses `cbce.observed-dom-search.v1` and contains exactly:

- `source_id` and matching `provider_id=cbce_<source>`;
- `mode`: `browser_video_v1` for XHS/Douyin/Kuaishou/Zhihu or
  `weibo_post_v1` for Weibo;
- timezone-aware `observed_at`;
- SHA-256 `evidence_digest` of the separately retained value-free observation;
- bounded `reviewed_by` identifier;
- one HTTPS `search_url_template` containing exactly one `{query}` and
  optionally one `{page}` placeholder;
- `login_selectors`, used only to detect that the visible browser is waiting for
  the user—never to solve or bypass a challenge;
- a strict `selectors` object for the selected mode.

Unknown top-level or selector fields are rejected. The loader also rejects
files larger than 64 KiB, symlinks, future timestamps, invalid review metadata,
unknown metrics, control characters, userinfo/ports/fragments and navigation
outside the source's approved HTTPS domains. Media URLs remain restricted to a
separate built-in platform allowlist.

## Observation and review workflow

1. Enable the CBCE runtime and open only the application's v2 profile for the
   source. Do not attach a personal/default browser profile.
2. Complete login or CAPTCHA manually in the visible browser. Content Bot must
   not automate the challenge.
3. Use the value-free DOM observation tooling on an allowed test query. Record
   structure and candidate selectors without retaining post text, author IDs,
   cookies, tokens or raw HTML.
4. Derive the contract from that observation, calculate the sanitized evidence
   digest and have a second reviewer confirm source/provider/host/metrics and
   selector scope. Never consult or copy MediaCrawler selector/signing files.
5. Place the reviewed JSON artifact in the contract root. A missing or invalid
   artifact produces `setup_required`, not an empty successful scan.
6. Explicitly select the provider, for example:

   ```dotenv
   CONTENT_BOT_CBCE_ENABLED=true
   CONTENT_BOT_CBCE_PROVIDER_OVERRIDES={"xhs":{"search":"cbce_xhs"}}
   ```

7. Run the aggregate-only live canary with the matching `provider_id`, verify
   cleanup, then repeat at a separated time. The legacy provider remains the
   rollback default until all cutover gates pass.

From `backend/`, the concrete manual flow is:

```powershell
# The browser uses only data/crawler-profiles-v2/<source>/<account-hash>.
.\.venv\Scripts\python.exe -m app.crawlers.login_session --source xhs --timeout 1200

# Close the login tab after completing login, then collect value-free structure.
New-Item -ItemType Directory -Force .\data\crawler-observations | Out-Null
.\.venv\Scripts\python.exe -m app.crawlers.dom_probe --source xhs --auth-timeout 120 `
  > .\data\crawler-observations\xhs-search.json

# The draft is a reviewed JSON object containing only search_url_template,
# login_selectors and selectors derived from this project's observation.
.\.venv\Scripts\python.exe -m app.crawlers.dom_contract_review `
  --observation .\data\crawler-observations\xhs-search.json `
  --draft .\data\crawler-observations\xhs-search-draft.json `
  --reviewed-by reviewer-2
```

The same login bootstrap is also available from the **Nguồn dữ liệu** card as
**Mở profile CBCE v2**. The API returns only a bounded state, safe message,
observation-ready flag and SHA-256 digest; it never returns cookies, browser
diagnostics or the profile path. Closing the visible login tab moves the
session to `verifying`: Content Bot reopens the retained profile, runs the
value-free probe and atomically stores
`data/cbce-dom-observations/<source>-latest.json`. A typed auth/challenge result
fails the session instead of recording a false observation. **Đóng phiên đăng
nhập** cancels either login or verification and closes owned resources.

The CLI probe remains available for reproducibility. When the UI flow succeeds,
use its generated observation directly during independent review, for example:

```powershell
.\.venv\Scripts\python.exe -m app.crawlers.dom_contract_review `
  --observation .\data\cbce-dom-observations\xhs-latest.json `
  --draft .\data\crawler-observations\xhs-search-draft.json `
  --reviewed-by reviewer-2
```

Contract derivation/review remains explicit because a structural observation
alone is not evidence that any selector or coverage claim is correct.
Profile bootstrap is available before the CBCE scan feature flag is enabled;
the flag and explicit provider override are still mandatory for data scanning.

`dom_contract_review` rejects symlinks, files over 64 KiB, unknown fields,
wrong source/provider/host, future or timezone-less observations, unbounded or
opaque structural signatures and every contract that the runtime loader would
reject. It hashes the exact value-free observation, validates the complete
candidate in an isolated directory, then atomically replaces only
`<source>.search.json`. An invalid review never overwrites the last valid
contract. It does not infer selectors or satisfy the required independent
review by itself.

## Runtime boundary

The API process passes only the fixed contract-root reference to an isolated
worker. The worker reloads and validates the source-named artifact, opens a
source/account-specific v2 profile, applies exact item/request/deadline budgets,
emits manual auth events, hashes author identity and closes owned browser
resources before `complete`. Checkpoints retain only the shared opaque cursor;
contract digests appear only as non-secret provenance.

Zhihu identities are prefixed by content kind (`answer:`, `article:` or
`video:`) before persistence so numeric IDs from different entity namespaces
cannot collide.
