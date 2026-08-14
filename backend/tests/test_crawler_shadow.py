import asyncio
import json
from datetime import UTC, datetime

from app.crawlers.runtime import (
    ShadowAcceptancePolicy,
    ShadowCollector,
    compare_shadow,
)
from app.services.connectors import RawContentItem


def _stream(items):
    async def generate():
        for item in items:
            yield item

    return generate()


def _item(
    external_id: str,
    *,
    url: str | None = None,
    title: str = "public title",
    author: str = "pseudonym",
    view_count: int | None = 1,
):
    metrics = {} if view_count is None else {"view_count": view_count}
    return RawContentItem(
        external_id=external_id,
        canonical_url=url or f"https://www.bilibili.com/video/{external_id}",
        title=title,
        author=author,
        published_at=datetime(2026, 8, 13, tzinfo=UTC),
        metrics=metrics,
        raw_payload={"raw_secret": "must not be retained"},
    )


def test_shadow_report_is_aggregate_only_and_measures_overlap() -> None:
    raw_id = "BV1SensitiveId"
    raw_title = "A raw title that must never be serialized"
    collector = ShadowCollector(
        b"comparison-key-32-bytes-long-value", allowed_metric_keys=("view_count",)
    )

    async def run():
        baseline = await collector.collect(
            _stream([_item(raw_id, title=raw_title), _item("BV2")]),
            max_items=10,
        )
        candidate = await collector.collect(
            _stream([_item(raw_id, title=raw_title), _item("BV3")]),
            max_items=10,
        )
        return baseline, candidate

    baseline, candidate = asyncio.run(run())
    report = compare_shadow(baseline, candidate)
    serialized = json.dumps(report.as_dict(), sort_keys=True)

    assert report.id_overlap_count == 1
    assert report.url_overlap_count == 1
    assert report.baseline_id_recall == 0.5
    assert report.candidate_id_precision == 0.5
    assert raw_id not in serialized
    assert raw_title not in serialized
    assert "raw_secret" not in serialized
    assert raw_id not in repr(baseline)


def test_shadow_collector_counts_duplicates_and_ignores_unapproved_metrics() -> None:
    collector = ShadowCollector(
        b"comparison-key-32-bytes-long-value", allowed_metric_keys=("view_count",)
    )
    snapshot = asyncio.run(
        collector.collect(
            _stream(
                [
                    _item("BV1"),
                    RawContentItem(
                        external_id="BV1",
                        canonical_url="https://www.bilibili.com/video/BV1",
                        title="",
                        metrics={"like_count": 99},
                    ),
                ]
            ),
            max_items=10,
        )
    )

    assert snapshot.item_count == 2
    assert snapshot.unique_id_count == 1
    assert snapshot.duplicate_id_count == 1
    assert snapshot.field_counts["title"] == 1
    assert snapshot.metric_counts == {"view_count": 1}


def test_shadow_acceptance_policy_returns_stable_reason_codes() -> None:
    collector = ShadowCollector(b"comparison-key-32-bytes-long-value")

    async def run():
        baseline = await collector.collect(
            _stream([_item("BV1"), _item("BV2")]), max_items=10
        )
        candidate = await collector.collect(
            _stream([_item("BV3", title="")]), max_items=10
        )
        return compare_shadow(baseline, candidate)

    report = asyncio.run(run())
    accepted, reasons = ShadowAcceptancePolicy().evaluate(report)

    assert accepted is False
    assert reasons == (
        "ID_OVERLAP_BELOW_THRESHOLD",
        "FIELD_INCOMPLETE:title",
    )


def test_shadow_collector_enforces_bounded_sample_and_key_length() -> None:
    try:
        ShadowCollector(b"too-short")
    except ValueError as exc:
        assert "at least 16 bytes" in str(exc)
    else:
        raise AssertionError("short comparison key was accepted")

    collector = ShadowCollector(b"comparison-key-32-bytes-long-value")
    snapshot = asyncio.run(
        collector.collect(_stream([_item("BV1"), _item("BV2")]), max_items=1)
    )
    assert snapshot.item_count == 1
