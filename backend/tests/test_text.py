from app.services.text import (
    clean_terms,
    contains_term,
    content_insights,
    detect_language,
    insights_match,
    normalized,
    relevance,
)


def test_latin_terms_use_word_and_phrase_boundaries() -> None:
    assert contains_term(normalized("NTE gameplay reveal"), normalized("NTE"))
    assert not contains_term(normalized("content update"), normalized("NTE"))
    assert contains_term(normalized("Hades II patch"), normalized("Hades II"))
    assert not contains_term(normalized("Hades III rumor"), normalized("Hades II"))


def test_matching_is_case_and_diacritic_insensitive() -> None:
    score, reasons = relevance(
        "Đánh giá HADES II",
        "",
        [],
        "",
        ["đánh giá", "Hades II"],
        [],
    )
    assert score == 80
    assert reasons == ["title: đánh giá", "title: Hades II"]


def test_cjk_terms_can_match_inside_unsegmented_text() -> None:
    assert contains_term(normalized("黑神话悟空实机演示"), normalized("黑神话悟空"))


def test_exclusion_wins_but_does_not_leak_as_substring() -> None:
    accepted, _ = relevance("NTE content update", "", [], "", ["NTE"], ["rent"])
    excluded, reasons = relevance("NTE giveaway", "", [], "", ["NTE"], ["giveaway"])
    assert accepted == 40
    assert excluded == 0
    assert reasons == ["excluded by: giveaway"]


def test_clean_terms_deduplicates_normalized_aliases() -> None:
    assert clean_terms([" Hades II ", "hades ii", "HADES  II", "Hades 2"]) == ["Hades II", "Hades 2"]


def test_game_insights_are_multilingual_and_explainable() -> None:
    insight = content_insights(
        "Bản cập nhật rất mượt nhưng vẫn bị crash",
        "FPS tốt hơn, đôi lúc văng game",
        ["Hades II"],
    )
    assert insight["language"]["code"] == "vi"
    assert insight["sentiment"]["label"] == "mixed"
    assert "positive: mượt" in insight["sentiment"]["reasons"]
    assert "negative: crash" in insight["sentiment"]["reasons"]
    topic_ids = {topic["id"] for topic in insight["topics"]}
    assert {"updates", "performance", "bugs"}.issubset(topic_ids)


def test_language_detection_uses_script_before_locale_hint() -> None:
    assert detect_language("黑神话悟空战斗很流畅", "en-US")["code"] == "zh"
    assert detect_language("새로운 전투 업데이트", None)["code"] == "ko"


def test_unknown_sentiment_does_not_invent_an_opinion() -> None:
    insight = content_insights("Hades II developer interview", "Release date discussion", [], "en")
    assert insight["sentiment"] == {"label": "neutral", "score": 0.0, "reasons": []}


def test_sentiment_negation_does_not_double_count_nested_term() -> None:
    english = content_insights("The game is not bad", "", [], "en")
    vietnamese = content_insights("Game không tốt", "", [], "vi")
    assert english["sentiment"] == {
        "label": "positive",
        "score": 1.0,
        "reasons": ["positive: not bad"],
    }
    assert vietnamese["sentiment"] == {
        "label": "negative",
        "score": -1.0,
        "reasons": ["negative: không tốt"],
    }


def test_insight_filters_can_be_combined() -> None:
    insight = content_insights("Bản vá crash và lag", "", [], "vi")
    assert insights_match(insight, language="vi", sentiment="negative", topic="bugs")
    assert insights_match(insight, topic="performance")
    assert not insights_match(insight, language="en")
    assert not insights_match(insight, sentiment="positive")
    assert not insights_match(insight, topic="story")
