# Steam public reviews provider provenance

## Provider boundary

- Content Bot uses the public Steam Store search surface and public `appreviews` JSON surface without account login, cookies, API keys, browser automation, or MediaCrawler code.
- Keyword-to-App-ID discovery is explicitly partial. An exact normalized game title wins; tied or low-confidence matches fail closed with a warning so a DLC, demo, soundtrack, or similarly named game is not silently selected.
- Users can bypass discovery by saving a Store/Community `/app/<numeric-id>` or `/games/<numeric-id>` URL. It is canonicalized to `https://store.steampowered.com/app/<id>/` and becomes an explicit pinned-game channel.

## Implemented behavior

- App discovery and review pagination have separate per-run request budgets. Review filter, language, purchase type, maximum discovery count, and provider-contract version participate in checkpoint compatibility.
- Keyword and saved-app scans always fetch the newest frontier (`cursor=*`) before an older continuation. They resume backlog only after the frontier overlaps recent compound review IDs, preventing a durable cursor from hiding newly posted reviews.
- Pagination requests at most the exact remaining item budget, supports more than 100 reviews, stops on repeated/empty cursors, and preserves the next unprocessed cursor when item or request budget is exhausted.
- Stable identity is `(app_id, recommendation_id)`. Search and channel paths share one normalizer and raw allowlist.
- Reviewer account objects, Steam IDs, ownership/playtime profiles, and unexpected provider fields are discarded. The public author label is `Steam reviewer`. Raw `helpful_votes` retains Steam `votes_up` semantics while the compatibility metric remains `like_count` until schema migration.
- A per-review deep link would require retaining a reviewer account/profile identifier. Content Bot deliberately keeps the app review-list URL instead to preserve the no-identity policy.

## Remaining gates

- The public Store surfaces have no product-grade SLA. Live canaries must verify schema and rate behavior before increasing default budgets.
- Optional review filters/languages need UI controls if product users require per-topic values rather than administrator defaults.

## Public references

- Steam Store: https://store.steampowered.com/
- Steam Community: https://steamcommunity.com/
