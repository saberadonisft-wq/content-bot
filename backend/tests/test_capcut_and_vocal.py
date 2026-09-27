from pathlib import Path

import pytest

from app.services.capcut_tts import (
    CapCutTtsError,
    _build_ssml,
    _random_device_id,
    synthesize_capcut_speech,
)


def test_random_device_id():
    d1 = _random_device_id()
    d2 = _random_device_id()
    assert len(d1) == 19
    assert d1.isdigit()
    assert d1 != d2


def test_build_ssml():
    ssml = _build_ssml("Xin chào & Hẹn gặp lại", "vi_female_1", "vi_female_1", rate=1.1)
    assert "<speak" in ssml
    assert "Xin chào &amp; Hẹn gặp lại" in ssml
    assert 'rate="1.10"' in ssml
    assert 'voice name="vi_female_1"' in ssml


def test_synthesize_capcut_speech_empty():
    with pytest.raises(CapCutTtsError, match="Văn bản đầu vào rỗng"):
        synthesize_capcut_speech("", Path("dummy.mp3"))
