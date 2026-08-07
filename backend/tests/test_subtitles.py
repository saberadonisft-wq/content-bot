from app.services.subtitles import parse_subtitles_text, seconds_to_srt_time, subtitles_to_srt


def test_seconds_to_srt_time():
    assert seconds_to_srt_time(0) == "00:00:00,000"
    assert seconds_to_srt_time(3.5) == "00:00:03,500"
    assert seconds_to_srt_time(65.2) == "00:01:05,200"


def test_parse_prompt_format():
    raw_text = """
    [00:00 - 00:03] Tại sao nhẫn cưới lại phải đeo ở ngón áp út này?
    [00:03 - 00:05] Bởi vì mạch máu ở ngón áp út nối thẳng đến trái tim.
    [00:06 - 00:10] Hai người sau khi kết hôn, trái tim của họ sẽ kết nối lại với nhau.
    """
    items = parse_subtitles_text(raw_text)
    assert len(items) == 3
    assert items[0]["start_seconds"] == 0.0
    assert items[0]["end_seconds"] == 3.0
    assert items[0]["text"] == "Tại sao nhẫn cưới lại phải đeo ở ngón áp út này?"
    assert items[1]["start_seconds"] == 3.0
    assert items[1]["end_seconds"] == 5.0
    assert items[2]["start_seconds"] == 6.0
    assert items[2]["end_seconds"] == 10.0


def test_parse_gemini_web_natural_format():
    raw_text = """
    Phụ đề Tiếng Việt
    00:00 - 00:03
    : Tại sao nhẫn cưới lại phải đeo ở ngón áp út này?

    00:03 - 00:05
    : Bởi vì mạch máu ở ngón áp út nối thẳng đến trái tim.

    00:06 - 00:10
    : Hai người sau khi kết hôn, trái tim của họ sẽ kết nối lại với nhau.
    """
    items = parse_subtitles_text(raw_text)
    assert len(items) == 3
    assert items[0]["start_seconds"] == 0.0
    assert items[0]["end_seconds"] == 3.0
    assert items[0]["text"] == "Tại sao nhẫn cưới lại phải đeo ở ngón áp út này?"
    assert items[1]["text"] == "Bởi vì mạch máu ở ngón áp út nối thẳng đến trái tim."


def test_parse_json_format():
    raw_text = """
    [
        {"start": "00:00", "end": "00:03", "text": "Câu một"},
        {"start": "00:03", "end": "00:05", "text": "Câu hai"}
    ]
    """
    items = parse_subtitles_text(raw_text)
    assert len(items) == 2
    assert items[0]["text"] == "Câu một"
    assert items[1]["text"] == "Câu hai"


def test_subtitles_to_srt():
    items = [
        {"start_time": "00:00:00,000", "end_time": "00:00:03,000", "text": "Dòng 1"},
        {"start_time": "00:00:03,000", "end_time": "00:00:05,000", "text": "Dòng 2"},
    ]
    srt = subtitles_to_srt(items)
    assert "1\n00:00:00,000 --> 00:00:03,000\nDòng 1" in srt
    assert "2\n00:00:03,000 --> 00:00:05,000\nDòng 2" in srt
