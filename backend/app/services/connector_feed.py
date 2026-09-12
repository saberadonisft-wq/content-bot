from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from xml.etree import ElementTree

from ..crawlers.runtime import (
    CrawlerErrorCode,
    CrawlerFailure,
)
from .connector_contracts import (
    ConnectorCapabilities,
    ConnectorStatus,
    RawContentItem,
    SearchQuery,
    SourceConnector,
)
from .connector_support import (
    allocate_limits,
    logger,
    parse_feed_datetime,
    plain_text,
    pooled_client,
)
from .feed_ingestion import (
    fetch_feed_document,
    parse_feed_templates,
    safe_entry_url,
)


class FeedConnector(SourceConnector):
    """Small allowlisted RSS/Atom connector for public game sites and Steam feeds."""

    def __init__(self, source_id: str, label: str, detail: str, url_templates: str):
        self.source_id = source_id
        self.label = label
        self.group = "Public web"
        self.detail = detail
        self.configuration_error: str | None = None
        try:
            self.feed_templates = parse_feed_templates(url_templates)
        except ValueError as exc:
            self.feed_templates = ()
            self.configuration_error = str(exc)
        self.url_templates = [template.value for template in self.feed_templates]
        self.capabilities = ConnectorCapabilities(True, watchlist_filter=True)

    async def healthcheck(self) -> ConnectorStatus:
        if self.configuration_error:
            return ConnectorStatus(
                "not_configured",
                self.configuration_error,
                "INVALID_FEED_CONFIGURATION",
            )
        if not self.feed_templates:
            return ConnectorStatus("not_configured", self.detail)
        return ConnectorStatus(
            "ready",
            f"{len(self.feed_templates)} validated public RSS/Atom feed template(s) configured.",
        )

    def checkpoint_fingerprint_fields(
        self,
        operation: str,
        channel: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del operation, channel
        return {
            "provider_contract": "rss-atom-v2",
            "feed_templates": [template.digest for template in self.feed_templates],
        }

    async def search(
        self, query: SearchQuery, checkpoint: dict[str, Any] | None = None
    ) -> AsyncIterator[RawContentItem]:
        if checkpoint and not query.legacy_checkpoint:
            query.legacy_checkpoint = dict(checkpoint)
        if not self.feed_templates:
            return
        seen: set[str] = set()
        successful_documents = 0
        failures: list[CrawlerFailure] = []
        allowed_hosts = frozenset(template.host for template in self.feed_templates)
        async with pooled_client(
            timeout=30,
            follow_redirects=False,
            headers={"User-Agent": "ContentBot/0.1 (local research tool)"},
        ) as client:
            term_limits = allocate_limits(query.max_items, len(query.search_terms))
            for search_term, term_limit in zip(
                query.search_terms, term_limits, strict=True
            ):
                term_yielded = 0
                if term_limit == 0:
                    continue
                for template in self.feed_templates:
                    validators = query.resume_cursor(
                        "feed",
                        feed=template.digest,
                        term=search_term,
                        default={},
                    )
                    try:
                        document = await fetch_feed_document(
                            client,
                            template.render(search_term),
                            allowed_hosts=allowed_hosts,
                            validators=(
                                validators if isinstance(validators, dict) else {}
                            ),
                        )
                        if document.not_modified:
                            successful_documents += 1
                            continue
                        root = ElementTree.fromstring(document.content)
                        nodes = self._feed_nodes(root)
                    except (CrawlerFailure, ElementTree.ParseError, ValueError) as exc:
                        failure = (
                            exc
                            if isinstance(exc, CrawlerFailure)
                            else CrawlerFailure(
                                CrawlerErrorCode.PARSE_CHANGED,
                                "Feed document is not valid RSS/Atom XML.",
                            )
                        )
                        failures.append(failure)
                        logger.warning(
                            "Skipping invalid feed document: feed=%s code=%s",
                            template.digest[:12],
                            failure.code.value,
                        )
                        if query.warning_callback:
                            await query.warning_callback(
                                failure.code.value,
                                failure.safe_message,
                            )
                        continue
                    successful_documents += 1
                    remaining_before = term_limit - term_yielded
                    for node in nodes:
                        if term_yielded >= term_limit:
                            break
                        title = self._text(node, "title")
                        link = self._entry_link(node)
                        canonical_url = safe_entry_url(link, base_url=document.url)
                        external_id = (
                            node.findtext("guid")
                            or node.findtext("{http://www.w3.org/2005/Atom}id")
                            or canonical_url
                        )
                        if not external_id or external_id in seen:
                            continue
                        if canonical_url is None:
                            canonical_url = safe_entry_url(
                                str(external_id),
                                base_url=document.url,
                            )
                        if canonical_url is None:
                            continue
                        seen.add(external_id)
                        published = (
                            self._text(node, "pubDate")
                            or self._text(node, "published")
                            or self._text(node, "updated")
                        )
                        published_at = parse_feed_datetime(published)
                        publisher_name, publisher_url = self._publisher(
                            node, document.url
                        )
                        yield RawContentItem(
                            external_id=str(external_id),
                            canonical_url=canonical_url,
                            title=plain_text(title, 180),
                            body_snippet=plain_text(
                                self._text(node, "description")
                                or self._text(node, "summary")
                                or self._text(node, "content")
                            ),
                            author=self._author(node),
                            published_at=published_at,
                            raw_payload={
                                "provider_id": "rss_atom",
                                "feed_id": template.digest,
                                "feed_host": template.host,
                                "publisher_name": publisher_name,
                                "publisher_url": publisher_url,
                                "media": self._media_metadata(node, document.url),
                                "search_term": search_term,
                            },
                        )
                        term_yielded += 1
                    if len(nodes) <= remaining_before:
                        next_validators = {
                            key: value
                            for key, value in {
                                "etag": document.etag,
                                "last_modified": document.last_modified,
                            }.items()
                            if value
                        }
                        if next_validators:
                            query.report_cursor(
                                "feed",
                                next_validators,
                                feed=template.digest,
                                term=search_term,
                            )
                    if term_yielded >= term_limit:
                        break
        if successful_documents == 0 and failures:
            raise failures[0]

    @staticmethod
    def _text(node: ElementTree.Element, tag: str) -> str:
        direct = node.findtext(tag)
        if direct:
            return direct.strip()
        namespaced = node.findtext(f"{{http://www.w3.org/2005/Atom}}{tag}")
        return namespaced.strip() if namespaced else ""

    @staticmethod
    def _feed_nodes(root: ElementTree.Element) -> list[ElementTree.Element]:
        local_name = root.tag.rsplit("}", 1)[-1].casefold()
        if local_name not in {"rss", "feed", "rdf"}:
            raise ValueError("XML root is not RSS/Atom")
        return root.findall(".//item") + root.findall(
            ".//{http://www.w3.org/2005/Atom}entry"
        )

    @staticmethod
    def _entry_link(node: ElementTree.Element) -> str:
        direct = node.findtext("link")
        if direct:
            return direct.strip()
        links = node.findall("{http://www.w3.org/2005/Atom}link")
        preferred = next(
            (
                entry.get("href", "")
                for entry in links
                if entry.get("href") and entry.get("rel", "alternate") == "alternate"
            ),
            "",
        )
        return preferred or next(
            (entry.get("href", "") for entry in links if entry.get("href")),
            "",
        )

    @staticmethod
    def _author(node: ElementTree.Element) -> str:
        values = (
            node.findtext("author"),
            node.findtext("{http://purl.org/dc/elements/1.1/}creator"),
            node.findtext(
                "{http://www.w3.org/2005/Atom}author/{http://www.w3.org/2005/Atom}name"
            ),
        )
        return plain_text(next((value for value in values if value), ""), 180)

    @staticmethod
    def _publisher(
        node: ElementTree.Element,
        base_url: str,
    ) -> tuple[str, str | None]:
        source = node.find("source")
        if source is None:
            return "", None
        name = plain_text(source.text or "", 180)
        url = safe_entry_url(str(source.get("url") or ""), base_url=base_url)
        return name, url

    @staticmethod
    def _media_metadata(
        node: ElementTree.Element,
        base_url: str,
    ) -> list[dict[str, Any]]:
        candidates: list[tuple[str, str, str, str]] = []
        for enclosure in node.findall("enclosure"):
            candidates.append(
                (
                    "enclosure",
                    str(enclosure.get("url") or ""),
                    str(enclosure.get("type") or ""),
                    str(enclosure.get("length") or ""),
                )
            )
        for link in node.findall("{http://www.w3.org/2005/Atom}link"):
            if str(link.get("rel") or "") == "enclosure":
                candidates.append(
                    (
                        "enclosure",
                        str(link.get("href") or ""),
                        str(link.get("type") or ""),
                        str(link.get("length") or ""),
                    )
                )
        for thumbnail in node.findall("{http://search.yahoo.com/mrss/}thumbnail"):
            candidates.append(
                (
                    "thumbnail",
                    str(thumbnail.get("url") or ""),
                    "image/*",
                    "",
                )
            )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for kind, raw_url, mime_type, raw_length in candidates:
            url = safe_entry_url(raw_url, base_url=base_url)
            if not url or url in seen:
                continue
            seen.add(url)
            try:
                size_bytes = int(raw_length) if raw_length else None
            except ValueError:
                size_bytes = None
            result.append(
                {
                    "kind": kind,
                    "url": url,
                    "mime_type": mime_type[:200] or None,
                    "size_bytes": (
                        size_bytes
                        if size_bytes is not None and size_bytes >= 0
                        else None
                    ),
                }
            )
            if len(result) >= 10:
                break
        return result
