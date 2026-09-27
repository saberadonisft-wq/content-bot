
import numpy as np

from app.services.caption_cleaner import (
    clean_caption_text,
    extract_hashtags,
    sanitize_filename,
    truncate_at_word_boundary,
)
from app.services.chromium_cookie_reader import (
    decrypt_cookie_value,
    get_browser_user_data_paths,
)
from app.services.speaker_pitch_diarization import (
    diarize_subtitle_cues_by_pitch,
    estimate_f0_autocorr,
)
from app.services.thumbnail_selector import score_video_frame
from app.services.vietnamese_tts_normalizer import (
    normalize_vietnamese_for_tts,
    read_integer,
)


def test_read_integer():
    assert read_integer(0) == "không"
    assert read_integer(5) == "năm"
    assert read_integer(15) == "mười lăm"
    assert read_integer(21) == "hai mươi mốt"
    assert read_integer(24) == "hai mươi tư"
    assert read_integer(105) == "một trăm lẻ năm"
    assert read_integer(1000) == "một nghìn"
    assert read_integer(1000000) == "một triệu"
    assert read_integer(1000000000) == "một tỷ"


def test_normalize_vietnamese_for_tts():
    # Dates
    text_date = "Cuộc họp diễn ra ngày 15/09/2026 tại Hà Nội."
    norm_date = normalize_vietnamese_for_tts(text_date)
    assert "ngày mười lăm tháng chín năm hai nghìn không trăm hai mươi sáu" in norm_date

    # Currencies and thousand dots
    text_money = "Giá sản phẩm là 150.000 VNĐ hoặc $20."
    norm_money = normalize_vietnamese_for_tts(text_money)
    assert "một trăm năm mươi nghìn đồng" in norm_money
    assert "hai mươi đô la" in norm_money

    # Times
    text_time = "Bắt đầu lúc 14:30."
    norm_time = normalize_vietnamese_for_tts(text_time)
    assert "mười bốn giờ ba mươi phút" in norm_time

    # Roman numerals
    text_roman = "Khám phá thế kỷ XXI."
    norm_roman = normalize_vietnamese_for_tts(text_roman)
    assert "thế kỷ hai mươi mốt" in norm_roman

    # Decimals and tech terms
    text_tech = "Phiên bản AI video 3,5 mới."
    norm_tech = normalize_vietnamese_for_tts(text_tech)
    assert "ba phẩy năm" in norm_tech
    assert "ây ai" in norm_tech
    assert "vi-đê-ô" in norm_tech


def test_caption_cleaner():
    raw_caption = (
        "Video hài hước cực đỉnh! #funny #douyin[话题]# "
        "Liên hệ hợp tác VX: haianh8899 hoặc alo 13812345678. "
        "Cảm ơn @user_test_999 đã theo dõi!"
    )
    cleaned = clean_caption_text(raw_caption)
    assert "VX:" not in cleaned
    assert "13812345678" not in cleaned
    assert "@user_test_999" not in cleaned
    assert "#funny" in cleaned
    assert "#douyin" in cleaned

    tags = extract_hashtags(raw_caption)
    assert "funny" in tags
    assert "douyin" in tags

    # Truncate
    truncated = truncate_at_word_boundary("Đây là một tiêu đề rất dài cần phải cắt bớt cho vừa vặn", 25)
    assert len(truncated) <= 25
    assert not truncated.endswith(" ")

    # Safe filename
    filename = sanitize_filename("Phim: Cuộc Chiến Siêu Nhiên? [Tập 01] <Full HD>", 40)
    assert ":" not in filename
    assert "?" not in filename
    assert "<" not in filename
    assert ">" not in filename


def test_chromium_cookie_reader_safe():
    paths = get_browser_user_data_paths()
    assert isinstance(paths, dict)

    empty_val = decrypt_cookie_value(b"", b"dummy_key_32_bytes_long_1234567")
    assert empty_val == ""


def test_score_video_frame():
    # Synthesize clean 720p color frame
    frame = np.full((360, 640, 3), 128, dtype=np.uint8)
    frame[50:150, 50:150] = [255, 0, 0]  # Blue square for contrast
    score, metrics = score_video_frame(frame)

    assert score > 0
    assert "sharpness" in metrics
    assert "colorfulness" in metrics
    assert "brightness" in metrics


def test_estimate_f0_pitch_synthetic():
    sr = 16000
    t = np.linspace(0, 0.5, int(sr * 0.5), endpoint=False)

    # 1. Male pitch: 120 Hz sine wave
    male_wave = (0.6 * np.sin(2 * np.pi * 120.0 * t)).astype(np.float32)
    f0_male = estimate_f0_autocorr(male_wave, sample_rate=sr)
    assert f0_male is not None
    assert 110.0 <= f0_male <= 130.0

    # 2. Female pitch: 220 Hz sine wave
    female_wave = (0.6 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    f0_female = estimate_f0_autocorr(female_wave, sample_rate=sr)
    assert f0_female is not None
    assert 210.0 <= f0_female <= 230.0


def test_diarize_subtitle_cues_fallback(tmp_path):
    fake_audio = tmp_path / "missing.wav"
    cues = [
        {"id": "1", "start_ms": 0, "end_ms": 1000, "text": "Câu 1"},
        {"id": "2", "start_ms": 1500, "end_ms": 2500, "text": "Câu 2"},
    ]
    diarized = diarize_subtitle_cues_by_pitch(fake_audio, cues)
    assert len(diarized) == 2
    assert "speaker_gender" in diarized[0]
