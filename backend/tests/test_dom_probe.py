from pathlib import Path

import pytest

from app.crawlers.dom_probe import dom_probe_spec, main, validate_probe_gate
from app.crawlers.runtime import (
    CrawlerErrorCode,
    CrawlerFailure,
    DomLandmark,
    DomStructureObservation,
)


def test_dom_probe_specs_use_value_free_neutral_search_targets() -> None:
    xhs = dom_probe_spec("xhs")
    douyin = dom_probe_spec("dy")
    kuaishou = dom_probe_spec("ks")
    weibo = dom_probe_spec("weibo")
    tieba = dom_probe_spec("tieba")
    zhihu = dom_probe_spec("zhihu")

    assert xhs.provider_id == "cbce_xhs"
    assert xhs.url == "https://www.xiaohongshu.com/search_result?keyword=game"
    assert douyin.provider_id == "cbce_douyin"
    assert douyin.url == "https://www.douyin.com/search/game"
    assert kuaishou.provider_id == "cbce_kuaishou"
    assert kuaishou.url == "https://www.kuaishou.com/search/video?searchKey=game"
    assert weibo.provider_id == "cbce_weibo"
    assert weibo.url == "https://s.weibo.com/weibo?q=game"
    assert tieba.provider_id == "cbce_tieba"
    assert tieba.url == "https://tieba.baidu.com/f/search/res?ie=utf-8&qw=game"
    assert zhihu.provider_id == "cbce_zhihu"
    assert zhihu.url == "https://www.zhihu.com/search?type=content&q=game"


def test_dom_probe_rejects_sources_without_an_observed_dom_contract() -> None:
    with pytest.raises(ValueError, match="does not have"):
        dom_probe_spec("bilibili")


def test_dom_probe_cli_reports_typed_auth_failure_without_traceback(
    monkeypatch, capsys
) -> None:
    async def fail_probe(
        source_id: str,
        *,
        executable: Path,
        profile_root: Path,
        auth_timeout_seconds: float,
    ) -> dict[str, object]:
        del source_id, executable, profile_root, auth_timeout_seconds
        raise CrawlerFailure(
            CrawlerErrorCode.AUTH_TIMEOUT,
            "Timed out waiting for interactive login.",
            retryable=True,
        )

    monkeypatch.setattr("app.crawlers.dom_probe.probe_dom", fail_probe)
    assert main(["--source", "weibo", "--auth-timeout", "1"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert '"error_code": "AUTH_TIMEOUT"' in captured.err
    assert "Traceback" not in captured.err


def _observation(*, landmarks=(), sentinels=()):
    return DomStructureObservation(
        source_id="zhihu",
        provider_id="cbce_zhihu",
        operation="search",
        final_host="www.zhihu.com",
        element_count=10,
        open_shadow_root_count=0,
        landmarks=landmarks,
        attribute_names=(),
        link_path_counts=(),
        sentinel_counts=sentinels,
    )


def test_dom_probe_gate_classifies_challenge_before_auth_shell() -> None:
    spec = dom_probe_spec("zhihu")
    with pytest.raises(CrawlerFailure) as raised:
        validate_probe_gate(
            spec,
            _observation(
                landmarks=(DomLandmark("div.signflowmodal", 1),),
                sentinels=(DomLandmark("challenge_1", 1),),
            ),
        )
    assert raised.value.code is CrawlerErrorCode.CHALLENGE_REQUIRED


def test_dom_probe_gate_classifies_project_observed_auth_landmark() -> None:
    with pytest.raises(CrawlerFailure) as raised:
        validate_probe_gate(
            dom_probe_spec("xhs"),
            _observation(landmarks=(DomLandmark("button.login-btn", 1),)),
        )
    assert raised.value.code is CrawlerErrorCode.AUTH_REQUIRED
