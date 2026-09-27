import pytest

from app.services.vietnamese_tts_normalizer import (
    normalize_vietnamese_for_tts,
    read_integer,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (10**9, "một tỷ"),
        (10**12, "một nghìn tỷ"),
        (10**15, "một triệu tỷ"),
        (10**18, "một tỷ tỷ"),
        (10**21, "một nghìn tỷ tỷ"),
        (10**12 + 5, "một nghìn tỷ không trăm lẻ năm"),
        (-(10**12), "âm một nghìn tỷ"),
    ],
)
def test_large_integer_preserves_place_value(value, expected):
    assert read_integer(value) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("3,05%", "ba phẩy không năm phần trăm"),
        ("-3,05", "âm ba phẩy không năm"),
        ("3.5 kg", "ba phẩy năm ki lô gam"),
        ("12kg", "mười hai ki lô gam"),
        ("-2,05°C", "âm hai phẩy không năm độ C"),
        ("10\nngười", "mười người"),
        ("9" * 100, "9" * 100),
        ("Giá 150.000 VNĐ.", "Giá một trăm năm mươi nghìn đồng."),
        ("$20.05", "hai mươi phẩy không năm đô la"),
        ("10 - 20%", "mười đến hai mươi phần trăm"),
        ("14h30", "mười bốn giờ ba mươi phút"),
        ("14h", "mười bốn giờ"),
        (
            "29/02/2024",
            "ngày hai mươi chín tháng hai năm hai nghìn không trăm hai mươi tư",
        ),
        ("31/02/2024", "31/02/2024"),
        ("25:70", "25:70"),
        ("RTX 3050", "RTX 3050"),
        ("phiên bản 1.2.3", "phiên bản 1.2.3"),
        ("192.168.1.1", "192.168.1.1"),
        ("https://example.test/AI/123", "https://example.test/AI/123"),
        ("ABC-123", "ABC-123"),
        ("Có 10.", "Có mười."),
        ("Mã 007", "Mã không không bảy"),
    ],
)
def test_context_preserves_meaning_and_identifiers(text, expected):
    assert normalize_vietnamese_for_tts(text) == expected
